"""Public demo sessions (ADR-0019): a browser's token as the seeded visitor, each with its own pseudonym.

A session is issued only to a request a platform proxy vouched for, or in the local and test environments.
Minting is limited per hour in Redis, per source (a pseudonymous client reference, never the raw address)
and tenant-wide: 5 and 200 (`Settings.public_session_limits`; only the local and test environments may change
them). Both answer 429 with Retry-After; without Redis no session is issued (503, fail closed). Every session
is audited (`auth.public_session`).
"""

import time

from redis.asyncio import Redis
from redis.exceptions import RedisError

from aiva_gateway.core.errors import AivaError, DependencyUnavailableError
from aiva_gateway.db.models import AuditLogRow

SESSION_TTL_SECONDS = 2 * 3600
_HOUR = 3600


class PublicSessionLimitedError(AivaError):
    status_code = 429
    code = "PUBLIC_SESSION_LIMITED"
    retryable = True

    def __init__(self, retry_after: int) -> None:
        super().__init__(
            "Too many demo sessions were started recently. Try again later.",
            remediation="Keep using the session you have, or try again later.",
            details={"retry_after_seconds": retry_after},
        )
        self.retry_after = retry_after


def _key(tenant_id: str, *parts: str) -> str:
    return ":".join(("aiva", "public-session", tenant_id, *parts))


async def admit_session(
    redis: Redis, tenant_id: str, source: str, *, limits: tuple[int, int], now: float | None = None
) -> None:
    """Count this session against the hourly `limits` (per source, tenant-wide); raise if either is already
    reached. A source over its own limit is refused before the tenant-wide count, so one source cannot use up
    everyone's sessions."""
    per_source, per_tenant = limits
    clock = time.time() if now is None else now
    hour = str(int(clock // _HOUR))
    retry_after = max(1, _HOUR - int(clock % _HOUR))
    try:
        if await _count(redis, _key(tenant_id, hour, "source", source)) > per_source:
            raise PublicSessionLimitedError(retry_after)
        if await _count(redis, _key(tenant_id, hour, "tenant")) > per_tenant:
            raise PublicSessionLimitedError(retry_after)
    except RedisError as exc:
        raise DependencyUnavailableError(
            "Demo sessions are temporarily unavailable.", remediation="Try again shortly."
        ) from exc


async def _count(redis: Redis, key: str) -> int:
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, 2 * _HOUR)
        count, _ = await pipe.execute()
    return int(count)


def session_audit(tenant_id: str, *, pseudonym: str, source: str) -> AuditLogRow:
    """`auth.public_session`: the new visitor's pseudonym and its source's client reference only."""
    return AuditLogRow(
        tenant_id=tenant_id,
        actor_kind="user",
        actor_id=pseudonym,
        action="auth.public_session",
        target_type="principal",
        target_id=pseudonym,
        details={"source": source, "ttl_seconds": SESSION_TTL_SECONDS},
    )
