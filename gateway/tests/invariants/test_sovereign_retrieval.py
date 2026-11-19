"""LLD Appendix B with knowledge-base retrieval on the sovereign-direct path (ADR-0020): confidential,
restricted and sovereign-only payloads that carry retrieved passages still never reach the CIM, the baseline
or an external model, and the sovereign path still never falls back."""

import pytest
from sqlalchemy import select

from aiva_gateway.db.models import ModelAttemptRow, TraceSpanRow
from aiva_gateway.inference.registry import AdapterRegistry
from aiva_gateway.inference.simulated import SimulatedInferenceAdapter
from tests.conftest import with_overrides
from tests.integration.test_context_sufficiency import CapturingAdapter

# `kb` is the integration tests' fixture: a seeded knowledge base and a retriever over it.
from tests.integration.test_sovereign_retrieval import TEST_FLOOR, kb, s1, sovereign  # noqa: F401

EXTERNAL = {"gemini-flash-lite", "deepseek-v4-1-flash", "claude-flagship"}
SENSITIVE = ["confidential", "restricted", "sovereign-only"]


async def _attempts(db, run_id: str) -> list[ModelAttemptRow]:  # type: ignore[no-untyped-def]
    rows = await db.execute(select(ModelAttemptRow).where(ModelAttemptRow.run_id == run_id))
    return sorted(rows.scalars().all(), key=lambda a: a.sequence)


@pytest.mark.parametrize("classification", SENSITIVE)
async def test_retrieved_passages_reach_only_the_sovereign_model(
    services,
    kb,  # noqa: F811 (the imported fixture)
    tester_client,
    db,
    classification,
) -> None:  # type: ignore[no-untyped-def]
    adapter = CapturingAdapter()
    payload = s1(data_classification=classification, baseline_comparison=True)
    async with tester_client(svc=sovereign(services, kb, adapter, TEST_FLOOR)) as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["status"] == "completed", body.get("error")
    assert body["execution_path"] == "sovereign-direct" and body["cim_invoked"] is False
    [sent] = adapter.payloads  # one call: the sovereign model's, with the retrieved passage
    assert 'id="KB-TARIFF-002#0"' in sent
    attempts = await _attempts(db, body["run_id"])
    assert [(a.stage, a.model_id) for a in attempts] == [("upstream", "gct-qwen-finetuned")]
    assert not {a.model_id for a in attempts} & EXTERNAL
    assert body["cost"]["baseline_micros"] == 0
    baseline = (
        await db.execute(
            select(TraceSpanRow.attributes["aiva.baseline.status"].astext).where(
                TraceSpanRow.run_id == body["run_id"],
                TraceSpanRow.attributes.has_key("aiva.baseline.status"),
            )
        )
    ).scalar_one()
    assert baseline == "denied-by-policy"


@pytest.mark.parametrize("classification", SENSITIVE)
async def test_retrieval_never_turns_a_sovereign_outage_into_a_fallback(
    services,
    kb,  # noqa: F811 (the imported fixture)
    tester_client,
    db,
    classification,
) -> None:  # type: ignore[no-untyped-def]
    down = SimulatedInferenceAdapter(unavailable_models={"gct-qwen-finetuned"})
    svc = with_overrides(
        sovereign(services, kb, CapturingAdapter(), TEST_FLOOR),
        adapters=AdapterRegistry(services.catalog, adapters={}, default=down),
    )
    async with tester_client(svc=svc) as client:
        response = await client.post("/v1/ai/chat", json=s1(data_classification=classification))
    body = response.json()
    assert response.status_code == 503 and body["error"]["code"] == "SOVEREIGN_ENDPOINT_UNAVAILABLE"
    attempts = await _attempts(db, body["run_id"])
    assert [a.model_id for a in attempts] == ["gct-qwen-finetuned", "gct-qwen-finetuned"]  # one bounded retry
    retrieval = (
        await db.execute(
            select(TraceSpanRow.attributes).where(
                TraceSpanRow.run_id == body["run_id"], TraceSpanRow.name == "retrieval"
            )
        )
    ).scalar_one()
    assert retrieval["aiva.retrieval.retrieved"] == 1  # the passage was retrieved, and still no fallback
