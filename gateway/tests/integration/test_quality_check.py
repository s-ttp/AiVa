"""A presenter's answer-quality comparison of a live run (ADR-0023), end to end against the real policy rules.

Providers are mocked: a fake frontier reference and a fake pinned grader, so nothing here calls a model or
spends. Policy is evaluated with the real rules on a bundle built from the test's catalog (OPA `simulate`), as
Govern's invariant tests do. Runs are served on the simulated gateway and their record is then marked live:
the comparison only reads the record.
"""

import json
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import select, update

from aiva_gateway.assurance.quality_check import REFERENCE_INSTRUCTION, REFERENCE_THINKING_ALLOWANCE
from aiva_gateway.contracts.policy import PolicyDecision, PolicyInput
from aiva_gateway.core.enums import Provenance
from aiva_gateway.db.models import (
    AuditLogRow,
    ExecutionRunRow,
    ModelAttemptRow,
    PolicyEventRow,
    UsageLedgerRow,
)
from aiva_gateway.govern.bundle import build_bundle
from aiva_gateway.inference.base import CREDENTIALS_MISSING, UnavailableAdapter
from aiva_gateway.inference.registry import AdapterRegistry
from aiva_gateway.inference.simulated import SimulatedInferenceAdapter
from aiva_gateway.inference.tokens import input_token_bound
from aiva_gateway.pipeline.interfaces import (
    InferenceRequest,
    InferenceResult,
    QuotaDeniedError,
    TokenUsage,
)
from aiva_gateway.services import Services
from tests.conftest import console, with_overrides

GRADER = "openai-grader"  # the pinned grader (config/models.yaml)
REFERENCE_ANSWER = "Synthetic reference: the Traveller 10 bundle costs 99 AED for 30 days."
GRADE_REPLY = {"score": 70, "missed_points": ["the 30-day validity"]}  # the grader's JSON; no verdict
QUESTION = "How much does the synthetic Traveller 10 bundle cost?"
EVIDENCE = [
    {
        "id": "KB-ROAM-001#2",
        "text": "[KB-ROAM-001 §2] Traveller roaming bundles (synthetic) - Traveller 10\n"
        "The synthetic GCT Traveller 10 bundle costs 99 AED for 30 days.",
    }
]


class FakeModel:
    """A provider that answers with fixed text and reports its usage, and records what it was sent."""

    def __init__(
        self, content: str, *, status: str = "ok", version: str = "fake-1", finish_reason: str = "stop"
    ) -> None:
        self.content, self.status, self.version = content, status, version
        self.finish_reason = finish_reason
        self.requests: list[InferenceRequest] = []

    async def complete(self, request: InferenceRequest) -> InferenceResult:
        self.requests.append(request)
        ok = self.status == "ok"
        return InferenceResult(
            status=self.status,
            content=self.content if ok else "",
            finish_reason=self.finish_reason if ok else None,
            usage=TokenUsage(input_tokens=1200, output_tokens=150, provenance=Provenance.MEASURED),
            latency_ms=900.0,
            ttft_ms=300.0,
            provider_model_version=self.version,
            provenance=Provenance.MEASURED,
            error_code=None if ok else "UPSTREAM_ERROR",
        )


class BundleEngine:
    """The real policy rules (OPA `simulate`) on a bundle built from the given catalog and the config's
    policies, whatever bundle the test OPA last had published."""

    def __init__(self, services: Services, catalog: Any) -> None:
        self._opa = services.opa
        self._bundle = build_bundle(catalog, catalog.policies, version="test", allow_simulated_ingress=True)

    async def evaluate(self, policy_input: PolicyInput) -> PolicyDecision:
        return await self._opa.simulate(self._bundle, policy_input)


class RefusingQuota:
    def __init__(self, quota: Any) -> None:
        self._quota = quota

    def __getattr__(self, name: str) -> Any:
        return getattr(self._quota, name)

    async def reserve(self, request: Any) -> Any:
        raise QuotaDeniedError("Budget exhausted.", rule_id="TEST-BUDGET", scope_type="application")


def without_grader(catalog: Any) -> Any:
    return replace(catalog, models=catalog.models.model_copy(update={"grader_model": None}))


