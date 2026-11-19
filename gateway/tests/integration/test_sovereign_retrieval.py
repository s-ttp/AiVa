"""Knowledge-base retrieval on the sovereign-direct path (ADR-0020): only for a context profile that opts in,
only when the caller supplied no evidence and RLCD does not confidently predict the context is sufficient,
only passages at least as similar to the question as the profile's floor, and only for the sovereign model."""

from collections.abc import AsyncIterator
from dataclasses import replace

import pytest
from sqlalchemy import select

from aiva_gateway.db.models import ModelAttemptRow, TraceSpanRow
from aiva_gateway.inference.registry import AdapterRegistry
from aiva_gateway.optimize.embeddings import HashingEmbedder
from aiva_gateway.optimize.knowledge import ingest_documents, load_jsonl
from aiva_gateway.optimize.retrieval import HybridRetriever
from aiva_gateway.optimize.rlcd import HeuristicRouter
from aiva_gateway.pipeline.interfaces import InitialAssessment, RouterRequest
from aiva_gateway.pipeline.orchestrator import (
    CIM_BYPASS_MESSAGE,
    SOVEREIGN_RETRIEVAL_SUFFICIENT_MESSAGE,
    SOVEREIGN_RETRIEVAL_SUPPLIED_MESSAGE,
)
from aiva_gateway.services import Services
from tests.conftest import chat_body, console, with_overrides
from tests.integration.test_context_sufficiency import CapturingAdapter
from tests.integration.test_retrieval import DETECTOR, SEED, _clear

# Scenario 1's question. The seed KB's KB-TARIFF-002 (granted to the support chatbot) explains the 15 AED
# fee; KB-FIN-004 is confidential and KB-CASE-006's customer example carries an identifier: neither is
# retrievable.
S1 = "Why did my synthetic GCT Prepaid Plus bill increase by 15 AED this month?"
# The tests' hashing embedder scores KB-TARIFF-002 at about 0.38 for S1 and every other chunk under 0.16, so
# this floor keeps exactly that passage; the configured 0.62 (calibrated for the pinned bge-base) keeps none.
TEST_FLOOR = 0.25


@pytest.fixture
async def kb(services: Services) -> AsyncIterator[HybridRetriever]:
    await _clear(services)
    async with services.sessionmaker() as session, session.begin():
        await ingest_documents(
            session, tenant_id="gct", documents=load_jsonl(SEED), embedder=HashingEmbedder(), dlp=services.dlp
        )
    yield HybridRetriever(
        services.sessionmaker, embedder=HashingEmbedder(), reranker=None, injection_detector=DETECTOR
    )
    await _clear(services)


def sovereign(
    services: Services, retriever: HybridRetriever, adapter: CapturingAdapter, floor: float | None = None
) -> Services:
    catalog = services.catalog
    if floor is not None:
        profiles = [
            p.model_copy(update={"sovereign_retrieval_min_cosine": floor})
            if p.id == "customer-support"
            else p
            for p in catalog.context_profiles
        ]
        catalog = replace(catalog, context_profiles=profiles)
    return with_overrides(
        services,
        catalog=catalog,
        retriever=retriever,
        adapters=AdapterRegistry(catalog, adapters={}, default=adapter),
    )


def s1(**extra: object) -> dict[str, object]:
    extra.setdefault("data_classification", "confidential")
    return chat_body(S1, scenario_id="customer-support", **extra, **console("gct-support-chatbot"))


async def spans(db, run_id: str) -> dict[str, dict[str, object]]:  # type: ignore[no-untyped-def]
    rows = (await db.execute(select(TraceSpanRow).where(TraceSpanRow.run_id == run_id))).scalars()
    return {row.name: row.attributes for row in rows}


async def test_a_confidential_question_without_evidence_gets_kb_passages_for_the_sovereign_model(
    services, kb, tester_client, db
) -> None:  # type: ignore[no-untyped-def]
    adapter = CapturingAdapter()
    async with tester_client(svc=sovereign(services, kb, adapter, TEST_FLOOR)) as client:
        body = (await client.post("/v1/ai/chat", json=s1())).json()
    assert body["status"] == "completed", body.get("error")
    assert body["execution_path"] == "sovereign-direct" and body["selected_model"] == "gct-qwen-finetuned"
    assert body["cim_invoked"] is False and body["cim"]["message"] == CIM_BYPASS_MESSAGE
    cim = body["cim"]
    assert (cim["passages_retrieved"], cim["passages_kept"], cim["passages_omitted"]) == (1, None, None)
    # The retrieved passage counts in the context before as well as after: the full context, nothing saved.
    usage = body["usage"]
    assert usage["original_context_tokens"] == usage["constructed_context_tokens"] > 100
    [payload] = adapter.payloads
    assert 'id="KB-TARIFF-002#0"' in payload and "15 AED digital services fee" in payload
    for never in ("KB-FIN-004", "+971", "KB-NOC-007"):  # confidential, an identifier, another application's
        assert never not in payload
    retrieval = (await spans(db, body["run_id"]))["retrieval"]
    assert retrieval["aiva.stage.status"] == "executed"
    assert retrieval["aiva.retrieval.path"] == "sovereign-direct"
    assert (retrieval["aiva.retrieval.supplied"], retrieval["aiva.retrieval.retrieved"]) == (0, 1)
    assert retrieval["aiva.retrieval.below_floor"] >= 1  # the other fused candidates were held back
    assert all(k.startswith("aiva.") for k in retrieval)
    attempts = (
        await db.execute(select(ModelAttemptRow).where(ModelAttemptRow.run_id == body["run_id"]))
    ).scalars()
    assert [(a.stage, a.model_id) for a in attempts] == [("upstream", "gct-qwen-finetuned")]


