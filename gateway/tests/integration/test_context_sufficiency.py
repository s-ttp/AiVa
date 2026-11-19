"""Exit gate: no context loss in critical cases (LLD §9, §17; PLAN P2).

Deliberately faulty CIM packages try to drop, alter, collapse or invent facts. Whatever the CIM does, the
payload actually submitted upstream must carry every critical fact verbatim: either the package passes
the Context Guard, or the full eligible context is restored. The CIM call is retried once when its
output is malformed, each attempt policy-checked and metered.
"""

import time

from sqlalchemy import select

from aiva_gateway.catalog.models import ModelSpec
from aiva_gateway.contracts.cim import CimEvidence, CimMessage, CimRequest, CimResponse, RequiredFact
from aiva_gateway.core.enums import Provenance
from aiva_gateway.db.models import (
    ModelAttemptRow,
    OptimizationEventRow,
    PolicyEventRow,
    TraceSpanRow,
    UsageLedgerRow,
)
from aiva_gateway.inference.registry import AdapterRegistry
from aiva_gateway.inference.simulated import SimulatedInferenceAdapter
from aiva_gateway.optimize.cim import EVIDENCE_PREAMBLE, evidence_block
from aiva_gateway.pipeline.interfaces import CimCallResult, InferenceRequest, InferenceResult, TokenUsage
from aiva_gateway.pipeline.orchestrator import full_context_tokens
from tests.conftest import chat_body, console, with_overrides

ALARMS = {
    "alarm-1": "alarm_id: ALM-7781\nsite_id: RUH-0412\nraised_at: 2026-10-14T06:02:11Z\nseverity: major\n"
    "Microwave hop MW-12 down; 3 cells affected.",
    "alarm-2": "alarm_id: ALM-7790\nsite_id: RUH-0419\nraised_at: 2026-10-14T06:03:40Z\nseverity: critical\n"
    "Power fault; battery at 18 percent.",
    "note-1": "Synthetic canteen menu for the NOC this week.",
}
CRITICAL_FACTS = [
    "ALM-7781",
    "RUH-0412",
    "2026-10-14T06:02:11Z",
    "major",
    "3 cells",
    "ALM-7790",
    "RUH-0419",
    "2026-10-14T06:03:40Z",
    "critical",
    "18 percent",
]
REQUIRED = ("alarm_id", "site_id", "raised_at", "severity")