def comparing(
    services: Services,
    *,
    grader: FakeModel | None = None,
    reference: FakeModel | None = None,
    **overrides: Any,
) -> tuple[Services, FakeModel, FakeModel]:
    """A live gateway with fake providers for the reference and the pinned grader."""
    reference = reference or FakeModel(REFERENCE_ANSWER, version="claude-opus-5-5")
    grader = grader or FakeModel(json.dumps(GRADE_REPLY), version="gpt-6.1-sol")
    catalog = services.catalog
    assert catalog.models.grader_model == GRADER
    registry = AdapterRegistry(
        catalog,
        adapters={catalog.models.frontier_baseline: reference, GRADER: grader},
        default=SimulatedInferenceAdapter(),
    )
    svc = with_overrides(
        services,
        policy_engine=BundleEngine(services, catalog),
        adapters=registry,
        settings=services.settings.model_copy(update={"inference_mode": "live"}),
        **overrides,
    )
    return svc, reference, grader


def chat_request(application_id: str = "gct-knowledge-portal", **extra: Any) -> dict[str, Any]:
    return {
        "messages": [{"role": "user", "content": QUESTION}],
        "evidence": EVIDENCE,
        "data_classification": "internal",
        **console(application_id),
        **extra,
    }


async def served(tester_client, db, request: dict[str, Any], *, mode: str = "live") -> tuple[str, str]:  # type: ignore[no-untyped-def]
    """Serve `request` on the simulated gateway, then mark its record as served in `mode`."""
    async with tester_client() as client:
        body = (await client.post("/v1/ai/chat", json=request)).json()
    assert body["status"] == "completed", body.get("error")
    await db.execute(
        update(ExecutionRunRow).where(ExecutionRunRow.run_id == body["run_id"]).values(execution_mode=mode)
    )
    await db.commit()
    return body["run_id"], body["response"]["content"]


async def compare(make_client, user_token, svc, run_id, request, answer, user="u-tester"):  # type: ignore[no-untyped-def]
    async with make_client(user_token(user), svc=svc) as client:
        return await client.post(
            f"/v1/runs/{run_id}/quality-check", json={"request": request, "answer": answer}
        )


