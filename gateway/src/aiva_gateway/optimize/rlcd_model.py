"""RLCD classifier: ModernBERT-base with four calibrated heads, in-process on CPU (LLD §10, ADR-0009).

Loading and scoring mirror ML & Data's reference (`ml/jobs/rlcd_predict.py`, `RlcdClassifier`) exactly:
masked mean pooling of the encoder's last hidden state, one linear head per output, and
softmax(logits / temperature) with the temperatures fitted on validation (`rlcd_config.json`).

- The pinned revision is downloaded with `HF_TOKEN` and loaded once, in the background, at startup. The
  gateway is not ready (`/health/ready`) while it loads, so the proxy holds requests; a run that still
  arrives while it loads waits for it (up to 60 s). Either way the same request is not routed one way
  before the model is ready and another way after. If the load fails, the gateway is ready and the
  heuristic serves, labelled as such.
- Identical inputs get identical predictions: a prediction is shared by every run with the same input while
  it is in flight, and remembered afterwards (by input hash, in memory only).
- The time budget (2 s) is the model's own time on one input; waiting for the worker behind other inputs
  does not count (bounded at 30 s). A run that gets no prediction (the load failed, the budget or the wait
  ran out, scoring raised) is routed by the heuristic, labelled as such, and says why in its `rlcd1` span
  (`aiva.rlcd.fallback`). RLCD can never weaken policy, so that is always safe.
- Inference runs on one worker thread, so it never blocks the event loop; torch uses the CPUs the container
  may use (its CPU quota), not the host's core count.
- PyTorch and transformers come from the optional `rlcd` extra and are imported only when the model loads.
- `FixtureRlcd` serves recorded model outputs (ML's parity rows) offline: CI needs neither the weights nor
  `HF_TOKEN`.
"""

import asyncio
import hashlib
import importlib
import json
import logging
import os
import time
from collections import OrderedDict
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

from opentelemetry import trace

from aiva_gateway.optimize.rlcd import render
from aiva_gateway.pipeline.interfaces import RlcdClassifier, RlcdPrediction

logger = logging.getLogger(__name__)

# RLCD v2 (ADR-0009, 2026-10-27 amendment): retrained on natural requests, a drop-in for v1 (same heads,
# classes, rendering and layout). v1 is kept privately at s-ttp/aiva-rlcd-modernbert-v1@db20e9922bbd for a
# rollback.
RLCD_REPO = "s-ttp/aiva-rlcd-modernbert"
RLCD_REVISION = "817911b1b4975d70aa0e7127b33212fa17e76898"
RLCD_TIMEOUT_SECONDS = 2.0  # the model's own time on one input
RLCD_QUEUE_SECONDS = 30.0  # waiting for the worker while it scores other inputs
RLCD_LOAD_WAIT_SECONDS = 60.0  # a run arriving while the model loads waits this long for it
REMEMBERED_PREDICTIONS = 1024
CGROUP = Path("/sys/fs/cgroup")


def model_version(repo: str, revision: str) -> str:
    return f"{repo.split('/')[-1]}@{revision[:12]}"


