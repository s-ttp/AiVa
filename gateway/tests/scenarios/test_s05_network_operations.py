"""Scenario 5 — Network Operations Intelligence (LLD §15).

Primary proof: alarm correlation, high complexity, source integrity.

- High complexity: sixty synthetic alarms at `critical` quality assess as complex. Only a model of
  quality rank 3 qualifies, so RLCD routes to the pinned frontier model (replayed). The answer
  correlates the microwave alarms of RUH-0412 and RUH-0419.
- Source integrity: the `network-alarm-correlation` profile requires alarm_id, site_id, raised_at and
  severity. The Context Guard verifies the package independently of the CIM: values verbatim from their
  source, every timestamp kept, no altered numbers, every kept passage cited. The answer's alarm IDs cite
  their passages (`answer-citations`), every one a passage the model was given. A package that alters a
  timestamp is rejected, the tampered value never reaches the model, and the full context is used.
- Nothing to omit: each of the sixty alarms carries facts found in no other passage, so the Guard could only
  verify the full context and the CIM is bypassed (last test). The package proofs above exercise the CIM
  and the Guard on this evidence with the bypass switched off.
"""

from dataclasses import replace

from sqlalchemy import select

from aiva_gateway.contracts.cim import CimMessage
from aiva_gateway.db.models import ClassificationEventRow, ContextPackageRow, ModelAttemptRow
from aiva_gateway.optimize.cim import SimulatedContextIntelligence
from aiva_gateway.optimize.retrieval import NullRetriever
from aiva_gateway.pipeline import orchestrator
from tests.conftest import console, with_overrides
from tests.scenarios.conftest import PROVIDER, capturing, scenario_fixture

APP = "gct-noc-assistant"


def no_bypass(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The package proofs exercise the CIM and the Guard; this evidence would otherwise bypass the CIM."""
    monkeypatch.setattr(orchestrator, "cim_bypass_reason", lambda *args, **kwargs: None)


ORIGINAL, TAMPERED = "2026-10-14T06:31:00Z", "2026-10-14T06:13:00Z"


class TamperingCim(SimulatedContextIntelligence):
    """Returns a schema-valid package in which one alarm timestamp was rewritten."""

    revision = "tampering-cim (test)"

    async def package(self, request, model):  # type: ignore[no-untyped-def]
        result = await super().package(request, model)
        assert result.response is not None
        messages = [
            CimMessage(role=m.role, content=m.content.replace(ORIGINAL, TAMPERED))
            for m in result.response.upstream_messages
        ]
        return replace(result, response=result.response.model_copy(update={"upstream_messages": messages}))


async def test_alarm_correlation_routes_to_a_frontier_model_with_verified_sources(
    scenario_services, tester_client, db, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    no_bypass(monkeypatch)
    fixture = scenario_fixture("network-operations")
    async with tester_client(svc=scenario_services) as client:
        body = (await client.post("/v1/ai/chat", json={**fixture["request"], **console(APP)})).json()
    assert body["status"] == "completed", body.get("error")
    assert body["routing"]["complexity"] in {"complex", "advanced"}
    assert body["selected_model"] == "claude-flagship"
    assert "quality tier critical requires rank >= 3" in body["routing"]["reasons"]
    classification = (
        await db.execute(
            select(ClassificationEventRow).where(ClassificationEventRow.run_id == body["run_id"])
        )
    ).scalar_one()
    assert classification.complexity == body["routing"]["complexity"]

    assert body["cim"]["sufficiency"] == "verified"
    package = (
        await db.execute(select(ContextPackageRow).where(ContextPackageRow.run_id == body["run_id"]))
    ).scalar_one()
    assert package.context_profile == "network-alarm-correlation"
    assert package.guard_details["failures"] == []
    assert {"verbatim-values", "chronology", "numeric-fidelity", "citations"} <= set(
        package.guard_details["checks"]
    )
    assert body["response"]["provenance"] == PROVIDER
    answer = body["response"]["content"]
    assert "ALM-7790" in answer and "MW-12" in answer
    # The answer names alarms by their own IDs (`ALM-7790`), which cite their passages (`noc/ALM-7790`):
    # every one was sent.
    assert body["assurance"]["answer_citations"] == {
        "status": "resolved",
        "cited": 10,
        "unresolved": 0,
        "unresolved_refs": [],
    }


async def test_a_package_that_alters_a_timestamp_never_reaches_the_model(
    scenario_services, tester_client, db, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    no_bypass(monkeypatch)
    fixture = scenario_fixture("network-operations")
    svc, captured = capturing(with_overrides(scenario_services, cim=TamperingCim()))
    async with tester_client(svc=svc) as client:
        body = (await client.post("/v1/ai/chat", json={**fixture["request"], **console(APP)})).json()
    assert body["status"] == "completed", body.get("error")
    assert body["cim"]["sufficiency"] == "failed-fallback-applied"
    package = (
        await db.execute(select(ContextPackageRow).where(ContextPackageRow.run_id == body["run_id"]))
    ).scalar_one()
    assert any("raised_at" in f for f in package.guard_details["failures"])
    [request] = captured.requests
    payload = "\n".join(m["content"] for m in request.messages)
    assert ORIGINAL in payload and TAMPERED not in payload


async def test_sixty_alarms_with_nothing_to_omit_bypass_the_cim(scenario_services, tester_client) -> None:  # type: ignore[no-untyped-def]
    # Without retrieval: retrieved knowledge-base passages carry no alarm facts, so they could be omitted.
    fixture = scenario_fixture("network-operations")
    svc, captured = capturing(with_overrides(scenario_services, retriever=NullRetriever()))
    async with tester_client(svc=svc) as client:
        body = (await client.post("/v1/ai/chat", json={**fixture["request"], **console(APP)})).json()
    assert body["status"] == "completed", body.get("error")
    assert not body["cim_invoked"] and body["cim"]["sufficiency"] == "not-applicable"
    assert body["cim"]["message"] == "CIM bypassed: nothing to omit; full context used"
    [request] = captured.requests
    payload = "\n".join(m["content"] for m in request.messages)
    assert all(f'id="{e["id"]}"' in payload for e in fixture["request"]["evidence"])  # all sixty, as is


async def test_with_retrieval_the_sixty_alarms_are_too_large_to_package_in_time(
    scenario_services, services, tester_client, db
) -> None:  # type: ignore[no-untyped-def]
    # Retrieval adds knowledge-base passages the CIM could omit, but a verifiable package must still keep all
    # sixty alarms: about 70 s of CIM decoding against a 60 s timeout. The CIM is not called (no timeout,
    # retry and fallback). The upstream call is simulated here: this test is about the CIM stage.
    fixture = scenario_fixture("network-operations")
    svc = with_overrides(scenario_services, adapters=services.adapters)
    async with tester_client(svc=svc) as client:
        body = (
            await client.post("/v1/ai/chat", json={**fixture["request"], **console(APP), "trace": True})
        ).json()
    assert body["status"] == "completed", body.get("error")
    assert not body["cim_invoked"] and body["cim"]["sufficiency"] == "not-applicable"
    assert body["cim"]["message"] == "CIM bypassed: package too large to build in time; full context used"
    spans = {span["name"]: span["attributes"] for span in body["trace"]}
    assert spans["retrieval"]["aiva.retrieval.retrieved"] > 0
    assert spans["cim"]["aiva.cim.bypass_reason"] == "too-large-to-build-in-time"
    stages = (
        await db.execute(select(ModelAttemptRow.stage).where(ModelAttemptRow.run_id == body["run_id"]))
    ).scalars()
    assert "cim" not in set(stages)