async def test_a_live_answer_is_compared_with_the_frontier_and_graded(  # type: ignore[no-untyped-def]
    services, tester_client, make_client, user_token, db
) -> None:
    request = chat_request()
    run_id, answer = await served(tester_client, db, request)
    served_events = {
        e.id: e.reservation_id
        for e in (await db.scalars(select(PolicyEventRow).where(PolicyEventRow.run_id == run_id))).all()
    }
    svc, reference, grader = comparing(services)
    response = await compare(make_client, user_token, svc, run_id, request, answer)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["repeated"], body["exception_applied"]) == ("compared", False, False)
    assert body["grade"] == {
        "verdict": "partial",  # from the score
        **GRADE_REPLY,
        "model_id": GRADER,
        "display_name": "GPT-6.1 Sol",
        "provenance": "estimated",
    }
    reference_name = services.catalog.model(services.catalog.models.frontier_baseline).display_name
    assert body["reference"]["display_name"] == reference_name == "Claude Opus 5.5"
    assert (
        body["reference"]["answer"] == REFERENCE_ANSWER and body["reference"]["model_id"] == "claude-flagship"
    )
    cost = body["cost"]
    assert cost["reference_micros"] > 0 and cost["grading_micros"] > 0
    assert cost["total_micros"] == cost["reference_micros"] + cost["grading_micros"]
    assert (
        cost["direct_frontier_measured_micros"] == cost["reference_micros"]
        and cost["provenance"] == "measured"
    )

    # The reference got the full, untrimmed context; the grader only question, evidence and both answers.
    (sent,) = reference.requests
    reference_payload = "\n".join(m["content"] for m in sent.messages)
    assert QUESTION in reference_payload and "costs 99 AED for 30 days" in reference_payload
    (graded,) = grader.requests
    assert graded.response_format is not None
    grader_input = json.loads(graded.messages[-1]["content"])
    assert set(grader_input) == {"question", "evidence", "reference_answer", "answer"}
    assert (grader_input["question"], grader_input["answer"]) == (QUESTION, answer)
    assert grader_input["reference_answer"] == REFERENCE_ANSWER

    # Metered on the original run with the comparison's own reservation; audited without text.
    ledger = (await db.scalars(select(UsageLedgerRow).where(UsageLedgerRow.run_id == run_id))).all()
    quality_rows = [r for r in ledger if r.charge_type in {"quality_reference", "quality_grading"}]
    assert sorted(r.charge_type for r in quality_rows) == ["quality_grading", "quality_reference"]
    (comparison_reservation,) = {r.reservation_id for r in quality_rows}
    served_reservations = {r.reservation_id for r in ledger if r not in quality_rows} | {
        reservation for reservation in served_events.values() if reservation is not None
    }
    assert served_reservations and comparison_reservation not in served_reservations
    attempts = (await db.scalars(select(ModelAttemptRow).where(ModelAttemptRow.run_id == run_id))).all()
    assert {"quality_reference", "quality_grading"} <= {a.stage for a in attempts}
    # Each call's egress decisions (before the reservation and again just before the call) carry the
    # comparison's reservation: Insights counts an S11 comparison as the exception, not a leak, by them.
    events = (await db.scalars(select(PolicyEventRow).where(PolicyEventRow.run_id == run_id))).all()
    comparison_events = sorted((e for e in events if e.id not in served_events), key=lambda e: e.sequence)
    assert [(e.stage, e.egress_model_id, e.action) for e in comparison_events] == [
        ("egress", model, "allow") for model in ("claude-flagship", GRADER, "claude-flagship", GRADER)
    ]
    assert {e.reservation_id for e in comparison_events} == {comparison_reservation}
    (audit,) = (
        await db.scalars(
            select(AuditLogRow).where(
                AuditLogRow.action == "quality.compare", AuditLogRow.target_id == run_id
            )
        )
    ).all()
    assert (audit.details["verdict"], audit.details["score"], audit.details["status"]) == (
        "partial",
        70,
        "compared",
    )
    recorded = json.dumps(audit.details)
    for text in (QUESTION, answer, REFERENCE_ANSWER, "the 30-day validity", "costs 99 AED"):
        assert text not in recorded

    # Asking again returns the recorded verdict; nothing is called, reserved or stored twice.
    again = (await compare(make_client, user_token, svc, run_id, request, answer)).json()
    assert (again["status"], again["repeated"], again["grade"]["verdict"], again["grade"]["score"]) == (
        "compared",
        True,
        "partial",
        70,
    )
    assert again["reference"]["answer"] is None and again["grade"]["missed_points"] == []
    # Both are named for people, first time and repeated, never by their id.
    for response in (body, again):
        for part in (response["reference"], response["grade"]):
            assert part["display_name"] and part["display_name"] != part["model_id"]
    assert (again["reference"]["display_name"], again["grade"]["display_name"]) == (
        "Claude Opus 5.5",
        "GPT-6.1 Sol",
    )
    assert again["cost"] == cost and len(reference.requests) == 1 and len(grader.requests) == 1
    ledger_after = (await db.scalars(select(UsageLedgerRow).where(UsageLedgerRow.run_id == run_id))).all()
    assert len(ledger_after) == len(ledger)


async def test_without_a_pinned_grader_nothing_is_called(  # type: ignore[no-untyped-def]
    services, tester_client, make_client, user_token, db
) -> None:
    request = chat_request()
    run_id, answer = await served(tester_client, db, request)
    svc, reference, _ = comparing(services)
    catalog = without_grader(services.catalog)
    unpinned = with_overrides(svc, catalog=catalog, policy_engine=BundleEngine(services, catalog))
    body = (await compare(make_client, user_token, unpinned, run_id, request, answer)).json()
    assert (body["status"], body["detail"]) == ("denied", "grader not configured")
    assert "AIPEL-QUAL-002" in body["rule_ids"] and reference.requests == []


