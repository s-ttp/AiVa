"""Passcode sign-in for the owner and demo users on deployed gateways (ADR-0019).

The passcode (`AIVA_DEMO_PASSCODE`) is compared in constant time. Guessing is limited in Redis, keyed by a
pseudonymous client reference (never the raw address):
- a source with `FAILURES_BEFORE_LOCKOUT` failures within `FAILURE_WINDOW_SECONDS` is locked out for
  `LOCKOUT_SECONDS`;
- at most `ATTEMPTS_PER_MINUTE` attempts a minute are verified tenant-wide, whatever their source.
Both answer 429 with Retry-After. Without Redis no attempt is verified (503, fail closed). Every verified
attempt is audited, without the passcode.
"""

import hashlib
import hmac
import time

from redis.asyncio import Redis
from redis.exceptions import RedisError

from aiva_gateway.core.errors import AivaError, DependencyUnavailableError
from aiva_gateway.db.models import AuditLogRow

FAILURES_BEFORE_LOCKOUT = 5
FAILURE_WINDOW_SECONDS = 15 * 60
LOCKOUT_SECONDS = 15 * 60
ATTEMPTS_PER_MINUTE = 30
UNATTESTED_SOURCE = "unattested"


class SignInLockedError(AivaError):
    status_code = 429
    code = "DEMO_SIGN_IN_LOCKED"
    retryable = True

    def __init__(self, retry_after: int) -> None:
        minutes = max(1, -(-retry_after // 60))
        super().__init__(
            f"Too many sign-in attempts. Try again in {minutes} minute{'s' if minutes > 1 else ''}.",
            remediation="Wait, then sign in with the demo passcode.",
            details={"retry_after_seconds": retry_after},
        )
        self.retry_after = retry_after


def passcode_matches(presented: str, expected: str) -> bool:
    """Constant-time comparison of digests, so neither the passcode nor its length leaks through timing."""
    return hmac.compare_digest(
        hashlib.sha256(presented.encode()).digest(), hashlib.sha256(expected.encode()).digest()
    )


def _key(tenant_id: str, *parts: str) -> str:
    return ":".join(("aiva", "demo-sign-in", tenant_id, *parts))


def _unavailable() -> DependencyUnavailableError:
    return DependencyUnavailableError("Sign-in is temporarily unavailable.", remediation="Try again shortly.")


async def admit_attempt(redis: Redis, tenant_id: str, source: str, *, now: float | None = None) -> None:
    """Raise unless this attempt may be verified: the source is not locked and the tenant-wide rate holds."""
    clock = time.time() if now is None else now
    minute = int(clock // 60)
    try:
        locked_for = int(await redis.ttl(_key(tenant_id, "lock", source)))
        if locked_for > 0:
            raise SignInLockedError(locked_for)
        attempts_key = _key(tenant_id, "attempts", str(minute))
        async with redis.pipeline(transaction=True) as pipe:
            pipe.incr(attempts_key)
            pipe.expire(attempts_key, 120)
            attempts, _ = await pipe.execute()
    except RedisError as exc:
        raise _unavailable() from exc
    if int(attempts) > ATTEMPTS_PER_MINUTE:
        raise SignInLockedError(max(1, 60 - int(clock % 60)))


async def record_failure(redis: Redis, tenant_id: str, source: str) -> bool:
    """Count a wrong passcode; lock the source out when it reaches the limit. Returns whether it did."""
    failures_key = _key(tenant_id, "failures", source)
    try:
        await redis.set(failures_key, 0, ex=FAILURE_WINDOW_SECONDS, nx=True)
        failures = int(await redis.incr(failures_key))
        if failures < FAILURES_BEFORE_LOCKOUT:
            return False
        async with redis.pipeline(transaction=True) as pipe:
            pipe.set(_key(tenant_id, "lock", source), 1, ex=LOCKOUT_SECONDS)
            pipe.delete(failures_key)
            await pipe.execute()
    except RedisError as exc:
        raise _unavailable() from exc
    return True


async def record_success(redis: Redis, tenant_id: str, source: str) -> None:
    try:
        await redis.delete(_key(tenant_id, "failures", source))
    except RedisError as exc:
        raise _unavailable() from exc


def sign_in_audit(
    tenant_id: str, *, source: str, principal_ref: str | None, outcome: str, locked: bool = False
) -> AuditLogRow:
    """`auth.demo_sign_in` or `auth.demo_sign_in_refused`. References are pseudonymous: the signed-in user's
    pseudonym, and the source's client reference; never a passcode, a raw address or a raw user ID."""
    signed_in = outcome == "signed-in"
    return AuditLogRow(
        tenant_id=tenant_id,
        actor_kind="user" if signed_in else "anonymous",
        actor_id=principal_ref if signed_in and principal_ref else source,
        action="auth.demo_sign_in" if signed_in else "auth.demo_sign_in_refused",
        target_type="principal",
        target_id=principal_ref or "unknown",
        details={"method": "passcode", "outcome": outcome, "source": source, "locked": locked},
    )
