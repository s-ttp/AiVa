"""Exact and semantic response caching through POST /v1/ai/chat (LLD §6 steps 4-5, §11, §21.4)."""

import uuid
from collections.abc import Sequence

import numpy as np
from redis.asyncio import Redis
from sqlalchemy import select

from aiva_gateway.db.models import CacheEventRow, ModelAttemptRow, OptimizationEventRow, TraceSpanRow
from aiva_gateway.optimize.cache import RedisResponseCache
from aiva_gateway.optimize.embeddings import EMBEDDING_DIMENSIONS, Vector
from tests.conftest import chat_body, console, with_overrides
from tests.integration.test_public_demo import start_session

GENERIC = "What are the synthetic roaming rates for Europe on the Traveller 10 bundle?"
SHARED_APP = "gct-knowledge-portal"  # default `cache_sharing: shared`; the support chatbot is per_user


class ConstantEmbedder:
    """Every text is maximally similar: isolates the factual-equivalence guard from the threshold."""

    model_id = "constant-test"
    dimensions = EMBEDDING_DIMENSIONS

    def embed(self, texts: Sequence[str], *, query: bool = False) -> Vector:
        rows = np.zeros((len(texts), self.dimensions), dtype=np.float32)
        rows[:, 0] = 1.0
        return rows


def cached_services(services, *, redis: Redis | None = None):  # type: ignore[no-untyped-def]
    cache = RedisResponseCache(
        redis or services.redis,
        embedder=ConstantEmbedder(),
        semantic_threshold=0.95,
        namespace=f"aiva:test:{uuid.uuid4().hex}",
    )
    return with_overrides(services, cache=cache)


async def _attempts(db, run_id: str) -> list[ModelAttemptRow]:  # type: ignore[no-untyped-def]
    return list((await db.execute(select(ModelAttemptRow).where(ModelAttemptRow.run_id == run_id))).scalars())


async def test_exact_hit_returns_before_inference_and_keeps_source_provenance(
    services, app_client, db
) -> None:
    svc = cached_services(services)
    async with app_client(SHARED_APP, svc=svc) as client:
        first = (await client.post("/v1/ai/chat", json=chat_body(GENERIC))).json()
        second = (await client.post("/v1/ai/chat", json=chat_body(GENERIC))).json()
    assert first["cache"] == {
        "checked": True,
        "hit": False,
        "cache_type": "exact+semantic",
        "upstream_call": True,
        "avoided_input_tokens": None,
        "avoided_output_tokens": None,
        "avoided_provenance": None,
    }
    assert second["status"] == "completed" and second["execution_path"] == "cache-return"
    assert {k: second["cache"][k] for k in ("checked", "hit", "cache_type", "upstream_call")} == {
        "checked": True,
        "hit": True,
        "cache_type": "exact",
        "upstream_call": False,
    }
    assert second["cim"]["message"] == "Full response-cache hit: no upstream inference call"
    assert second["cim"]["passages_retrieved"] is second["cim"]["passages_kept"] is None  # nothing retrieved
    # The tokens this request did not spend: the original run's usage, a counterfactual, so never measured.
    assert (second["cache"]["avoided_input_tokens"], second["cache"]["avoided_output_tokens"]) == (
        first["usage"]["upstream_submitted_input_tokens"],
        first["usage"]["upstream_output_tokens"],
    )
    assert second["cache"]["avoided_input_tokens"] > 0
    assert second["cache"]["avoided_provenance"] == "simulated"  # the original answer was simulated
    assert second["response"]["content"] == first["response"]["content"]
    # A cached simulated answer stays simulated; a cache hit never upgrades provenance to measured.
    assert second["response"]["provenance"] == first["response"]["provenance"] == "simulated"
    assert await _attempts(db, second["run_id"]) == []
    event = (
        await db.execute(select(CacheEventRow).where(CacheEventRow.run_id == second["run_id"]))
    ).scalar_one()
    assert event.hit and event.cache_type == "exact" and event.source_run_id == first["run_id"]
    optimization = (
        await db.execute(select(OptimizationEventRow).where(OptimizationEventRow.run_id == second["run_id"]))
    ).scalar_one()
    assert optimization.avoided_call and optimization.modeled_avoided_tokens > 0


async def test_semantic_hit_requires_factual_equivalence(services, app_client) -> None:
    svc = cached_services(services)
    paraphrase = "List the synthetic roaming rates for Europe on the Traveller 10 bundle."
    other_region = GENERIC.replace("Europe", "Asia")
    async with app_client(SHARED_APP, svc=svc) as client:
        source = (await client.post("/v1/ai/chat", json=chat_body(GENERIC))).json()
        similar = (await client.post("/v1/ai/chat", json=chat_body(paraphrase))).json()
        different = (await client.post("/v1/ai/chat", json=chat_body(other_region))).json()
    assert similar["cache"]["hit"] and similar["cache"]["cache_type"] == "semantic"
    assert similar["response"]["content"] == source["response"]["content"]
    # Identical embeddings, different region: the entity check refuses the reuse.
    assert not different["cache"]["hit"] and different["cache"]["upstream_call"]


