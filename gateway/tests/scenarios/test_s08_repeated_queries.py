"""Scenario 8 — Repeated Queries & Caching (LLD §15).

Primary proof: exact/semantic hit safety, policy versions, avoided inference.

- Exact and semantic hits return before any CIM, reservation or upstream call. The avoided cost is
  modeled from the source run and kept apart from measured usage (LLD §21.4).
- Semantic reuse needs factual equivalence. A different region with an identical embedding is refused.
  The paraphrase avoids the first person: "Tell me…" makes a request personal, which rules out
  semantic reuse. The support chatbot caches per end user, so these runs share one tester.
  The embedding is a constant stand-in here; the pinned bge model is not downloaded in CI.
- Policy versions: the policy bundle hash is part of every key. A newly published version misses, and
  re-publishing the earlier version (a rollback re-pins its hash) hits its entries again.
- Account-specific questions are cached per end user only: one user's answer never reaches another.
"""

from typing import Any

from sqlalchemy import select

from aiva_gateway.auth.principal import RunIdentity
from aiva_gateway.contracts.chat import ChatRequest
from aiva_gateway.core.enums import IdentityMode, SourceMode
from aiva_gateway.db.models import BudgetReservationRow, CacheEventRow, ModelAttemptRow, OptimizationEventRow
from aiva_gateway.pipeline.orchestrator import Orchestrator
from tests.conftest import ADMIN, CONSOLE_IP, SECOND_ADMIN, chat_body, console
from tests.scenarios.conftest import PROVIDER, scenario_fixture, with_cache

APP = "gct-support-chatbot"


def _ask(question: str) -> dict[str, Any]:
    fixture = scenario_fixture("repeated-queries")
    return chat_body(question, **fixture["request"], **console(APP))


async def _attempts(db, run_id: str) -> list[ModelAttemptRow]:  # type: ignore[no-untyped-def]
    return list((await db.execute(select(ModelAttemptRow).where(ModelAttemptRow.run_id == run_id))).scalars())


async def _cache_event(db, run_id: str) -> CacheEventRow:  # type: ignore[no-untyped-def]
    return (await db.execute(select(CacheEventRow).where(CacheEventRow.run_id == run_id))).scalar_one()


async def test_exact_and_semantic_hits_are_safe_and_avoid_inference(
    scenario_services, tester_client, db
) -> None:  # type: ignore[no-untyped-def]
    fixture = scenario_fixture("repeated-queries")
    svc = with_cache(scenario_services)
    async with tester_client(svc=svc) as client:
        first = (await client.post("/v1/ai/chat", json=_ask(fixture["question"]))).json()
        exact = (await client.post("/v1/ai/chat", json=_ask(fixture["question"]))).json()
        similar = (await client.post("/v1/ai/chat", json=_ask(fixture["paraphrase"]))).json()
        other = (await client.post("/v1/ai/chat", json=_ask(fixture["other_region"]))).json()

    assert first["status"] == "completed" and first["cache"]["upstream_call"]
    assert first["selected_model"] == "deepseek-v4-1-flash" and first["response"]["provenance"] == PROVIDER
    for hit, kind in ((exact, "exact"), (similar, "semantic")):
        assert hit["execution_path"] == "cache-return" and hit["cache"]["cache_type"] == kind
        assert hit["cache"]["upstream_call"] is False
        assert hit["cim"]["message"] == "Full response-cache hit: no upstream inference call"
        assert hit["response"]["content"] == first["response"]["content"]
        assert hit["response"]["provenance"] == PROVIDER  # never upgraded to measured
        # Avoided inference: no attempt, no reservation, no fictitious submitted tokens.
        assert await _attempts(db, hit["run_id"]) == []
        assert (
            hit["budget"]["reservation_id"] is None and hit["usage"]["upstream_submitted_input_tokens"] == 0
        )
        assert (await _cache_event(db, hit["run_id"])).source_run_id == first["run_id"]
        optimization = (
            await db.execute(select(OptimizationEventRow).where(OptimizationEventRow.run_id == hit["run_id"]))
        ).scalar_one()
        assert optimization.avoided_call
        assert optimization.modeled_avoided_micros == first["cost"]["upstream_micros"] > 0
    # Identical embedding, different region: not factually equivalent, so the question is answered anew.
    assert other["cache"]["hit"] is False and other["cache"]["upstream_call"]
    assert other["response"]["content"] != first["response"]["content"]
    assert len(await _attempts(db, other["run_id"])) >= 1