async def test_the_comparison_is_bound_to_the_served_run(  # type: ignore[no-untyped-def]
    services, tester_client, make_client, user_token, db
) -> None:
    request = chat_request()
    run_id, answer = await served(tester_client, db, request)
    svc, reference, grader = comparing(services)
    edited = {**request, "messages": [{"role": "user", "content": QUESTION + " And in Europe?"}]}
    for body_request, body_answer in ((request, answer + " (edited)"), (edited, answer)):
        response = await compare(make_client, user_token, svc, run_id, body_request, body_answer)
        assert response.status_code == 409 and response.json()["error"]["code"] == "QUALITY_RUN_MISMATCH"
    # Someone else's run is not found; so is an unknown one.
    other = await compare(make_client, user_token, svc, run_id, request, answer, user="u-platform-admin")
    assert other.status_code == 404
    assert (await compare(make_client, user_token, svc, "run_unknown", request, answer)).status_code == 404
    assert reference.requests == [] and grader.requests == []


@pytest.mark.parametrize("mode", ["simulated", "replay", "record"])
async def test_only_a_live_run_is_compared(  # type: ignore[no-untyped-def]
    services, tester_client, make_client, user_token, db, mode: str
) -> None:
    request = chat_request()
    run_id, answer = await served(tester_client, db, request, mode=mode)
    svc, reference, _ = comparing(services)
    response = await compare(make_client, user_token, svc, run_id, request, answer)
    assert response.status_code == 409 and response.json()["error"]["code"] == "QUALITY_NOT_LIVE"
    assert reference.requests == []


async def test_presenters_only_and_authorization_before_the_body(  # type: ignore[no-untyped-def]
    services, make_client, user_token
) -> None:
    svc, _, _ = comparing(services)
    async with make_client(svc=svc) as client:
        assert (await client.post("/v1/runs/run_x/quality-check", json={})).status_code == 401
    async with make_client(user_token("u-finance"), svc=svc) as client:
        assert (await client.post("/v1/runs/run_x/quality-check", json={})).status_code == 403
    async with make_client(user_token("u-tester"), svc=svc) as client:
        assert (await client.post("/v1/runs/run_x/quality-check", json={})).status_code == 422


async def test_a_refused_envelope_calls_nothing(  # type: ignore[no-untyped-def]
    services, tester_client, make_client, user_token, db
) -> None:
    request = chat_request()
    run_id, answer = await served(tester_client, db, request)
    svc, reference, grader = comparing(services)
    svc = with_overrides(svc, quota=RefusingQuota(svc.quota))
    body = (await compare(make_client, user_token, svc, run_id, request, answer)).json()
    assert (body["status"], body["detail"], body["rule_ids"]) == (
        "not_reserved",
        "comparison not reserved",
        ["TEST-BUDGET"],
    )
    assert reference.requests == [] and grader.requests == []


async def test_an_unavailable_grader_stops_the_comparison_before_any_spend(  # type: ignore[no-untyped-def]
    services, tester_client, make_client, user_token, db
) -> None:
    request = chat_request()
    run_id, answer = await served(tester_client, db, request)
    svc, reference, grader = comparing(services)
    keyless = AdapterRegistry(
        services.catalog,
        adapters={
            services.catalog.models.frontier_baseline: reference,
            GRADER: UnavailableAdapter(CREDENTIALS_MISSING),
        },
        default=SimulatedInferenceAdapter(),
    )
    body = (
        await compare(make_client, user_token, with_overrides(svc, adapters=keyless), run_id, request, answer)
    ).json()
    assert (body["status"], body["detail"], body["reference"], body["grade"]) == (
        "failed",
        "Grader unavailable: no credentials configured",
        None,
        None,
    )
    assert body["cost"]["total_micros"] == 0 and not body["exception_applied"]
    assert reference.requests == []
    ledger = (await db.scalars(select(UsageLedgerRow).where(UsageLedgerRow.run_id == run_id))).all()
    assert not [r for r in ledger if r.charge_type in {"quality_reference", "quality_grading"}]
    # Nothing was reserved, so it is not the run's comparison: once the key is set, it can be compared.
    retried = (await compare(make_client, user_token, svc, run_id, request, answer)).json()
    assert (retried["status"], retried["repeated"]) == ("compared", False)
    assert len(reference.requests) == 1 and len(grader.requests) == 1


