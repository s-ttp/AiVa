"""Idempotency-Key replay (LLD §8, §12) backed by Redis with a 24-hour TTL.

The stored replay contains the response body, so it lives in Redis with expiry, never in Postgres.
"""

import json
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from aiva_gateway.core.errors import ConflictError, DependencyUnavailableError

TTL_SECONDS = 24 * 3600


def _key(scope: str, idempotency_key: str) -> str:
    return f"aiva:idem:{scope}:{idempotency_key}"


def _unavailable(exc: Exception) -> DependencyUnavailableError:
    return DependencyUnavailableError(
        "Idempotency store unavailable; the request was not processed.",
        code="IDEMPOTENCY_STORE_UNAVAILABLE",
        remediation="Retry later.",
    )


async def begin(redis: Redis, scope: str, idempotency_key: str, request_hash: str) -> dict[str, Any] | None:
    """Claim the key. Returns a stored replay for a completed identical request, else None."""
    key = _key(scope, idempotency_key)
    marker = json.dumps({"state": "in_progress", "request_hash": request_hash})
    try:
        if await redis.set(key, marker, nx=True, ex=TTL_SECONDS):
            return None
        raw = await redis.get(key)
    except RedisError as exc:
        raise _unavailable(exc) from exc
    existing: dict[str, Any] = json.loads(raw) if raw else {}
    if existing.get("request_hash") != request_hash:
        raise ConflictError(
            "This Idempotency-Key was already used for a different request.",
            code="IDEMPOTENCY_KEY_REUSED",
            remediation="Use a new Idempotency-Key.",
        )
    if existing.get("state") != "completed":
        raise ConflictError(
            "A request with this Idempotency-Key is still in progress.",
            code="IDEMPOTENCY_IN_PROGRESS",
            remediation="Retry after it completes.",
        )
    return existing


async def complete(
    redis: Redis, scope: str, idempotency_key: str, request_hash: str, status_code: int, body: dict[str, Any]
) -> None:
    value = json.dumps(
        {"state": "completed", "request_hash": request_hash, "status_code": status_code, "body": body}
    )
    try:
        await redis.set(_key(scope, idempotency_key), value, ex=TTL_SECONDS)
    except RedisError as exc:
        raise _unavailable(exc) from exc


async def abandon(redis: Redis, scope: str, idempotency_key: str) -> None:
    try:
        await redis.delete(_key(scope, idempotency_key))
    except RedisError:
        return
