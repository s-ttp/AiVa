"""Shared machinery for live provider adapters (LLD §3 inference adapter, §17 failure handling).

Adapters never retry internally: the orchestrator performs the single bounded retry so that every attempt
is policy-checked, reserved and metered as its own ledger row (LLD §12, §21.4). An adapter only reports
whether a failure is retryable. Provider-reported usage is authoritative (`measured`, or `replayed` from a
recording); when a provider reports none, tokens are estimated and labelled `estimated`.
"""

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from aiva_gateway.core.canonical import estimate_tokens
from aiva_gateway.core.enums import Provenance
from aiva_gateway.pipeline.interfaces import InferenceRequest, InferenceResult, TokenUsage

# Stable error codes recorded on model attempts (`model_attempt.error_code`).
TIMEOUT = "TIMEOUT"
RATE_LIMITED = "RATE_LIMITED"
UPSTREAM_ERROR = "UPSTREAM_ERROR"
CONNECTION_ERROR = "CONNECTION_ERROR"
ENDPOINT_COLD_START = "ENDPOINT_COLD_START"
ENDPOINT_UNAVAILABLE = "ENDPOINT_UNAVAILABLE"
ENDPOINT_NOT_CONFIGURED = "ENDPOINT_NOT_CONFIGURED"
ENDPOINT_REVISION_MISMATCH = "ENDPOINT_REVISION_MISMATCH"
CREDENTIALS_MISSING = "CREDENTIALS_MISSING"
AUTH_FAILED = "AUTH_FAILED"
PROVIDER_REJECTED = "PROVIDER_REJECTED"
CIRCUIT_OPEN = "CIRCUIT_OPEN"
REPLAY_MISS = "REPLAY_MISS"

# Failures that say the provider (not the request) is unhealthy; only these move the circuit breaker.
_BREAKER_FAILURES = frozenset({TIMEOUT, UPSTREAM_ERROR, CONNECTION_ERROR})
MAX_RATE_LIMIT_BACKOFF_SECONDS = 2.0


class ProviderCallError(Exception):
    """A classified provider failure.

    `request_sent` is False when the provider cannot have processed the request (connection refused,
    configuration error, HTTP error response), so no usage is charged. A timeout or a failure part-way
    through a stream may have been billed; usage is then estimated conservatively and labelled.
    """

    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        request_sent: bool = False,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.request_sent = request_sent
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class ReplayMissError(Exception):
    """Replay mode found no recording for a request. Never falls through to the network."""


