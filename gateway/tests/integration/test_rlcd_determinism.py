"""Identical requests get identical RLCD decisions (Scenarios' S1 rehearsal; ADR-0009).

The rehearsal saw the S1 sample prompt flip between sovereign-direct (confidential) and local-fast
(internal). The pinned model scores it confidential at 0.9994 on every run, identically on 1, 2 and 4 CPU
threads. The flips were runs that got no prediction, so the heuristic served, and it never tightens:
- runs that arrived while the model was still loading (16 s on 4 vCPU);
- runs whose 2 s budget, counted from the moment they queued for the single worker, ran out behind other
  runs (reproduced against a local gateway: 21 of 40 concurrent S1 runs);
- predictions slowed many-fold by more torch threads than the container's CPU quota (64 threads on 4 CPUs:
  4.5 s instead of 40 ms).
A run that fell back was also cached at its weaker classification, so identical requests inherited it.
"""

import asyncio
import time
from pathlib import Path

from sqlalchemy import select

from aiva_gateway.db.models import TraceSpanRow
from aiva_gateway.optimize.rlcd_model import RLCD_LABELS, ModernBertRlcd, available_cpus
from tests.conftest import chat_body, console, with_overrides
from tests.integration.test_rlcd_pipeline import SpyCache

S1 = "Why did my synthetic GCT Prepaid Plus bill increase by 15 AED this month?"  # config/scenarios.yaml
# The pinned model's output for the S1 sample prompt (measured offline on CPU, 2026-10-16).
S1_OUTPUT = {
    "task_category": (0.998756, 0.000469, 4e-05, 0.000321, 0.000108, 0.000226, 7.9e-05),
    "sensitivity": (0.000412, 0.000118, 0.999371, 2.2e-05, 7.8e-05),
    "complexity": (0.011352, 0.446591, 0.54005, 0.002007),
    "context_need": (0.001857, 0.997805, 0.000338),
}


class StandInModel:
    """The loaded classifier's stand-in on the worker thread: `S1_OUTPUT` after `seconds` of scoring."""

    config = {"heads": {head: list(labels) for head, labels in RLCD_LABELS.items()}}

    def __init__(self, seconds: float = 0.0, *, fail: bool = False) -> None:
        self.seconds, self.fail, self.calls = seconds, fail, 0

    def probabilities(self, text: str) -> dict[str, tuple[float, ...]]:
        self.calls += 1
        time.sleep(self.seconds)
        if self.fail:
            raise RuntimeError("scoring failed")
        return S1_OUTPUT


class StandInRlcd(ModernBertRlcd):
    """The production classifier wrapper around the stand-in model, which loads in `load_seconds`."""

    def __init__(
        self, model: StandInModel, *, load_seconds: float = 0.0, timeout_seconds: float = 2.0
    ) -> None:
        super().__init__(threads=1, timeout_seconds=timeout_seconds)
        self.model, self.load_seconds = model, load_seconds

    def _load(self) -> StandInModel:
        time.sleep(self.load_seconds)
        return self.model


class UnloadableRlcd(StandInRlcd):
    def _load(self) -> StandInModel:
        raise OSError("no weights")


def ask(text: str) -> list[dict[str, str]]:
    return [{"role": "user", "content": text}]


async def test_s1_gets_one_path_and_one_classification_every_time(services, tester_client) -> None:  # type: ignore[no-untyped-def]
    model = StandInModel(seconds=0.15)
    rlcd = StandInRlcd(model, load_seconds=0.5)
    payload = {**chat_body(S1, scenario_id="customer-support"), **console("gct-support-chatbot")}
    loading = asyncio.create_task(rlcd.start())  # the gateway has just started: the model is loading
    try:
        async with tester_client(svc=with_overrides(services, rlcd=rlcd)) as client:
            # 24 at once (queued 3.6 s deep behind one worker before the fix), then one at a time.
            burst = await asyncio.gather(*(client.post("/v1/ai/chat", json=payload) for _ in range(24)))
            sequential = [await client.post("/v1/ai/chat", json=payload) for _ in range(4)]
        await loading
    finally:
        await rlcd.aclose()
    bodies = [response.json() for response in [*burst, *sequential]]
    assert all(body["status"] == "completed" for body in bodies), bodies[0].get("error")
    decisions = {
        (body["execution_path"], body["policy"]["effective_classification"], body["routing"]["rlcd_version"])
        for body in bodies
    }
    assert decisions == {("sovereign-direct", "confidential", rlcd.version)}
    assert model.calls == 1  # identical inputs share one prediction


