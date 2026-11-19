"""Assurance check `answer-citations` through POST /v1/ai/chat (ADR-0009).

The answer is checked against the payload the model was actually given: a citation of a passage the CIM
omitted, or of one never retrieved, is reported as unresolved, and the answer is still delivered. Runs with
no upstream payload to check against (a full response-cache return, a blocked run) report `not_checked`.
"""

from dataclasses import replace

from sqlalchemy import select

from aiva_gateway.db.models import AssuranceResultRow, TraceSpanRow
from aiva_gateway.inference.registry import AdapterRegistry
from aiva_gateway.inference.simulated import SimulatedInferenceAdapter
from aiva_gateway.pipeline.interfaces import InferenceRequest, InferenceResult
from tests.conftest import chat_body, console, with_overrides
from tests.integration.test_context_sufficiency import ALARMS, ScriptedCim, package
from tests.integration.test_response_cache import GENERIC, SHARED_APP, cached_services

MADE_UP = "[GCT-POL-999 §9]"
# Cites both kept alarms, the omitted canteen note and a document nobody retrieved.
ANSWER = f"alarm-1 and alarm-2 share a backhaul fault; note-1 is unrelated. See {MADE_UP}."


class AnsweringAdapter(SimulatedInferenceAdapter):
    async def complete(self, request: InferenceRequest) -> InferenceResult:
        return replace(await super().complete(request), content=ANSWER)


async def test_a_citation_the_package_omitted_is_reported_and_the_answer_delivered(
    services, tester_client, db
) -> None:  # type: ignore[no-untyped-def]
    svc = with_overrides(
        services,
        cim=ScriptedCim(package(["alarm-1", "alarm-2"])),  # omits note-1
        adapters=AdapterRegistry(services.catalog, adapters={}, default=AnsweringAdapter()),
    )
    payload = chat_body(
        "Correlate the alarms.",
        scenario_id="network-operations",
        evidence=[{"id": k, "text": v} for k, v in ALARMS.items()],
        **console("gct-noc-assistant"),
    )
    async with tester_client(svc=svc) as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["status"] == "completed" and body["cim"]["sufficiency"] == "verified"
    assert body["response"]["content"] == ANSWER  # reported, never blocked or rewritten
    assert body["assurance"]["passed"] and "answer-citations" in body["assurance"]["checks"]
    assert body["assurance"]["answer_citations"] == {
        "status": "unresolved",
        "cited": 4,
        "unresolved": 2,
        "unresolved_refs": [MADE_UP, "note-1"],
    }

    [result] = (
        await db.execute(select(AssuranceResultRow).where(AssuranceResultRow.run_id == body["run_id"]))
    ).scalars()
    assert result.evidence_coverage == 0.5  # resolved ÷ cited
    spans = (await db.execute(select(TraceSpanRow).where(TraceSpanRow.run_id == body["run_id"]))).scalars()
    [span] = [s for s in spans if s.name == "assurance"]
    assert span.attributes["aiva.assurance.unresolved_citations"] == 2  # the count only
    assert not any("note-1" in str(value) or "GCT-POL" in str(value) for value in span.attributes.values())


async def test_a_response_cache_return_is_not_checked(services, app_client, db) -> None:  # type: ignore[no-untyped-def]
    async with app_client(SHARED_APP, svc=cached_services(services)) as client:
        first = (await client.post("/v1/ai/chat", json=chat_body(GENERIC))).json()
        second = (await client.post("/v1/ai/chat", json=chat_body(GENERIC))).json()
    assert first["assurance"]["answer_citations"]["status"] != "not_checked"
    assert second["execution_path"] == "cache-return"
    assert second["assurance"]["answer_citations"] == {
        "status": "not_checked",
        "cited": 0,
        "unresolved": 0,
        "unresolved_refs": [],
    }
    rows = (
        await db.execute(select(AssuranceResultRow).where(AssuranceResultRow.run_id == second["run_id"]))
    ).scalars()
    assert all(row.evidence_coverage is None for row in rows)


async def test_a_blocked_run_is_not_checked(app_client) -> None:  # type: ignore[no-untyped-def]
    async with app_client("gct-support-chatbot", peer="203.0.113.9") as client:  # untrusted network
        body = (await client.post("/v1/ai/chat", json=chat_body("hello"))).json()
    assert body["status"] == "blocked"
    assert body["assurance"]["answer_citations"]["status"] == "not_checked"