async def test_a_failed_reference_is_reported_and_metered(  # type: ignore[no-untyped-def]
    services, tester_client, make_client, user_token, db
) -> None:
    request = chat_request()
    run_id, answer = await served(tester_client, db, request)
    svc, _, grader = comparing(services, reference=FakeModel("", status="error"))
    body = (await compare(make_client, user_token, svc, run_id, request, answer)).json()
    assert (body["status"], body["detail"], body["grade"]) == (
        "failed",
        "the reference model did not answer",
        None,
    )
    assert grader.requests == []
    # It held a reservation: it is the run's comparison, returned as recorded.
    again = (await compare(make_client, user_token, svc, run_id, request, answer)).json()
    assert (again["status"], again["repeated"]) == ("failed", True)


class RecordingQuota:
    """The real quota, keeping each reservation request it was asked for."""

    def __init__(self, quota: Any) -> None:
        self._quota = quota
        self.requests: list[Any] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._quota, name)

    async def reserve(self, request: Any) -> Any:
        self.requests.append(request)
        return await self._quota.reserve(request)


async def test_the_reference_answers_within_the_served_budget_and_is_reserved_for_it(  # type: ignore[no-untyped-def]
    services, tester_client, make_client, user_token, db
) -> None:
    request = chat_request(max_output_tokens=700)
    run_id, answer = await served(tester_client, db, request)
    svc, reference, _ = comparing(services)
    quota = RecordingQuota(svc.quota)
    body = (
        await compare(make_client, user_token, with_overrides(svc, quota=quota), run_id, request, answer)
    ).json()
    assert body["status"] == "compared"
    (sent,) = reference.requests
    # The served request's answer budget plus the thinking allowance, never the baseline's 6,144.
    assert sent.max_output_tokens == 700 + REFERENCE_THINKING_ALLOWANCE
    assert sent.messages[0] == {"role": "system", "content": REFERENCE_INSTRUCTION}
    # The reservation's reference line is the worst case of exactly that call.
    (reserved,) = quota.requests
    flagship = services.catalog.model("claude-flagship")
    (line,) = [line for line in reserved.lines if line.model_id == "claude-flagship"]
    assert line.amount_micros == services.prices.worst_case_micros(
        flagship,
        input_tokens=input_token_bound(sent.messages),
        max_output_tokens=sent.max_output_tokens,
        attempts=1,
    )


async def test_a_reference_cut_off_at_its_limit_is_noted_and_never_graded_as_complete(  # type: ignore[no-untyped-def]
    services, tester_client, make_client, user_token, db
) -> None:
    request = chat_request()
    run_id, answer = await served(tester_client, db, request)
    truncated = FakeModel(REFERENCE_ANSWER, finish_reason="max_tokens")
    svc, _, grader = comparing(services, reference=truncated)
    body = (await compare(make_client, user_token, svc, run_id, request, answer)).json()
    assert body["status"] == "compared"
    assert body["reference"]["note"] == "The reference stopped at its length limit, so it is incomplete"
    (graded,) = grader.requests
    assert json.loads(graded.messages[-1]["content"])["reference_note"] == body["reference"]["note"]


@pytest.mark.parametrize(("score", "verdict"), [(96, "match"), (72, "partial"), (25, "miss")])
async def test_the_verdict_follows_the_score(  # type: ignore[no-untyped-def]
    services, tester_client, make_client, user_token, db, score: int, verdict: str
) -> None:
    request = chat_request()
    run_id, answer = await served(tester_client, db, request)
    reply = FakeModel(json.dumps({"score": score, "missed_points": []}))
    svc, _, _ = comparing(services, grader=reply)
    grade = (await compare(make_client, user_token, svc, run_id, request, answer)).json()["grade"]
    assert (grade["score"], grade["verdict"]) == (score, verdict)


async def test_an_invalid_verdict_is_never_partly_read(  # type: ignore[no-untyped-def]
    services, tester_client, make_client, user_token, db
) -> None:
    request = chat_request()
    run_id, answer = await served(tester_client, db, request)
    svc, _, _ = comparing(services, grader=FakeModel('{"score": 140, "missed_points": []}'))
    body = (await compare(make_client, user_token, svc, run_id, request, answer)).json()
    assert (body["status"], body["detail"], body["grade"]) == (
        "failed",
        "the grader's reply was not a valid verdict",
        None,
    )
    assert body["reference"]["answer"] == REFERENCE_ANSWER