def available_cpus(*, cgroup: Path = CGROUP) -> int:
    """CPUs this process may use: its CPU affinity, capped by the container's cgroup CPU quota.

    `os.cpu_count()` reports the host's cores. In a quota-limited container, torch would start that many
    threads on fewer CPUs, which slows a prediction many-fold (64 threads on 4 CPUs: 4.5 s instead of 40 ms).
    """
    cpus = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count() or 1
    try:
        quota, period = (cgroup / "cpu.max").read_text().split()[:2]  # cgroup v2
    except (OSError, ValueError):
        try:  # cgroup v1
            quota = (cgroup / "cpu.cfs_quota_us").read_text().strip()
            period = (cgroup / "cpu.cfs_period_us").read_text().strip()
        except OSError:
            return cpus
    if quota in {"max", "-1"}:
        return cpus
    try:
        return max(1, min(cpus, int(quota) // int(period)))
    except (ValueError, ZeroDivisionError):
        return cpus


class _Scorer(Protocol):
    """The loaded model as the worker uses it (`_Classifier`)."""

    @property
    def config(self) -> Mapping[str, Any]: ...

    def probabilities(self, text: str) -> dict[str, tuple[float, ...]]: ...


class _Fallback(Exception):
    """No prediction for this run; `reason` goes on the run's `rlcd1` span."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Job:
    """One scoring of one input on the worker, shared by every run with the same input."""

    def __init__(
        self, loop: asyncio.AbstractEventLoop, executor: ThreadPoolExecutor, scorer: _Scorer, text: str
    ):
        self.started = asyncio.Event()
        self.started_at = 0.0

        def score() -> dict[str, tuple[float, ...]]:
            self.started_at = time.perf_counter()
            loop.call_soon_threadsafe(self.started.set)
            return scorer.probabilities(text)

        self.future = loop.run_in_executor(executor, score)

    @property
    def failed(self) -> bool:
        return self.future.done() and (self.future.cancelled() or self.future.exception() is not None)


class _Classifier:
    """The reference loading and scoring, on CPU. Built and called only on the worker thread."""

    def __init__(self, path: Path, threads: int) -> None:
        torch: Any = importlib.import_module("torch")
        transformers: Any = importlib.import_module("transformers")
        safetensors: Any = importlib.import_module("safetensors.torch")
        torch.set_num_threads(threads)
        self.torch = torch
        self.config: dict[str, Any] = json.loads((path / "rlcd_config.json").read_text())
        self.tokenizer = transformers.AutoTokenizer.from_pretrained(path)
        encoder = transformers.AutoModel.from_config(
            transformers.AutoConfig.from_pretrained(path / "encoder")
        )
        hidden = encoder.config.hidden_size
        heads = {n: torch.nn.Linear(hidden, len(c)) for n, c in self.config["heads"].items()}
        module = torch.nn.Module()
        module.encoder = encoder
        module.heads = torch.nn.ModuleDict(heads)
        # Same parameter names as the trainer ("encoder.*", "heads.<name>.*"); strict on purpose.
        module.load_state_dict(safetensors.load_file(str(path / "model.safetensors")), strict=True)
        module.eval()
        self.module = module

    def probabilities(self, text: str) -> dict[str, tuple[float, ...]]:
        torch = self.torch
        with torch.no_grad():
            batch = self.tokenizer(
                text, truncation=True, max_length=self.config["max_length"], return_tensors="pt"
            )
            hidden = self.module.encoder(**batch).last_hidden_state
            mask = batch["attention_mask"].unsqueeze(-1).to(hidden.dtype)
            pooled = (hidden * mask).sum(1) / mask.sum(1).clamp(min=1.0)
            return {
                name: tuple(
                    float(p) for p in (head(pooled) / self.config["temperatures"][name]).softmax(-1)[0]
                )
                for name, head in self.module.heads.items()
            }


class ModernBertRlcd:
    """Downloads and loads the pinned model in the background; classifies off the event loop."""

    def __init__(
        self,
        *,
        repo: str = RLCD_REPO,
        revision: str = RLCD_REVISION,
        token: str | None = None,
        local_dir: Path | None = None,
        threads: int | None = None,
        timeout_seconds: float = RLCD_TIMEOUT_SECONDS,
        queue_seconds: float = RLCD_QUEUE_SECONDS,
        load_wait_seconds: float = RLCD_LOAD_WAIT_SECONDS,
    ) -> None:
        self.version = model_version(repo, revision)
        self._repo, self._revision, self._token, self._local_dir = repo, revision, token, local_dir
        self._threads = threads or available_cpus()
        self._timeout, self._queue, self._load_wait = timeout_seconds, queue_seconds, load_wait_seconds
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aiva-rlcd")
        self._classifier: _Scorer | None = None
        self._labels: Mapping[str, tuple[str, ...]] = {}
        self._loading: asyncio.Event | None = None
        self._jobs: OrderedDict[str, _Job] = OrderedDict()

    @property
    def ready(self) -> bool:
        return self._classifier is not None

    @property
    def status(self) -> str:
        """For readiness: `ok` once loaded, `failed` if the load failed, otherwise `loading`."""
        if self._classifier is not None:
            return "ok"
        return "failed" if self._loading is not None and self._loading.is_set() else "loading"

    def _load(self) -> _Scorer:
        for module in ("torch", "transformers", "safetensors"):  # the `rlcd` extra, before any download
            importlib.import_module(module)
        path = self._local_dir
        if path is None:
            hub: Any = importlib.import_module("huggingface_hub")
            path = Path(hub.snapshot_download(self._repo, revision=self._revision, token=self._token))
        return _Classifier(path, self._threads)

    async def start(self) -> None:
        """Load the model; on failure stay unavailable (the heuristic serves) and say why in the log."""
        self._loading = asyncio.Event()
        started = time.perf_counter()
        try:
            classifier = await asyncio.get_running_loop().run_in_executor(self._executor, self._load)
        except Exception:
            logger.warning(
                "RLCD model %s unavailable; the heuristic RLCD serves", self.version, exc_info=True
            )
        else:
            self._labels = {name: tuple(labels) for name, labels in classifier.config["heads"].items()}
            self._classifier = classifier
            logger.info("RLCD model %s loaded in %.1f s", self.version, time.perf_counter() - started)
        finally:
            self._loading.set()  # runs waiting for the load go on, with the model or the heuristic

    async def classify(self, messages: list[dict[str, str]]) -> RlcdPrediction | None:
        started = time.perf_counter()
        try:
            probabilities = await self._predict(render(messages))
        except _Fallback as fallback:
            trace.get_current_span().set_attribute("aiva.rlcd.fallback", fallback.reason)
            return None
        return RlcdPrediction(
            model_version=self.version,
            labels=self._labels,
            probabilities=probabilities,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    async def _predict(self, text: str) -> dict[str, tuple[float, ...]]:
        if self._classifier is None and self._loading is not None and not self._loading.is_set():
            try:
                await asyncio.wait_for(self._loading.wait(), self._load_wait)
            except TimeoutError:
                logger.warning("RLCD model still loading after %.0f s; heuristic used", self._load_wait)
                raise _Fallback("loading") from None
        classifier = self._classifier
        if classifier is None:
            raise _Fallback("unavailable")
        job = self._job(classifier, text)
        if not job.started.is_set():
            try:
                await asyncio.wait_for(job.started.wait(), self._queue)
            except TimeoutError:
                logger.warning("RLCD worker busy for %.0f s; heuristic used for this run", self._queue)
                raise _Fallback("queue") from None
        remaining = self._timeout - (time.perf_counter() - job.started_at)
        try:
            # Shielded: a run giving up never cancels the prediction other runs share.
            return await asyncio.wait_for(asyncio.shield(job.future), max(remaining, 0.0))
        except TimeoutError:
            logger.warning("RLCD prediction exceeded %.1f s; heuristic used for this run", self._timeout)
            raise _Fallback("timeout") from None
        except Exception:
            logger.warning("RLCD prediction failed; heuristic used for this run", exc_info=True)
            raise _Fallback("error") from None

    def _job(self, classifier: _Scorer, text: str) -> _Job:
        """The prediction for this input: in flight or remembered, else a new one. Failures are retried."""
        key = hashlib.sha256(text.encode()).hexdigest()
        job = self._jobs.get(key)
        if job is None or job.failed:
            job = _Job(asyncio.get_running_loop(), self._executor, classifier, text)
            self._jobs[key] = job
        self._jobs.move_to_end(key)
        while len(self._jobs) > REMEMBERED_PREDICTIONS:
            self._jobs.popitem(last=False)
        return job

    async def aclose(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


class FixtureRlcd:
    """Recorded outputs of the pinned model, keyed by rendered input (ML's parity rows); offline, for CI."""

    def __init__(self, path: Path, *, labels: Mapping[str, tuple[str, ...]], version: str) -> None:
        self.version = version
        self._labels = labels
        self._rows: dict[str, dict[str, tuple[float, ...]]] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                self._rows[row["text"]] = {h: tuple(v["probs"]) for h, v in row["expected"].items()}

    async def classify(self, messages: list[dict[str, str]]) -> RlcdPrediction | None:
        probabilities = self._rows.get(render(messages))
        if probabilities is None:
            return None  # not a recorded input: the heuristic serves, as when the model is unavailable
        return RlcdPrediction(self.version, self._labels, probabilities, latency_ms=0.0)


class NoRlcd:
    """The RLCD model is not configured: every run uses the heuristic."""

    version = "none"

    async def classify(self, messages: list[dict[str, str]]) -> RlcdPrediction | None:
        return None


RLCD_LABELS: Mapping[str, tuple[str, ...]] = {
    "task_category": ("qa", "summarization", "coding", "rag", "analysis", "generation", "agentic"),
    "sensitivity": ("public", "internal", "confidential", "restricted", "sovereign-only"),
    "complexity": ("simple", "moderate", "complex", "advanced"),
    "context_need": ("sufficient", "needs-retrieval", "needs-packaging"),
}


def build_rlcd(
    backend: str, *, token: str | None, fixture: Path | None, local_dir: Path | None = None
) -> RlcdClassifier:
    if backend == "model":
        return ModernBertRlcd(token=token, local_dir=local_dir)
    if backend == "fixture":
        if fixture is None:
            raise ValueError("rlcd_backend=fixture needs rlcd_fixture_path")
        return FixtureRlcd(fixture, labels=RLCD_LABELS, version=model_version(RLCD_REPO, RLCD_REVISION))
    if backend == "heuristic":
        return NoRlcd()
    raise ValueError(f"rlcd_backend must be model, fixture or heuristic, got {backend!r}")
