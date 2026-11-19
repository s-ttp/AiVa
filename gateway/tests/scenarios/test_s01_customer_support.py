"""Scenario 1 — Customer Support Chatbot (LLD §15).

Primary proof: long history, tariff RAG, an unverified prune never served, cache and savings.

- Tariff RAG: retrieval brings the tariff passage that explains the bill increase (application-granted,
  internal) into the context.
- Context pruning: the CIM keeps that passage and the latest turns, and omits the rest. The CI stand-in
  is a deterministic, `simulated` pruning CIM. The omitted passages are knowledge passages, whose relevance
  the Context Guard cannot verify, so the prune is never served (ADR-0009, 2026-10-22 amendment): the full
  context, every turn and passage, reaches the model, and the accounting shows no reduction.
- Savings: the answer runs on a cheaper model (replayed) and the pinned frontier baseline runs as a
  separate, policy-checked charge. A repeat of the request is a full cache hit with no upstream call,
  and its avoided cost is modeled from the source run, never counted as measured usage.
"""

from sqlalchemy import select

from aiva_gateway.db.models import (
    CacheEventRow,
    ContextPackageRow,
    ModelAttemptRow,
    OptimizationEventRow,
    UsageLedgerRow,
)
from tests.conftest import console, with_overrides
from tests.scenarios.conftest import PROVIDER, PruningCim, capturing, scenario_fixture, with_cache

APP = "gct-support-chatbot"


async def test_an_unverified_prune_sends_the_full_context_answered_from_the_tariff_kb_then_cached(
    scenario_services, tester_client, db
) -> None:  # type: ignore[no-untyped-def]
    fixture = scenario_fixture("customer-support")
    # With its title, the add-on passage shares five terms with the question (bill, month, plus, prepaid,
    # synthetic); the Prepaid Plus price lines share four and are pruned.
    cim = PruningCim(min_overlap=5)
    svc, captured = capturing(with_cache(with_overrides(scenario_services, cim=cim)))
    body = {**fixture["request"], **console(APP)}
    async with tester_client(svc=svc) as client:
        first = (await client.post("/v1/ai/chat", json=body)).json()
        repeat = (await client.post("/v1/ai/chat", json=body)).json()

    assert first["status"] == "completed", first.get("error")
    assert first["execution_path"] == "cim-assisted" and first["cim_invoked"]

    # Tariff RAG: the explaining passage was retrieved and kept; unrelated passages were pruned.
    package = (
        await db.execute(select(ContextPackageRow).where(ContextPackageRow.run_id == first["run_id"]))
    ).scalar_one()
    expected = fixture["expect"]["retrieved_doc"]
    assert any(i.startswith(expected) for i in package.retained_ids)
    assert package.removed_ids and not any(i.startswith(expected) for i in package.removed_ids)
    assert package.guard_details["failures"] == []
    # The Guard checked the package, but not the relevance of the passages the CIM pruned: such a prune is
    # never served. It is a failed check, and the full context is sent instead.
    assert first["cim"]["sufficiency"] == package.validation == "failed-fallback-applied"
    assert first["cim"]["message"] == "Context sufficiency check failed; safe fallback applied"
    assert package.guard_details["sufficiency_basis"].startswith("relevance-unverified")

    # The full context: all 19 turns and every retrieved passage, so no tokens are saved.
    upstream = next(r for r in captured.requests if r.model.id == first["selected_model"])
    assert [m["content"] for m in upstream.messages[1:]] == [
        m["content"] for m in fixture["request"]["messages"]
    ]
    assert all(f'id="{i}"' in upstream.messages[0]["content"] for i in package.evidence_refs)
    usage = first["usage"]
    assert usage["constructed_context_tokens"] == usage["original_context_tokens"]
    optimization = (
        await db.execute(select(OptimizationEventRow).where(OptimizationEventRow.run_id == first["run_id"]))
    ).scalar_one()
    assert optimization.removed_passages_count == len(package.removed_ids) > 0  # what the CIM proposed
    assert optimization.context_tokens_after == optimization.context_tokens_before

    # Savings: the answer and the pinned baseline are separate charges on replayed provider usage.
    assert first["selected_model"] == "deepseek-v4-1-flash"
    assert first["response"]["provenance"] == PROVIDER
    attempts = (
        (await db.execute(select(ModelAttemptRow).where(ModelAttemptRow.run_id == first["run_id"])))
        .scalars()
        .all()
    )
    assert sorted((a.stage, a.model_id) for a in attempts) == [
        ("baseline", "claude-flagship"),
        ("cim", "enterprise-cim"),
        ("upstream", "deepseek-v4-1-flash"),
    ]
    # Tokens by where they were served: the CIM, the answering model and the baseline, each on its own line.
    by_model = {(u["stage"], u["model_id"]): u for u in first["usage"]["by_model"]}
    assert set(by_model) == {(a.stage, a.model_id) for a in attempts}
    for a in attempts:
        usage = by_model[(a.stage, a.model_id)]
        assert (usage["input_tokens"], usage["output_tokens"]) == (a.input_tokens, a.output_tokens)
    assert by_model[("upstream", "deepseek-v4-1-flash")]["provenance"] == PROVIDER
    cost = first["cost"]
    assert 0 < cost["upstream_micros"] < cost["baseline_micros"]
    assert cost["total_api_micros"] == cost["upstream_micros"] + cost["cim_micros"] + cost["baseline_micros"]
    charges = (
        (await db.execute(select(UsageLedgerRow).where(UsageLedgerRow.run_id == first["run_id"])))
        .scalars()
        .all()
    )
    assert {c.charge_type for c in charges} >= {"upstream_inference", "baseline_comparison"}

    # Cache: the repeat is answered without any upstream call; avoided cost is modeled, not measured.
    assert repeat["execution_path"] == "cache-return"
    assert repeat["cache"] == {
        "checked": True,
        "hit": True,
        "cache_type": "exact",
        "upstream_call": False,
        # The tokens the repeat did not spend: the source run's upstream usage, labelled no better than its
        # replayed answer.
        "avoided_input_tokens": first["usage"]["upstream_submitted_input_tokens"],
        "avoided_output_tokens": first["usage"]["upstream_output_tokens"],
        "avoided_provenance": "replayed",
    }
    assert repeat["response"]["content"] == first["response"]["content"]
    assert repeat["response"]["provenance"] == PROVIDER  # keeps the source run's provenance
    assert repeat["usage"]["upstream_submitted_input_tokens"] == 0 and repeat["cost"]["total_api_micros"] == 0
    hit = (
        await db.execute(select(OptimizationEventRow).where(OptimizationEventRow.run_id == repeat["run_id"]))
    ).scalar_one()
    assert hit.avoided_call and hit.modeled_avoided_micros == cost["upstream_micros"]
    event = (
        await db.execute(select(CacheEventRow).where(CacheEventRow.run_id == repeat["run_id"]))
    ).scalar_one()
    assert event.source_run_id == first["run_id"]
    assert (
        await db.execute(select(ModelAttemptRow).where(ModelAttemptRow.run_id == repeat["run_id"]))
    ).first() is None
