"""Response caches (LLD §11): exact and policy-scoped semantic, both in Redis.

Key material (exact): tenant, the run's execution mode (ADR-0019), application, sharing scope, effective
classification, policy bundle hash, knowledge-base version, allowed-model set, messages, evidence ids *and*
content hashes, max output tokens.
Any policy publish, KB change or model-pool change therefore misses.

Sharing scope (fails closed; LLD §11 "no cross-user account/fraud reuse"):
- A request is *personal* when any user message refers to the speaker (I, me, my, we, us, our, …),
  carries a DLP identifier, is fraud work, or is confidential or above. Spotting account nouns cannot work:
  "how much do I owe?" names none. Personal requests are cached per end user (pseudonym) only, never
  semantically, and not at all without an end-user identity (an application credential).
- Applications with `cache_sharing: per_user` (customer-facing ones) cache every request per end user.
- Other requests share entries within tenant + application + role set + classification (LLD §11).

Semantic reuse (LLD §11 "similarity threshold plus factual equivalence") is offered only for single-turn,
evidence-free, DLP-clean, public/internal, non-personal requests, inside the same sharing scope.
A semantic hit additionally needs an identical entity signature (numbers, codes, proper nouns,
negations), because embeddings alone rate "roaming rates for Europe" and "…for Asia" as 0.94 similar.
Query text is embedded in memory only; Redis holds the vector and the signature hash, never the prompt.
A Redis failure degrades to a cache miss; it never returns an unvalidated entry.
"""

import base64
import json
import logging
import re
import time
from collections import OrderedDict
from typing import Any

import numpy as np
from redis.asyncio import Redis
from redis.exceptions import RedisError

from aiva_gateway.contracts.policy import PolicyDecision
from aiva_gateway.core.canonical import sha256_hex
from aiva_gateway.core.enums import Classification, Provenance
from aiva_gateway.optimize.embeddings import Embedder, Vector, embed_async
from aiva_gateway.pipeline.context import RunContext
from aiva_gateway.pipeline.interfaces import CachedResponse, CacheKey, SemanticCacheQuery

logger = logging.getLogger(__name__)

NOT_CACHEABLE_PERSONAL = "Not cached: personal request without an end-user identity"
NOT_CACHEABLE_PER_USER_APP = "Not cached: the application caches per end user and none was identified"
NOT_CACHEABLE_VISITOR = "Not cached: public visitor without a session reference"
# Any first-person reference makes a request personal. Contractions (I'm, we've) match on their stem.
_FIRST_PERSON = re.compile(r"(?i)\b(?:i|me|my|mine|myself|we|us|our|ours|ourselves)\b")
_NEGATIONS = frozenset(
    {"not", "no", "never", "without", "except", "cannot", "can't", "don't", "doesn't", "isn't", "aren't"}
)
_ENTITY_STOPWORDS = frozenset({"i", "what", "which", "how", "why", "when", "where", "who", "please", "the"})
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9'._/-]*")


def _is_fraud(ctx: RunContext) -> bool:
    tags = ctx.request.routing.tags
    return (
        ctx.business_function == "fraud"
        or any("fraud" in tag for tag in tags)
        or "fraud" in (ctx.request.scenario_id or "")
    )


def is_first_person(text: str) -> bool:
    return _FIRST_PERSON.search(text) is not None


def _is_personal(ctx: RunContext) -> bool:
    findings = (ctx.dlp.finding_types if ctx.dlp else []) + (
        ctx.evidence_dlp.finding_types if ctx.evidence_dlp else []
    )
    if findings or _is_fraud(ctx):
        return True
    return any(is_first_person(m["content"]) for m in ctx.messages if m["role"] == "user")


def entity_signature(text: str) -> str:
    """Hash of the facts a semantic hit must share exactly: numbers, codes, proper nouns, negations."""
    entities: set[str] = set()
    sentence_start = True
    for match in _TOKEN.finditer(text):
        word = match.group(0).rstrip(".")
        lower = word.lower()
        if (
            any(ch.isdigit() for ch in word)
            or (word.isupper() and len(word) > 1)
            or (word[:1].isupper() and not sentence_start and lower not in _ENTITY_STOPWORDS)
        ):
            entities.add(lower)
        if lower in _NEGATIONS:
            entities.add(f"neg:{lower}")
        tail = text[match.end() : match.end() + 1]
        sentence_start = match.group(0).endswith((".", "?", "!")) or tail in {".", "?", "!"}
    return sha256_hex(sorted(entities))[:32]


def normalise_query(text: str) -> str:
    return " ".join(text.split())


