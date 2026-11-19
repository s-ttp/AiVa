"""Appendix B / LLD §11: no response-cache reuse across users for personal (account-specific, fraud) data.

Two end users with different pseudonyms send the same personal prompt. Whatever the first one cached, the
second must never receive it: not by exact key, and not semantically even when the embedder rates the two
requests as identical. The same holds for every request to an application that caches per user.
"""

import uuid
from collections.abc import Sequence

import numpy as np

from aiva_gateway.core.enums import Provenance
from aiva_gateway.optimize.cache import RedisResponseCache
from aiva_gateway.optimize.embeddings import EMBEDDING_DIMENSIONS, Vector
from aiva_gateway.pipeline.interfaces import CachedResponse
from tests.unit.test_cache_keys import FIRST_PERSON_PROMPTS, GENERIC, key, run


class IdenticalEmbedder:
    """Rates every pair of texts as identical: the worst case for semantic reuse."""

    model_id = "identical-test"
    dimensions = EMBEDDING_DIMENSIONS

    def embed(self, texts: Sequence[str], *, query: bool = False) -> Vector:
        rows = np.zeros((len(texts), self.dimensions), dtype=np.float32)
        rows[:, 0] = 1.0
        return rows


def entry(owner: str) -> CachedResponse:
    return CachedResponse(
        source_run_id=f"run-{owner}",
        model_id="gemini-flash-lite",
        content=f"answer for {owner}",
        modeled_input_tokens=10,
        modeled_output_tokens=10,
        modeled_cost_micros=5,
        provenance=Provenance.SIMULATED,
    )


async def test_personal_entries_never_cross_users(services) -> None:  # type: ignore[no-untyped-def]
    cache = RedisResponseCache(
        services.redis, embedder=IdenticalEmbedder(), namespace=f"aiva:test:{uuid.uuid4().hex}"
    )
    cases = [run(prompt, user="p-alice") for prompt in FIRST_PERSON_PROMPTS]
    cases.append(run(GENERIC, user="p-alice", sharing="per_user"))
    for alice_ctx in cases:
        alice = key(alice_ctx)
        assert alice is not None
        await cache.store(alice, entry("alice"), ttl_seconds=60)
        assert (await cache.lookup(alice)) is not None  # Alice gets her own entry back
        prompt = alice_ctx.messages[0]["content"]
        bob = key(
            run(
                prompt,
                user="p-bob",
                sharing=alice_ctx.application.cache_sharing if alice_ctx.application else None,
            )
        )
        assert bob is not None and bob.key_hash != alice.key_hash
        assert await cache.lookup(bob) is None, prompt