class CapturingAdapter(SimulatedInferenceAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.payloads: list[str] = []

    async def complete(self, request: InferenceRequest) -> InferenceResult:
        self.payloads.append("\n".join(m["content"] for m in request.messages))
        return await super().complete(request)


def package(selected: list[str], texts: dict[str, str] | None = None, **overrides: object) -> CimResponse:
    texts = {**ALARMS, **(texts or {})}
    facts = [
        RequiredFact(field=field, value=value, source_id=evidence_id)
        for evidence_id in selected
        for field, value in (line.split(": ", 1) for line in ALARMS[evidence_id].splitlines() if ": " in line)
        if field in REQUIRED
    ]
    blocks = "\n".join(evidence_block(i, texts[i]) for i in selected)
    data: dict[str, object] = {
        "intent": "correlate alarms",
        "required_facts": facts,
        "selected_evidence_ids": selected,
        "omitted_evidence_ids": [i for i in ALARMS if i not in selected],
        "upstream_messages": [
            CimMessage(role="system", content=f"{EVIDENCE_PREAMBLE}\n{blocks}"),
            CimMessage(role="user", content="Correlate the alarms."),
        ],
        "self_reported_complete": True,
    }
    data.update(overrides)
    return CimResponse.model_validate(data)


class ScriptedCim:
    """Returns the scripted outcomes in order (a CimResponse, or an error code)."""

    revision = "scripted-cim"

    def __init__(self, *outcomes: CimResponse | tuple[str, bool]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    async def package(self, request: CimRequest, model: ModelSpec) -> CimCallResult:
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        usage = TokenUsage(input_tokens=200, output_tokens=150, provenance=Provenance.SIMULATED)
        started = time.perf_counter()
        if isinstance(outcome, CimResponse):
            return CimCallResult(outcome, usage, self.revision, 1.0, Provenance.SIMULATED)
        code, retryable = outcome
        return CimCallResult(
            None,
            usage,
            self.revision,
            (time.perf_counter() - started) * 1000,
            Provenance.SIMULATED,
            code,
            retryable,
        )


async def run(services, tester_client, cim: ScriptedCim):  # type: ignore[no-untyped-def]
    adapter = CapturingAdapter()
    svc = with_overrides(
        services, cim=cim, adapters=AdapterRegistry(services.catalog, adapters={}, default=adapter)
    )
    payload = chat_body(
        "Correlate the alarms.",
        scenario_id="network-operations",
        evidence=[{"id": k, "text": v} for k, v in ALARMS.items()],
        **console("gct-noc-assistant"),
    )
    async with tester_client(svc=svc) as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["status"] == "completed", body.get("error")
    assert len(adapter.payloads) == 1
    return body, adapter.payloads[0]


def assert_no_context_loss(sent: str) -> None:
    missing = [fact for fact in CRITICAL_FACTS if fact not in sent]
    assert not missing, f"critical facts lost: {missing}"


FAULTY = {
    "drops a passage holding required facts": package(["alarm-1"]),
    "alters a number": package(
        ["alarm-1", "alarm-2"], {"alarm-2": ALARMS["alarm-2"].replace("18 percent", "81 percent")}
    ),
    "collapses the timeline": package(
        ["alarm-1", "alarm-2"],
        {"alarm-2": ALARMS["alarm-2"].replace("2026-10-14T06:03:40Z", "2026-10-14T06:02:11Z")},
    ),
    "invents a source": package(
        ["alarm-1", "alarm-2"], selected_evidence_ids=["alarm-1", "alarm-2", "alarm-9"]
    ),
    "drops a citation": package(["alarm-1", "alarm-2"]).model_copy(
        update={"upstream_messages": [CimMessage(role="user", content="Correlate the alarms.")]}
    ),
}


async def test_faulty_packages_never_lose_context(services, tester_client) -> None:  # type: ignore[no-untyped-def]
    for defect, faulty in FAULTY.items():
        body, sent = await run(services, tester_client, ScriptedCim(faulty))
        assert body["cim"]["sufficiency"] == "failed-fallback-applied", defect
        assert body["routing"]["context_strategy"] == "full", defect
        assert_no_context_loss(sent)


async def test_verified_package_removes_only_irrelevant_context(services, tester_client) -> None:  # type: ignore[no-untyped-def]
    body, sent = await run(services, tester_client, ScriptedCim(package(["alarm-1", "alarm-2"])))
    assert body["cim"]["sufficiency"] == "verified"
    assert_no_context_loss(sent)
    assert "canteen" not in sent
    assert body["usage"]["constructed_context_tokens"] < body["usage"]["original_context_tokens"] + 60


async def test_the_run_view_counts_the_passages_the_cim_was_given_and_kept(
    services, tester_client, db
) -> None:  # type: ignore[no-untyped-def]
    # Story mode reads counts, not `cim.message`: retrieved once retrieval ran, kept and omitted when the CIM
    # returned a package, by `passage_counts` (an id that names no passage keeps nothing).
    def counts(body: dict) -> tuple[int | None, int | None, int | None]:  # type: ignore[type-arg]
        cim = body["cim"]
        return cim["passages_retrieved"], cim["passages_kept"], cim["passages_omitted"]

    verified, _ = await run(services, tester_client, ScriptedCim(package(["alarm-1", "alarm-2"])))
    assert verified["cim"]["sufficiency"] == "verified" and counts(verified) == (3, 2, 1)
    span = await db.scalar(
        select(TraceSpanRow.attributes).where(
            TraceSpanRow.run_id == verified["run_id"], TraceSpanRow.name == "cim"
        )
    )
    assert span is not None
    assert (span["aiva.cim.passages_kept"], span["aiva.cim.passages_omitted"]) == (2, 1)
    # The Guard failed the package: its selection is still what the CIM kept; the full context was sent.
    invented, _ = await run(services, tester_client, ScriptedCim(FAULTY["invents a source"]))
    assert invented["cim"]["sufficiency"] == "failed-fallback-applied" and counts(invented) == (3, 2, 1)
    # No package at all: nothing kept or omitted to count.
    failed, _ = await run(services, tester_client, ScriptedCim(("CIM_TRUNCATED", False)))
    assert failed["cim_invoked"] and counts(failed) == (3, None, None)


async def test_context_savings_start_from_the_full_context(services, tester_client, db) -> None:  # type: ignore[no-untyped-def]
    # "Before" is the full context, counted as the constructed context is: a verified package that omits a
    # passage saves its tokens, and a fallback to the full context saves nothing and adds nothing.
    full = full_context_tokens(
        [{"role": "user", "content": "Correlate the alarms."}],
        [CimEvidence(id=k, text=v) for k, v in ALARMS.items()],
    )

    async def before_after(body: dict) -> tuple[int, int]:  # type: ignore[type-arg]
        event = await db.scalar(
            select(OptimizationEventRow).where(OptimizationEventRow.run_id == body["run_id"])
        )
        assert event is not None
        assert (body["usage"]["original_context_tokens"], body["usage"]["constructed_context_tokens"]) == (
            event.context_tokens_before,
            event.context_tokens_after,
        )
        return event.context_tokens_before, event.context_tokens_after

    verified, _ = await run(services, tester_client, ScriptedCim(package(["alarm-1", "alarm-2"])))
    before, after = await before_after(verified)
    assert verified["cim"]["sufficiency"] == "verified" and before == full and after < before
    fallback, _ = await run(services, tester_client, ScriptedCim(FAULTY["alters a number"]))
    assert fallback["cim"]["sufficiency"] == "failed-fallback-applied"
    assert await before_after(fallback) == (full, full)


async def test_malformed_cim_output_is_retried_once_with_egress_checks(services, tester_client, db) -> None:  # type: ignore[no-untyped-def]
    cim = ScriptedCim(("CIM_INVALID_JSON", True), package(["alarm-1", "alarm-2"]))
    body, sent = await run(services, tester_client, cim)
    assert cim.calls == 2 and body["cim"]["sufficiency"] == "verified"
    assert_no_context_loss(sent)
    attempts = sorted(
        (await db.execute(select(ModelAttemptRow).where(ModelAttemptRow.run_id == body["run_id"]))).scalars(),
        key=lambda a: a.sequence,
    )
    assert [(a.stage, a.status) for a in attempts] == [("cim", "error"), ("cim", "ok"), ("upstream", "ok")]
    # Per-call tokens, the failed CIM attempt included: every call that used tokens, in order.
    by_model = [
        (u["model_id"], u["stage"], u["input_tokens"], u["output_tokens"]) for u in body["usage"]["by_model"]
    ]
    assert by_model == [
        (a.model_id, a.stage, a.input_tokens, a.output_tokens) for a in attempts
    ] and by_model[0][2:] == (200, 150)
    assert {u["provenance"] for u in body["usage"]["by_model"]} == {"simulated"}
    assert attempts[0].error_code == "CIM_INVALID_JSON" and attempts[1].retry_of == attempts[0].attempt_id
    charges = {
        row.attempt_id: row.charge_type
        for row in (
            await db.execute(select(UsageLedgerRow).where(UsageLedgerRow.run_id == body["run_id"]))
        ).scalars()
    }
    assert charges[attempts[0].attempt_id] == "cim" and charges[attempts[1].attempt_id] == "retry"
    egress = sorted(
        (
            await db.execute(
                select(PolicyEventRow).where(
                    PolicyEventRow.run_id == body["run_id"], PolicyEventRow.stage == "egress"
                )
            )
        ).scalars(),
        key=lambda e: e.sequence,
    )
    assert [e.egress_model_id for e in egress] == [a.model_id for a in attempts]  # one decision per call


async def test_retry_is_bounded_then_full_context_is_restored(services, tester_client) -> None:  # type: ignore[no-untyped-def]
    cim = ScriptedCim(("CIM_SCHEMA_INVALID", True))
    body, sent = await run(services, tester_client, cim)
    assert cim.calls == 2 and body["cim"]["sufficiency"] == "failed-fallback-applied"
    assert_no_context_loss(sent)


async def test_non_retryable_cim_failure_falls_back_immediately(services, tester_client) -> None:  # type: ignore[no-untyped-def]
    cim = ScriptedCim(("CIM_TRUNCATED", False))
    body, sent = await run(services, tester_client, cim)
    assert cim.calls == 1 and body["cim"]["sufficiency"] == "failed-fallback-applied"
    assert_no_context_loss(sent)


async def test_package_beyond_the_reserved_input_bound_restores_the_full_context(
    services, tester_client, db
) -> None:  # type: ignore[no-untyped-def]
    """Multi-byte text can pass the Guard's token budget yet exceed the reservation's byte bound."""
    dense = package(["alarm-1", "alarm-2"])
    note = CimMessage(role="system", content="€" * 6000)  # 1,500 estimated tokens, 18,000 bytes
    dense = dense.model_copy(update={"upstream_messages": [*dense.upstream_messages, note]})
    body, sent = await run(services, tester_client, ScriptedCim(dense))
    assert body["cim"]["sufficiency"] == "verified" and body["routing"]["context_strategy"] == "full"
    assert "€" not in sent
    assert_no_context_loss(sent)
    span = (
        await db.execute(
            select(TraceSpanRow).where(
                TraceSpanRow.run_id == body["run_id"], TraceSpanRow.name == "preflight"
            )
        )
    ).scalar_one()
    assert span.attributes["aiva.preflight.context_restored"] is True


async def test_a_package_that_drops_every_passage_falls_back_to_the_full_context(
    services, tester_client
) -> None:  # type: ignore[no-untyped-def]
    # S6's notice step: no scenario profile (`general`, no required fields) and the CIM keeps nothing. The
    # answering model must still be grounded: the Guard fails it and the full eligible context is sent.
    empty = package([], selected_evidence_ids=[], omitted_evidence_ids=list(ALARMS))
    adapter = CapturingAdapter()
    svc = with_overrides(
        services,
        cim=ScriptedCim(empty),
        adapters=AdapterRegistry(services.catalog, adapters={}, default=adapter),
    )
    payload = chat_body(
        "Draft a short customer notice based only on the evidence.",
        evidence=[{"id": k, "text": v} for k, v in ALARMS.items()],
        **console("gct-noc-assistant"),
    )
    async with tester_client(svc=svc) as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["status"] == "completed", body.get("error")
    assert body["cim_invoked"] and body["cim"]["sufficiency"] == "failed-fallback-applied"
    assert body["routing"]["context_strategy"] == "full"
    [sent] = adapter.payloads
    assert_no_context_loss(sent)  # every passage reached the answering model