def build_cache_key(
    ctx: RunContext, decision: PolicyDecision, *, knowledge_version: str
) -> tuple[CacheKey | None, str]:
    """Return the policy-scoped cache key, or None with the reason the request must not be cached."""
    assert ctx.classification is not None
    classification = ctx.classification.effective
    identity = ctx.identity
    personal = _is_personal(ctx) or classification.requires_sovereign_path
    per_user_app = ctx.application is not None and ctx.application.cache_sharing == "per_user"
    # Every public demo visitor holds the same role, so role sharing would let one visitor's run answer the
    # next visitor's first run of a scenario. Each public session has its own pseudonym (ADR-0019): scope
    # to it, so a visitor's first run takes the scenario's real path and only their own repeats hit.
    visitor = identity.is_public_visitor
    if personal or per_user_app or visitor:
        if identity.principal_kind != "user" or not identity.principal_pseudonym:
            if personal:
                return None, NOT_CACHEABLE_PERSONAL
            return None, NOT_CACHEABLE_PER_USER_APP if per_user_app else NOT_CACHEABLE_VISITOR
        sharing = f"user:{identity.principal_pseudonym}"
    else:
        sharing = f"roles:{','.join(sorted(identity.roles)) or '-'}"
    # The run's execution mode (ADR-0019) is part of the scope, so exact and semantic reuse never cross modes:
    # a replayed or simulated answer is never served to a live run, nor a live answer to a replay run.
    mode = ctx.execution_mode or "-"
    scope = f"{ctx.tenant_id}/{mode}/{identity.application_id}/{sharing}/{classification.value}"
    common = {
        "scope": scope,
        "policy": decision.bundle_hash,
        "kb": knowledge_version,
        "models": sorted(decision.effective.allowed_models),
        "max_output_tokens": ctx.request.max_output_tokens,
        # A request for the baseline comparison is never served an answer cached by a run that made no
        # baseline call, nor the reverse.
        "baseline_comparison": ctx.request.baseline_comparison,
    }
    if ctx.required_model is not None:
        # A required model (ADR-0026) is only ever served its own answers, never another model's.
        common["required_model"] = ctx.required_model
    key_hash = sha256_hex(
        {
            **common,
            "messages": ctx.messages,
            "evidence": sorted((e.id, sha256_hex(e.text)) for e in ctx.request.evidence),
        }
    )
    semantic = None
    single_user_turn = len(ctx.messages) == 1 and ctx.messages[0]["role"] == "user"
    injection = bool(ctx.dlp and ctx.dlp.injection_detected)
    if (
        not personal
        and classification.rank <= Classification.INTERNAL.rank
        and single_user_turn
        and not ctx.request.evidence
        and not injection
    ):
        text = normalise_query(ctx.messages[0]["content"])
        semantic = SemanticCacheQuery(
            text=text, bucket=sha256_hex(common)[:32], entity_signature=entity_signature(text)
        )
    if personal:
        reason = "Scoped to the end user: personal request"
    elif per_user_app:
        reason = "Scoped to the end user: the application caches per user"
    elif visitor:
        reason = "Scoped to the visitor session"
    else:
        reason = "Shared within application and role"
    return CacheKey(key_hash=key_hash, scope=scope, semantic=semantic), reason


class NullResponseCache:
    cache_type = "none"

    async def lookup(self, key: CacheKey) -> CachedResponse | None:
        return None

    async def store(self, key: CacheKey, entry: CachedResponse, ttl_seconds: int) -> None:
        return None


def _encode_vector(vector: Vector) -> str:
    return base64.b64encode(vector.astype(np.float16).tobytes()).decode("ascii")


def _decode_vector(data: str) -> Vector:
    return np.frombuffer(base64.b64decode(data), dtype=np.float16).astype(np.float32)


def _entry(payload: dict[str, Any], cache_type: str, similarity: float | None) -> CachedResponse:
    return CachedResponse(
        source_run_id=payload["source_run_id"],
        model_id=payload["model_id"],
        content=payload["content"],
        modeled_input_tokens=int(payload["modeled_input_tokens"]),
        modeled_output_tokens=int(payload["modeled_output_tokens"]),
        modeled_cost_micros=int(payload["modeled_cost_micros"]),
        provenance=Provenance(payload["provenance"]),
        cache_type=cache_type,
        similarity=similarity,
    )


