"""Claude via the official Anthropic SDK (Messages API, streamed for TTFT).

- `max_retries=0`: the orchestrator owns the bounded retry (inference/base.py).
- Effort is set explicitly (the request's, else the configured one); thinking cannot be disabled on Opus 5.5
  and counts against `max_tokens`, so the reserved output bound still holds. With thinking on, no temperature
  is sent (the catalog refuses one for an Anthropic model, `ModelSpec`).
- Server-side refusal fallbacks are deliberately not enabled: a provider-chosen model switch would bypass
  the AI-PEL egress check and the pinned rate card. A refusal is returned as `finish_reason="refusal"`.
- Usage: Anthropic's `input_tokens` excludes cache reads and writes; the ledger convention is total input
  with `cached_input_tokens` as the discounted subset, so both are added back here.
"""

from typing import Any

import anthropic
import httpx2

from aiva_gateway.core.enums import Provenance
from aiva_gateway.inference.base import (
    AUTH_FAILED,
    CONNECTION_ERROR,
    PROVIDER_REJECTED,
    RATE_LIMITED,
    TIMEOUT,
    UPSTREAM_ERROR,
    CircuitBreaker,
    LiveAdapter,
    ProviderCallError,
    StreamState,
    parse_retry_after,
)
from aiva_gateway.pipeline.interfaces import InferenceRequest, TokenUsage


def to_anthropic_messages(messages: list[dict[str, str]]) -> tuple[str | None, list[dict[str, str]]]:
    """System-role messages become the top-level system prompt; tool output is passed as user content."""
    system = [m["content"] for m in messages if m["role"] == "system"]
    converted = [
        {"role": "assistant" if m["role"] == "assistant" else "user", "content": m["content"]}
        for m in messages
        if m["role"] != "system"
    ]
    return ("\n\n".join(system) if system else None), converted


class AnthropicAdapter(LiveAdapter):
    provider = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        http_client: httpx2.AsyncClient,
        provenance: Provenance,
        effort: str = "medium",
        breaker: CircuitBreaker | None = None,
    ) -> None:
        super().__init__(provenance=provenance, breaker=breaker)
        self._http = http_client
        self._client = anthropic.AsyncAnthropic(api_key=api_key, http_client=http_client, max_retries=0)
        self.effort = effort

    async def aclose(self) -> None:
        await self._client.close()

    async def _stream(self, request: InferenceRequest, state: StreamState) -> None:
        system, messages = to_anthropic_messages(request.messages)
        params: dict[str, Any] = {
            "model": request.model.provider_model_id,
            "max_tokens": request.max_output_tokens,
            "messages": messages,
            "output_config": {"effort": request.effort or self.effort},
        }
        if system:
            params["system"] = system
        async with self._client.messages.stream(**params) as stream:
            async for event in stream:
                if event.type == "content_block_delta" and event.delta.type == "text_delta":
                    state.add_text(event.delta.text)
            message = await stream.get_final_message()
        usage = message.usage
        cache_read = usage.cache_read_input_tokens or 0
        cache_write = usage.cache_creation_input_tokens or 0
        details = getattr(usage, "output_tokens_details", None)
        thinking = getattr(details, "thinking_tokens", 0) or 0
        state.usage = TokenUsage(
            input_tokens=usage.input_tokens + cache_read + cache_write,
            output_tokens=usage.output_tokens,
            cached_input_tokens=cache_read,
            reasoning_tokens=thinking,
            provenance=self.provenance,
        )
        state.finish_reason = message.stop_reason
        state.model_version = message.model

    def _classify(self, exc: Exception) -> ProviderCallError:
        if isinstance(exc, anthropic.APITimeoutError):
            return ProviderCallError(TIMEOUT, retryable=True, request_sent=True)
        if isinstance(exc, anthropic.APIConnectionError):
            return ProviderCallError(CONNECTION_ERROR, retryable=True)
        if isinstance(exc, anthropic.RateLimitError):
            retry_after = parse_retry_after(exc.response.headers.get("retry-after"))
            return ProviderCallError(
                RATE_LIMITED, retryable=True, status_code=429, retry_after_seconds=retry_after
            )
        if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
            return ProviderCallError(AUTH_FAILED, retryable=False, status_code=exc.status_code)
        if isinstance(exc, anthropic.APIStatusError):
            if exc.status_code < 400:  # an `error` event inside a 200 stream: billed work may exist
                return ProviderCallError(UPSTREAM_ERROR, retryable=True, request_sent=True)
            if exc.status_code >= 500:  # includes 529 overloaded
                return ProviderCallError(UPSTREAM_ERROR, retryable=True, status_code=exc.status_code)
            return ProviderCallError(PROVIDER_REJECTED, retryable=False, status_code=exc.status_code)
        if isinstance(exc, httpx2.TimeoutException):
            return ProviderCallError(TIMEOUT, retryable=True, request_sent=True)
        if isinstance(exc, httpx2.TransportError):
            return ProviderCallError(CONNECTION_ERROR, retryable=True)
        # A mid-stream error event or malformed stream: the call reached the provider.
        return ProviderCallError(UPSTREAM_ERROR, retryable=True, request_sent=True)