def find_cause[E: BaseException](exc: BaseException, kind: type[E]) -> E | None:
    """Search an exception and its cause/context chain (SDKs wrap transport errors)."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, kind):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


@dataclass
class StreamState:
    """Collects one streamed completion. TTFT = first non-empty answer token (thinking excluded)."""

    started: float
    parts: list[str] = field(default_factory=list)
    first_token_at: float | None = None
    usage: TokenUsage | None = None
    finish_reason: str | None = None
    model_version: str | None = None

    def add_text(self, text: str | None) -> None:
        if not text:
            return
        if self.first_token_at is None:
            self.first_token_at = time.perf_counter()
        self.parts.append(text)

    @property
    def text(self) -> str:
        return "".join(self.parts)

    @property
    def ttft_ms(self) -> float | None:
        return None if self.first_token_at is None else (self.first_token_at - self.started) * 1000


class CircuitBreaker:
    """Per-model breaker (LLD §17): opens after consecutive provider failures, probes after a cooldown.

    In-process state: each gateway replica protects its own calls. Rate limits, cold starts and request
    errors do not count — the provider is reachable in those cases.
    """

    def __init__(self, *, failure_threshold: int = 5, reset_seconds: float = 30.0) -> None:
        self.failure_threshold = failure_threshold
        self.reset_seconds = reset_seconds
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if time.monotonic() - self._opened_at >= self.reset_seconds:
            return "half-open"
        return "open"

    def allow(self) -> bool:
        state = self.state
        if state == "closed":
            return True
        if state == "half-open" and not self._probe_in_flight:
            self._probe_in_flight = True
            return True
        return False

    def record(self, error_code: str | None) -> None:
        self._probe_in_flight = False
        if error_code is None:
            self._failures, self._opened_at = 0, None
            return
        if error_code not in _BREAKER_FAILURES:
            if self._opened_at is not None:  # a reachable provider closes a half-open breaker
                self._failures, self._opened_at = 0, None
            return
        self._failures += 1
        if self._failures >= self.failure_threshold or self._opened_at is not None:
            self._opened_at = time.monotonic()


def estimate_input_tokens(request: InferenceRequest) -> int:
    return sum(estimate_tokens(m["content"]) for m in request.messages)


class LiveAdapter(ABC):
    """Template for provider adapters: breaker, deadline, classification, usage and provenance."""

    provider: str = "live"

    def __init__(self, *, provenance: Provenance, breaker: CircuitBreaker | None = None) -> None:
        if provenance not in (Provenance.MEASURED, Provenance.REPLAYED):
            raise ValueError("live adapters report measured or replayed results only")
        self.provenance = provenance
        self.breaker = breaker or CircuitBreaker()

    @property
    def _estimate_label(self) -> Provenance:
        return Provenance.ESTIMATED if self.provenance is Provenance.MEASURED else self.provenance

    @abstractmethod
    async def _stream(self, request: InferenceRequest, state: StreamState) -> None:
        """Run one streamed call, filling `state`. Raise ProviderCallError or SDK exceptions."""

    @abstractmethod
    def _classify(self, exc: Exception) -> ProviderCallError:
        """Map an SDK or transport exception to a ProviderCallError."""

    async def aclose(self) -> None:  # optional hook
        return None

    async def complete(self, request: InferenceRequest) -> InferenceResult:
        started = time.perf_counter()
        state = StreamState(started=started)
        if not self.breaker.allow():
            return self._failure(request, state, ProviderCallError(CIRCUIT_OPEN, retryable=False))
        try:
            async with asyncio.timeout(request.timeout_seconds):
                await self._stream(request, state)
        except ProviderCallError as exc:
            return await self._fail(request, state, exc)
        except TimeoutError:
            return await self._fail(
                request, state, ProviderCallError(TIMEOUT, retryable=True, request_sent=True)
            )
        except Exception as exc:
            if find_cause(exc, ReplayMissError) is not None:
                return await self._fail(request, state, ProviderCallError(REPLAY_MISS, retryable=False))
            return await self._fail(request, state, self._classify(exc))
        self.breaker.record(None)
        usage = state.usage
        if usage is None:
            usage = TokenUsage(
                input_tokens=estimate_input_tokens(request),
                output_tokens=estimate_tokens(state.text),
                provenance=self._estimate_label,
            )
        return InferenceResult(
            status="ok",
            content=state.text,
            finish_reason=state.finish_reason,
            usage=usage,
            latency_ms=(time.perf_counter() - started) * 1000,
            ttft_ms=state.ttft_ms,
            provider_model_version=state.model_version or request.model.revision,
            provenance=self.provenance,
        )

    async def _fail(
        self, request: InferenceRequest, state: StreamState, error: ProviderCallError
    ) -> InferenceResult:
        self.breaker.record(error.code)
        if error.code == RATE_LIMITED:
            # Give the orchestrator's single bounded retry a chance; capped so the run stays responsive.
            hint = 1.0 if error.retry_after_seconds is None else error.retry_after_seconds
            delay = min(hint, MAX_RATE_LIMIT_BACKOFF_SECONDS)
            await asyncio.sleep(delay)
        return self._failure(request, state, error)

    def _failure(
        self, request: InferenceRequest, state: StreamState, error: ProviderCallError
    ) -> InferenceResult:
        if state.usage is not None:
            usage = state.usage
        elif error.request_sent or state.parts:
            # Possibly billed: charge the prompt plus any partial output, labelled as an estimate.
            usage = TokenUsage(
                input_tokens=estimate_input_tokens(request),
                output_tokens=estimate_tokens(state.text),
                provenance=self._estimate_label,
            )
        else:
            usage = TokenUsage(provenance=self.provenance)
        return InferenceResult(
            status="error",
            content="",
            finish_reason=None,
            usage=usage,
            latency_ms=(time.perf_counter() - state.started) * 1000,
            ttft_ms=state.ttft_ms,
            provider_model_version=state.model_version or request.model.revision,
            provenance=self.provenance,
            error_code=error.code,
            retryable=error.retryable,
        )


class UnavailableAdapter:
    """Stands in for a model that cannot be called in this deployment (no credentials or endpoint).

    Fails without contacting anything and is never retried; the sovereign path therefore fails closed.
    """

    def __init__(self, error_code: str, *, provenance: Provenance = Provenance.MEASURED) -> None:
        self.error_code = error_code
        self.provenance = provenance

    async def complete(self, request: InferenceRequest) -> InferenceResult:
        return InferenceResult(
            status="error",
            content="",
            finish_reason=None,
            usage=TokenUsage(provenance=self.provenance),
            latency_ms=0.0,
            ttft_ms=None,
            provider_model_version=request.model.revision,
            provenance=self.provenance,
            error_code=self.error_code,
            retryable=False,
        )