async def test_scenario_11_sample_is_never_sent_for_comparison(  # type: ignore[no-untyped-def]
    services, tester_client, make_client, user_token, db
) -> None:
    scenario = services.catalog.scenario("live-sovereign-routing")
    assert scenario is not None and scenario.sample_evidence
    sample = {
        "messages": [{"role": "user", "content": scenario.sample_prompt}],
        "routing": {"mode": "hybrid", "tags": scenario.routing_tags},
        "data_classification": scenario.declared_classification,
        "scenario_id": scenario.id,
        "evidence": [e.model_dump() for e in scenario.sample_evidence],
        **console(scenario.application_id),
    }
    run_id, answer = await served(tester_client, db, sample)
    svc, reference, grader = comparing(services)
    body = (await compare(make_client, user_token, svc, run_id, sample, answer)).json()
    # No exception to AIPEL-INV-001 in this build: the confidential sample never leaves.
    assert (body["status"], body["exception_applied"]) == ("denied", False), body
    assert reference.requests == [] and grader.requests == []

    typed = {**sample, "messages": [{"role": "user", "content": scenario.sample_prompt + " Please."}]}
    run_id, answer = await served(tester_client, db, typed)
    svc, reference, grader = comparing(services)
    body = (await compare(make_client, user_token, svc, run_id, typed, answer)).json()
    assert (body["status"], body["detail"]) == (
        "denied",
        "Comparison is available for the synthetic sample only.",
    )
    assert reference.requests == [] and grader.requests == []


async def test_market_research_reference_gets_the_same_web_passages_or_says_it_did_not(  # type: ignore[no-untyped-def]
    services, tester_client, make_client, user_token, db
) -> None:
    from tests.integration.test_web_search import CLEAN, OTHER, ok, searching
    from tests.integration.test_web_search import request as market_request

    searched, _ = searching(services, ok(CLEAN, OTHER))
    request = market_request()
    async with tester_client(svc=searched) as client:
        served_body = (await client.post("/v1/ai/chat", json=request)).json()
    assert served_body["search"]["results_sent"] == 2, served_body.get("search")
    run_id, answer = served_body["run_id"], served_body["response"]["content"]
    await db.execute(
        update(ExecutionRunRow).where(ExecutionRunRow.run_id == run_id).values(execution_mode="live")
    )
    await db.commit()

    svc, reference, grader = comparing(services)
    body = (await compare(make_client, user_token, svc, run_id, request, answer)).json()
    assert body["status"] == "compared", body
    assert body["reference"]["passages"]["web"] == 2 and body["reference"]["note"] is None
    payload = "\n".join(m["content"] for m in reference.requests[0].messages)
    assert '<evidence id="WEB-1#1">' in payload and '<evidence id="WEB-2#1">' in payload
    assert "reference_note" not in json.loads(grader.requests[0].messages[-1]["content"])
    # Web text is never recorded, by the comparison either.
    (audit,) = (
        await db.scalars(
            select(AuditLogRow).where(
                AuditLogRow.action == "quality.compare", AuditLogRow.target_id == run_id
            )
        )
    ).all()
    assert CLEAN["url"] not in json.dumps(audit.details) and audit.details["passages"]["web"] == 2

    # Once they are no longer held (restart or TTL), the reference answers without them and says so.
    async with tester_client(svc=searched) as client:
        again_served = (await client.post("/v1/ai/chat", json=request)).json()
    run_id, answer = again_served["run_id"], again_served["response"]["content"]
    await db.execute(
        update(ExecutionRunRow).where(ExecutionRunRow.run_id == run_id).values(execution_mode="live")
    )
    await db.commit()
    from aiva_gateway.optimize.web_search import WebEvidenceMemory

    svc, reference, grader = comparing(services, web_evidence=WebEvidenceMemory())
    body = (await compare(make_client, user_token, svc, run_id, request, answer)).json()
    note = "Reference without the web results (they are no longer held)"
    assert (body["status"], body["reference"]["passages"]["web"], body["reference"]["note"]) == (
        "compared",
        0,
        note,
    )
    assert json.loads(grader.requests[0].messages[-1]["content"])["reference_note"] == note
    assert "WEB-1#1" not in "\n".join(m["content"] for m in reference.requests[0].messages)
