"""Per-visitor live-run window for the public demo (ADR-0019, ADR-0008 amendment).

A public visitor may start at most `limit` live runs in any rolling `window`. The window is a Redis sorted
set per visitor pseudonym (member: run ID, score: start time), checked and extended by one Lua script so
concurrent requests cannot overshoot it. Taking a slot is idempotent per run. A slot is given back when the
run's budget refuses it or the run makes no model call. The window lives in Redis only (ADR-0019: clearing
cookies already resets it); the `public_demo` daily budget is the durable bound on spend. An unreachable
store fails closed.
"""

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from redis.asyncio import Redis
from redis.exceptions import RedisError

from aiva_gateway.core.errors import DependencyUnavailableError

# KEYS[1] window. ARGV: now (ms), window (ms), limit, run ID.
# Returns {1, used} when the run holds a slot, or {0, used, oldest start (ms)} when the window is full.
TAKE = """
local now, window, limit = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
if redis.call('ZSCORE', KEYS[1], ARGV[4]) then
  return {1, redis.call('ZCARD', KEYS[1])}
end
local used = redis.call('ZCARD', KEYS[1])
if used >= limit then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  return {0, used, oldest[2]}
end
redis.call('ZADD', KEYS[1], now, ARGV[4])
redis.call('PEXPIRE', KEYS[1], window)
return {1, used + 1}
"""


@dataclass(frozen=True)
class WindowState:
    remaining: int
    resets_at: datetime | None  # when the oldest live run leaves the window; None while runs remain


class VisitorLimitReached(Exception):
    """`resets_at` is None when visitors' live runs are switched off (a limit of 0): nothing frees up."""

    def __init__(self, resets_at: datetime | None) -> None:
        super().__init__("This visitor has used every live run in the current hour.")
        self.resets_at = resets_at


class LiveRunWindow:
    def __init__(
        self,
        redis: Redis,
        *,
        limit: int,
        window: timedelta,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.redis, self.limit, self.window, self.clock = redis, limit, window, clock
        self._take = redis.register_script(TAKE)

    @staticmethod
    def key(tenant_id: str, visitor: str) -> str:
        return f"aiva:quota:{{{tenant_id}}}:live:{visitor}"

    async def take(self, tenant_id: str, visitor: str, run_id: str) -> None:
        """Hold a live-run slot for `run_id`, or raise VisitorLimitReached."""
        if self.limit <= 0:
            raise VisitorLimitReached(None)  # switched off: no slot will ever free up
        now = self.clock()
        try:
            reply = await self._take(
                keys=[self.key(tenant_id, visitor)],
                args=[_ms(now), _ms_delta(self.window), self.limit, run_id],
            )
        except (RedisError, OSError) as exc:
            raise _unavailable() from exc
        if int(reply[0]) == 0:
            oldest = _at(int(float(reply[2]))) if len(reply) > 2 else None
            raise VisitorLimitReached(oldest + self.window if oldest else None)

    async def give_back(self, tenant_id: str, visitor: str, run_id: str) -> None:
        """Return a run's slot (idempotent). Best effort: a slot not returned expires with the window."""
        with suppress(RedisError, OSError):
            await self.redis.zrem(self.key(tenant_id, visitor), run_id)

    async def state(self, tenant_id: str, visitor: str) -> WindowState:
        now = self.clock()
        key = self.key(tenant_id, visitor)
        try:
            pairs = await self.redis.zrangebyscore(key, _ms(now - self.window) + 1, "+inf", withscores=True)
        except (RedisError, OSError) as exc:
            raise _unavailable() from exc
        starts = [int(score) for _, score in cast(list[tuple[Any, float]], pairs)]
        remaining = max(0, self.limit - len(starts))
        oldest = min(starts, default=None)
        resets_at = _at(oldest) + self.window if remaining == 0 and oldest is not None else None
        return WindowState(remaining, resets_at)


def _ms(at: datetime) -> int:
    return int(at.timestamp() * 1000)


def _ms_delta(delta: timedelta) -> int:
    return int(delta.total_seconds() * 1000)


def _at(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def _unavailable() -> DependencyUnavailableError:
    return DependencyUnavailableError(
        "The quota store is unavailable; the live run was not started.",
        code="QUOTA_STORE_UNAVAILABLE",
        remediation="Retry later, or run the scenario in replay.",
    )