async def test_waiting_for_the_worker_does_not_count_against_the_models_time_budget() -> None:
    model = StandInModel(seconds=0.1)
    rlcd = StandInRlcd(model, timeout_seconds=0.3)
    await rlcd.start()
    try:
        # Eight different inputs queue behind one worker for up to 0.8 s; each scores in 0.1 s.
        results = await asyncio.gather(*(rlcd.classify(ask(f"question {i}")) for i in range(8)))
    finally:
        await rlcd.aclose()
    assert all(result is not None for result in results) and model.calls == 8


async def test_a_prediction_over_budget_falls_back_once_and_its_late_result_is_kept() -> None:
    model = StandInModel(seconds=0.4)
    rlcd = StandInRlcd(model, timeout_seconds=0.1)
    await rlcd.start()
    try:
        assert await rlcd.classify(ask(S1)) is None  # over the model's budget: the heuristic serves this run
        await asyncio.sleep(0.5)
        late = await rlcd.classify(ask(S1))
    finally:
        await rlcd.aclose()
    assert late is not None and late.probabilities == S1_OUTPUT and model.calls == 1


async def test_a_failed_prediction_is_retried_and_a_failed_load_never_waits() -> None:
    model = StandInModel(fail=True)
    rlcd = StandInRlcd(model)
    await rlcd.start()
    try:
        assert await rlcd.classify(ask(S1)) is None
        assert await rlcd.classify(ask(S1)) is None
    finally:
        await rlcd.aclose()
    assert model.calls == 2  # a failure is not remembered

    broken = UnloadableRlcd(StandInModel())
    await broken.start()
    started = time.perf_counter()
    assert await broken.classify(ask(S1)) is None and not broken.ready  # every run: the heuristic
    assert time.perf_counter() - started < 0.1
    await broken.aclose()


async def test_a_run_the_serving_model_missed_is_not_cached_or_reused(services, tester_client, db) -> None:  # type: ignore[no-untyped-def]
    # Knowledge portal: a shared cache, so the heuristic run's answer would be stored at `internal`.
    cache, rlcd = SpyCache(), StandInRlcd(StandInModel(fail=True))
    await rlcd.start()
    payload = {**chat_body("Which bundles include unlimited SMS?"), **console("gct-knowledge-portal")}
    try:
        async with tester_client(svc=with_overrides(services, rlcd=rlcd, cache=cache)) as client:
            body = (await client.post("/v1/ai/chat", json=payload)).json()
    finally:
        await rlcd.aclose()
    assert body["status"] == "completed" and body["routing"]["rlcd_version"] == "heuristic-v0"
    assert cache.stored == []
    spans = {
        span.name: span.attributes
        for span in (
            await db.execute(select(TraceSpanRow).where(TraceSpanRow.run_id == body["run_id"]))
        ).scalars()
    }
    assert spans["rlcd1"]["aiva.rlcd.fallback"] == "error"
    assert spans["reconciliation"]["aiva.cache.store_skipped"] == "RLCD model missed this run"


async def test_the_gateway_is_not_ready_until_the_model_has_loaded(make_client, services) -> None:  # type: ignore[no-untyped-def]
    settings = services.settings.model_copy(update={"rlcd_backend": "model"})
    rlcd = StandInRlcd(StandInModel(), load_seconds=0.3)
    broken = UnloadableRlcd(StandInModel())

    async def readiness(classifier: StandInRlcd) -> tuple[int, str]:
        async with make_client(svc=with_overrides(services, settings=settings, rlcd=classifier)) as client:
            response = await client.get("/health/ready")
        return response.status_code, response.json()["checks"]["rlcd_model"]

    loading = asyncio.create_task(rlcd.start())
    try:
        assert await readiness(rlcd) == (503, "loading")  # the proxy answers GATEWAY_STARTING meanwhile
        await loading
        assert await readiness(rlcd) == (200, "ok")
        await broken.start()
        # A failed load is ready but degraded: the labelled heuristic serves rather than the demo going down.
        assert await readiness(broken) == (200, "failed")
    finally:
        await rlcd.aclose()
        await broken.aclose()
    async with make_client() as client:  # the heuristic backend has no model to wait for
        response = await client.get("/health/ready")
    assert response.status_code == 200 and "rlcd_model" not in response.json()["checks"]


def test_torch_threads_respect_the_container_cpu_quota(tmp_path: Path) -> None:
    unlimited = available_cpus(cgroup=tmp_path)
    (tmp_path / "cpu.max").write_text("100000 100000\n")  # cgroup v2: one CPU
    assert available_cpus(cgroup=tmp_path) == 1
    (tmp_path / "cpu.max").write_text("max 100000\n")
    assert available_cpus(cgroup=tmp_path) == unlimited
    (tmp_path / "cpu.max").unlink()
    (tmp_path / "cpu.cfs_quota_us").write_text("150000\n")  # cgroup v1: 1.5 CPUs -> 1 thread
    (tmp_path / "cpu.cfs_period_us").write_text("100000\n")
    assert available_cpus(cgroup=tmp_path) == 1