async def test_no_passage_under_the_floor_is_added(services, kb, tester_client, db) -> None:  # type: ignore[no-untyped-def]
    # The configured floor, 0.62: nothing the test embedder scores clears it, so nothing is added.
    adapter = CapturingAdapter()
    async with tester_client(svc=sovereign(services, kb, adapter)) as client:
        body = (await client.post("/v1/ai/chat", json=s1())).json()
    assert body["status"] == "completed", body.get("error")
    [payload] = adapter.payloads
    assert "<evidence" not in payload
    retrieval = (await spans(db, body["run_id"]))["retrieval"]
    assert retrieval["aiva.retrieval.retrieved"] == 0 and retrieval["aiva.retrieval.below_floor"] >= 1


async def test_supplied_evidence_is_not_topped_up_from_the_kb(services, kb, tester_client, db) -> None:  # type: ignore[no-untyped-def]
    # Scenario 1's default chat: it supplies its passage, so its sovereign body is unchanged.
    adapter = CapturingAdapter()
    evidence = [{"id": "KB-TARIFF-020#0", "text": "[KB-TARIFF-020 §1] Add-ons - Streaming\nA 15 AED fee."}]
    async with tester_client(svc=sovereign(services, kb, adapter, TEST_FLOOR)) as client:
        body = (await client.post("/v1/ai/chat", json=s1(evidence=evidence))).json()
    assert body["status"] == "completed", body.get("error")
    [payload] = adapter.payloads
    assert "KB-TARIFF-020#0" in payload and "KB-TARIFF-002" not in payload
    retrieval = (await spans(db, body["run_id"]))["retrieval"]
    assert retrieval["aiva.stage.status"] == "skipped"
    assert retrieval["aiva.stage.skip_reason"] == SOVEREIGN_RETRIEVAL_SUPPLIED_MESSAGE
    assert body["cim"]["passages_retrieved"] is None  # no retrieval ran


class ConfidentlySufficient(HeuristicRouter):
    """RLCD's classifier predicting `context_need: sufficient` above its confidence threshold."""

    def assess(self, request: RouterRequest) -> InitialAssessment:
        assessment = super().assess(request)
        return replace(
            assessment, context_sufficient=True, confidences={"context_need": 0.97}, unclassified=()
        )


async def test_rlcd_confidently_sufficient_skips_the_search(services, kb, tester_client, db) -> None:  # type: ignore[no-untyped-def]
    adapter = CapturingAdapter()
    svc = with_overrides(sovereign(services, kb, adapter, TEST_FLOOR), router=ConfidentlySufficient())
    async with tester_client(svc=svc) as client:
        body = (await client.post("/v1/ai/chat", json=s1())).json()
    assert body["status"] == "completed", body.get("error")
    [payload] = adapter.payloads
    assert "<evidence" not in payload
    retrieval = (await spans(db, body["run_id"]))["retrieval"]
    assert retrieval["aiva.stage.skip_reason"] == SOVEREIGN_RETRIEVAL_SUFFICIENT_MESSAGE


async def test_a_profile_that_does_not_opt_in_never_searches(services, kb, tester_client, db) -> None:  # type: ignore[no-untyped-def]
    # Fraud investigation (sovereign_retrieval_k: 0): the sovereign path is exactly as before ADR-0020.
    assert services.catalog.context_profile("fraud-investigation").sovereign_retrieval_k == 0
    adapter = CapturingAdapter()
    payload = chat_body(
        "Investigate synthetic SIM-swap alert FR-2026-0412",
        scenario_id="fraud-management",
        data_classification="confidential",
        **console("gct-fraud-investigator"),
    )
    async with tester_client(svc=sovereign(services, kb, adapter, TEST_FLOOR)) as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["status"] == "completed", body.get("error")
    assert "<evidence" not in adapter.payloads[0]
    retrieval = (await spans(db, body["run_id"]))["retrieval"]
    assert (retrieval["aiva.stage.status"], retrieval["aiva.stage.skip_reason"]) == (
        "skipped",
        CIM_BYPASS_MESSAGE,
    )


def test_only_customer_support_opts_in_and_by_default_nothing_does() -> None:
    from aiva_gateway.catalog import load_catalog
    from aiva_gateway.catalog.models import ContextProfile
    from aiva_gateway.config import REPO_ROOT

    catalog = load_catalog(REPO_ROOT / "config")
    opted = {
        p.id: (p.sovereign_retrieval_k, p.sovereign_retrieval_min_cosine) for p in catalog.context_profiles
    }
    assert {k: v for k, v in opted.items() if v[0]} == {"customer-support": (3, 0.62)}
    assert ContextProfile(id="x", required_fields=[], target_budget_tokens=100).sovereign_retrieval_k == 0
    assert catalog.scenario("customer-support").context_profile == "customer-support"  # type: ignore[union-attr]