class RedisResponseCache:
    """Exact entries as `<ns>:exact:<key_hash>`; semantic vectors per bucket in `<ns>:semantic:<bucket>`."""

    def __init__(
        self,
        redis: Redis,
        *,
        embedder: Embedder | None,
        semantic_threshold: float = 0.95,
        namespace: str = "aiva:cache",
        max_bucket_entries: int = 256,
    ) -> None:
        self._redis = redis
        self._embedder = embedder
        self.semantic_threshold = semantic_threshold
        self._ns = namespace
        self._max_bucket_entries = max_bucket_entries
        self._vectors: OrderedDict[str, Vector] = OrderedDict()
        self.cache_type = "exact+semantic" if embedder is not None else "exact"

    def _exact(self, key_hash: str) -> str:
        return f"{self._ns}:exact:{key_hash}"

    def _bucket(self, bucket: str) -> str:
        return f"{self._ns}:semantic:{bucket}"

    async def _vector(self, key: CacheKey) -> Vector | None:
        if key.semantic is None or self._embedder is None:
            return None
        cached = self._vectors.get(key.key_hash)
        if cached is not None:
            return cached
        vector: Vector = np.asarray((await embed_async(self._embedder, [key.semantic.text]))[0])
        self._vectors[key.key_hash] = vector
        while len(self._vectors) > 128:
            self._vectors.popitem(last=False)
        return vector

    async def lookup(self, key: CacheKey) -> CachedResponse | None:
        try:
            raw = await self._redis.get(self._exact(key.key_hash))
            if raw:
                payload = json.loads(raw)
                if payload.get("scope") == key.scope:
                    return _entry(payload, "exact", None)
            return await self._semantic_lookup(key)
        except (RedisError, OSError, ValueError, KeyError) as exc:
            logger.warning("response cache lookup failed; treating as a miss: %s", type(exc).__name__)
            return None

    async def _semantic_lookup(self, key: CacheKey) -> CachedResponse | None:
        if key.semantic is None or self._embedder is None:
            return None
        bucket_key = self._bucket(key.semantic.bucket)
        members = {str(k): str(v) for k, v in (await self._redis.hgetall(bucket_key)).items()}
        if not members:
            return None
        vector = await self._vector(key)
        assert vector is not None
        now, expired = time.time(), []
        best: tuple[float, str] | None = None
        for field, raw in members.items():
            item = json.loads(raw)
            if item["exp"] < now:
                expired.append(field)
                continue
            if item["sig"] != key.semantic.entity_signature or item["model"] != self._embedder.model_id:
                continue
            similarity = float(np.dot(vector, _decode_vector(item["v"])))
            if similarity >= self.semantic_threshold and (best is None or similarity > best[0]):
                best = (similarity, field)
        if expired:
            await self._redis.hdel(bucket_key, *expired)
        if best is None:
            return None
        raw_entry = await self._redis.get(self._exact(best[1]))
        if not raw_entry:
            return None
        payload = json.loads(raw_entry)
        if payload.get("scope") != key.scope:
            return None
        return _entry(payload, "semantic", round(best[0], 4))

    async def store(self, key: CacheKey, entry: CachedResponse, ttl_seconds: int) -> None:
        payload = {
            "scope": key.scope,
            "source_run_id": entry.source_run_id,
            "model_id": entry.model_id,
            "content": entry.content,
            "modeled_input_tokens": entry.modeled_input_tokens,
            "modeled_output_tokens": entry.modeled_output_tokens,
            "modeled_cost_micros": entry.modeled_cost_micros,
            "provenance": entry.provenance.value,
            "stored_at": time.time(),
        }
        try:
            await self._redis.set(self._exact(key.key_hash), json.dumps(payload), ex=ttl_seconds)
            vector = await self._vector(key)
            if key.semantic is None or vector is None or self._embedder is None:
                return
            bucket_key = self._bucket(key.semantic.bucket)
            item = {
                "v": _encode_vector(vector),
                "sig": key.semantic.entity_signature,
                "model": self._embedder.model_id,
                "exp": time.time() + ttl_seconds,
            }
            await self._redis.hset(bucket_key, key.key_hash, json.dumps(item))
            await self._redis.expire(bucket_key, ttl_seconds)
            if await self._redis.hlen(bucket_key) > self._max_bucket_entries:
                members = {str(k): str(v) for k, v in (await self._redis.hgetall(bucket_key)).items()}
                oldest = sorted(members, key=lambda f: json.loads(members[f])["exp"])
                await self._redis.hdel(bucket_key, *oldest[: len(members) - self._max_bucket_entries])
        except (RedisError, OSError, ValueError) as exc:
            logger.warning("response cache store failed: %s", type(exc).__name__)
