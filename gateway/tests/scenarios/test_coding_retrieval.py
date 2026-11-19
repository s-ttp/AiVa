"""Coding requests do not search the knowledge base (`context_profiles.yaml` `retrieval_skip_tasks`).

The synthetic KB holds tariffs and policies, no technical material. Scenario 16's coding questions each drew
8 unrelated passages on the CIM-assisted path, sent them to the frontier model, and the answer said on stage
that none applied (2026-10-25). A coding run now skips the retrieval stage: no knowledge-base or web passages,
the request's own evidence only. The CIM is still bypassed for code (`cim_trained_tasks`), with the same
message.
Inference is simulated: these runs are about which context reaches the model.
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest

from aiva_gateway.catalog import load_catalog
from aiva_gateway.config import REPO_ROOT, Settings
from aiva_gateway.optimize.cim_live import TASK_OUTSIDE_TRAINING
from aiva_gateway.pipeline.orchestrator import CIM_BYPASS_MESSAGES
from aiva_gateway.services import Services, build_services
from tests.conftest import console
from tests.scenarios.conftest import capturing

CATALOG = load_catalog(REPO_ROOT / "config")
SKIPPED = "Knowledge base not searched: coding requests do not use it"


@pytest.fixture
async def simulated(settings: Settings, scenario_kb: None) -> AsyncIterator[Services]:
    svc = build_services(
        settings.model_copy(update={"inference_mode": "simulated", "response_cache_enabled": False})
    )
    yield svc
    await svc.aclose()


def _body(scenario_id: str, question: str | None = None, **extra: Any) -> dict[str, Any]:
    scenario = CATALOG.scenario(scenario_id)
    assert scenario is not None
    return {
        "messages": [{"role": "user", "content": question or scenario.sample_prompt}],
        "routing": {"mode": "hybrid", "tags": scenario.routing_tags},
        "data_classification": scenario.declared_classification,
        "quality_tier": scenario.quality_tier,
        "max_output_tokens": scenario.max_output_tokens,
        "scenario_id": scenario.id,
        "trace": True,
        **console(scenario.application_id),
        **extra,
    }


def _retrieval_stage(body: dict[str, Any]) -> dict[str, Any]:
    spans = [s.get("attributes", {}) for s in body["trace"]]
    return next(a for a in spans if a.get("aiva.stage") == "retrieval")


def _coding_questions() -> list[tuple[str, str]]:
    s16 = CATALOG.scenario("live-code-generation")
    assert s16 is not None and s16.sample_prompt
    questions = [("coding-assistant", None), ("live-code-generation", None)]
    questions += [("live-code-generation", q) for q in s16.alternate_prompts]
    return questions  # type: ignore[return-value]


@pytest.mark.parametrize(("scenario_id", "question"), _coding_questions())
async def test_a_coding_request_searches_no_knowledge_base(
    simulated, tester_client, scenario_id, question
) -> None:  # type: ignore[no-untyped-def]
    svc, captured = capturing(simulated)
    async with tester_client(svc=svc) as client:
        body = (await client.post("/v1/ai/chat", json=_body(scenario_id, question))).json()
    assert body["status"] == "completed", body.get("error")
    assert body["execution_path"] == "cim-assisted" and not body["cim_invoked"]
    assert body["cim"]["message"] == CIM_BYPASS_MESSAGES[TASK_OUTSIDE_TRAINING]  # unchanged
    assert body["cim"]["passages_retrieved"] is None  # no retrieval ran
    stage = _retrieval_stage(body)
    assert (stage["aiva.stage.status"], stage["aiva.stage.skip_reason"]) == ("skipped", SKIPPED)
    assert body["selected_model"] == "claude-flagship"
    [request] = captured.requests
    assert not any("<evidence" in m["content"] for m in request.messages)


async def test_a_coding_request_keeps_the_evidence_it_supplies(simulated, tester_client) -> None:  # type: ignore[no-untyped-def]
    evidence = [{"id": "spec-1", "text": "CDR columns: record_id, subscriber_ref, bytes_used, roaming"}]
    svc, captured = capturing(simulated)
    async with tester_client(svc=svc) as client:
        body = (await client.post("/v1/ai/chat", json=_body("coding-assistant", evidence=evidence))).json()
    assert body["status"] == "completed", body.get("error")
    assert _retrieval_stage(body)["aiva.stage.status"] == "skipped"
    [request] = captured.requests
    sent = "\n".join(m["content"] for m in request.messages)
    assert 'id="spec-1"' in sent and "<evidence id=" in sent and sent.count("<evidence id=") == 1


async def test_a_rag_request_still_searches_the_knowledge_base(simulated, tester_client) -> None:  # type: ignore[no-untyped-def]
    async with tester_client(svc=simulated) as client:
        body = (await client.post("/v1/ai/chat", json=_body("enterprise-rag"))).json()
    assert body["status"] == "completed", body.get("error")
    assert body["cim"]["passages_retrieved"] > 0
    assert _retrieval_stage(body).get("aiva.stage.status") != "skipped"