async def test_account_specific_requests_without_end_user_identity_are_not_cached(
    services, app_client, db
) -> None:
    svc = cached_services(services)
    prompt = "Why did my bill increase by 15 AED this month?"
    async with app_client("gct-support-chatbot", svc=svc) as client:
        bodies = [(await client.post("/v1/ai/chat", json=chat_body(prompt))).json() for _ in range(2)]
    for body in bodies:
        assert body["status"] == "completed"
        assert body["cache"]["checked"] is False and body["cache"]["upstream_call"] is True
        assert len(await _attempts(db, body["run_id"])) == 1


async def test_cache_store_outage_degrades_to_a_miss(services, app_client) -> None:
    broken = Redis.from_url("redis://127.0.0.1:9/0", decode_responses=True, socket_connect_timeout=0.2)
    svc = cached_services(services, redis=broken)
    async with app_client(SHARED_APP, svc=svc) as client:
        body = (await client.post("/v1/ai/chat", json=chat_body(GENERIC))).json()
    assert body["status"] == "completed" and body["cache"]["hit"] is False
    await broken.aclose()


async def test_a_baseline_comparison_is_never_answered_by_a_run_without_one(services, app_client, db) -> None:
    # Scenarios' rehearsal: a visitor who turns on the comparison after someone ran the same prompt without
    # it must still see a baseline. Identical comparison requests still share their own cached answer.
    svc = cached_services(services)
    async with app_client(SHARED_APP, svc=svc) as client:
        plain = (await client.post("/v1/ai/chat", json=chat_body(GENERIC))).json()
        compared, repeat = [
            (await client.post("/v1/ai/chat", json=chat_body(GENERIC, baseline_comparison=True))).json()
            for _ in range(2)
        ]
        again = (await client.post("/v1/ai/chat", json=chat_body(GENERIC))).json()
    assert plain["cache"]["hit"] is False
    assert compared["execution_path"] != "cache-return" and compared["cache"]["hit"] is False
    assert "baseline" in {a.stage for a in await _attempts(db, compared["run_id"])}
    assert repeat["execution_path"] == "cache-return"  # the same comparison request: its own entry
    assert again["execution_path"] == "cache-return"  # the plain request: the first answer


async def test_public_visitors_never_share_cached_answers_but_reuse_their_own(
    services, make_client, app_client, db
) -> None:
    # Every visitor holds the public_visitor role: a role-shared entry would answer the next visitor's first
    # run of a scenario from someone else's run, hiding the scenario's real path. Scoped per session instead.
    svc = cached_services(services)
    paraphrase = "List the synthetic roaming rates for Europe on the Traveller 10 bundle."
    first, second = await start_session(make_client), await start_session(make_client)
    request = chat_body(GENERIC, **console(SHARED_APP))
    async with make_client(first, svc=svc) as client:
        ran = (await client.post("/v1/ai/chat", json=request)).json()
        repeat = (await client.post("/v1/ai/chat", json=request)).json()
    async with make_client(second, svc=svc) as client:
        other = (await client.post("/v1/ai/chat", json=request)).json()
        similar = (await client.post("/v1/ai/chat", json=chat_body(paraphrase, **console(SHARED_APP)))).json()
    assert ran["status"] == "completed" and ran["cache"]["hit"] is False
    assert repeat["execution_path"] == "cache-return"  # the same visitor's repeat
    assert other["cache"]["hit"] is False and other["execution_path"] != "cache-return"  # exact: not shared
    assert similar["cache"]["hit"] and similar["cache"]["cache_type"] == "semantic"  # their own entry only
    assert similar["response"]["content"] == other["response"]["content"]
    spans = (await db.execute(select(TraceSpanRow).where(TraceSpanRow.run_id == ran["run_id"]))).scalars()
    [cache_span] = [s for s in spans if s.name == "cache"]
    assert cache_span.attributes["aiva.cache.scope_rule"] == "Scoped to the visitor session"
    # Application callers keep sharing within application and role (the earlier tests in this module).
    async with app_client(SHARED_APP, svc=svc) as client:
        shared = (await client.post("/v1/ai/chat", json=chat_body(GENERIC))).json()
        again = (await client.post("/v1/ai/chat", json=chat_body(GENERIC))).json()
    assert shared["cache"]["hit"] is False and again["execution_path"] == "cache-return"