async def test_a_new_policy_version_never_serves_answers_cached_under_another(
    scenario_services, tester_client, make_client, user_token, policy_versions, db
) -> None:  # type: ignore[no-untyped-def]
    fixture = scenario_fixture("repeated-queries")
    svc = with_cache(scenario_services)
    question = _ask(fixture["question"])
    async with tester_client(svc=svc) as client:
        first = (await client.post("/v1/ai/chat", json=question)).json()
        assert (await client.post("/v1/ai/chat", json=question)).json()["cache"]["hit"]
    start = first["policy"]["policy_version"]

    # A restricting policy for another application: support traffic is unaffected, but the bundle changed.
    await policy_versions(
        [
            {
                "policy_id": "GCT-TEST-CACHE-001",
                "title": "Scenario 8: output DLP for the coding assistant",
                "priority": 400,
                "match": {"application_any": ["gct-coding-assistant"]},
                "enforce": {"output_dlp": "required"},
            }
        ]
    )
    async with tester_client(svc=svc) as client:
        after = (await client.post("/v1/ai/chat", json=question)).json()
    assert after["policy"]["policy_version"] != start
    assert after["cache"]["hit"] is False and after["cache"]["upstream_call"]
    assert (await _cache_event(db, after["run_id"])).key_hash != (
        await _cache_event(db, first["run_id"])
    ).key_hash

    # Rolling back re-pins the earlier bundle hash, so its entries are valid again.
    async with make_client(user_token(SECOND_ADMIN if ADMIN else ADMIN)) as admin:
        rolled = await admin.post(
            "/v1/admin/policies/publish", json={"version": start, "approval_note": "scenario 8 rollback"}
        )
    assert rolled.status_code == 200, rolled.json()
    async with tester_client(svc=svc) as client:
        again = (await client.post("/v1/ai/chat", json=question)).json()
    assert again["policy"]["policy_version"] == start
    assert (
        again["cache"]["hit"] and (await _cache_event(db, again["run_id"])).source_run_id == first["run_id"]
    )


def _user(pseudonym: str) -> RunIdentity:
    return RunIdentity(
        application_id=APP,
        identity_mode=IdentityMode.SIMULATED,
        source_ip=CONSOLE_IP,
        source_mode=SourceMode.SIMULATED,
        principal_kind="user",
        principal_pseudonym=pseudonym,
        roles=("tester",),
    )


async def test_account_specific_answers_never_cross_users(scenario_services) -> None:  # type: ignore[no-untyped-def]
    question = scenario_fixture("repeated-queries")["account_specific"]
    svc = with_cache(scenario_services)

    async def ask(pseudonym: str):  # type: ignore[no-untyped-def]
        orchestrator = Orchestrator(svc)
        request = ChatRequest.model_validate(chat_body(question, scenario_id="repeated-queries"))
        ctx = orchestrator.new_context(request, _user(pseudonym), "gct")
        await orchestrator.run(ctx)
        return ctx

    alice_first, alice_again, bob = (
        await ask("pu-scenario-a"),
        await ask("pu-scenario-a"),
        await ask("pu-scenario-b"),
    )
    assert alice_first.cache_checked and alice_again.path.value == "cache-return"
    assert (
        alice_again.cache_hit_entry is not None
        and alice_again.cache_hit_entry.source_run_id == alice_first.run_id
    )
    # Same question, another user: a different cache scope, so a miss and a fresh governed run.
    assert bob.path.value != "cache-return" and bob.cache_scope != alice_first.cache_scope
    assert [a.stage.value for a in bob.attempts if a.stage.value == "upstream"] == ["upstream"]


async def test_cache_hits_hold_no_budget(scenario_services, tester_client, db) -> None:  # type: ignore[no-untyped-def]
    fixture = scenario_fixture("repeated-queries")
    svc = with_cache(scenario_services)
    async with tester_client(svc=svc) as client:
        first = (await client.post("/v1/ai/chat", json=_ask(fixture["question"]))).json()
        hit = (await client.post("/v1/ai/chat", json=_ask(fixture["question"]))).json()
    assert hit["cache"]["hit"]
    reservations = (
        (
            await db.execute(
                select(BudgetReservationRow).where(
                    BudgetReservationRow.run_id.in_([first["run_id"], hit["run_id"]])
                )
            )
        )
        .scalars()
        .all()
    )
    assert [r.run_id for r in reservations] == [first["run_id"]]
