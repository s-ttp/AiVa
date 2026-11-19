"""Gemini via the official Google GenAI SDK (streamed for TTFT).

- No SDK retries (the SDK retries only when `retry_options` is set; it is never set here).
- An explicit `httpx` client keeps the SDK on httpx, so recordings and the proxy configuration apply.
- `max_output_tokens` bounds answer and thinking tokens together (verified 2026-10-14), so the reserved
  output bound holds. Billable output = candidates + thoughts (Gemini prices thinking as output).
- Usage: `prompt_token_count` already includes `cached_content_token_count`.
"""

from typing import cast

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

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
)
from aiva_gateway.pipeline.interfaces import InferenceRequest, TokenUsage


def to_gemini_contents(messages: list[dict[str, str]]) -> tuple[str | None, list[types.Content]]:
    system = [m["content"] for m in messages if m["role"] == "system"]
    contents = [
        types.Content(
            role="model" if m["role"] == "assistant" else "user", parts=[types.Part(text=m["content"])]
        )
        for m in messages
        if m["role"] != "system"
    ]
    return ("\n\n".join(system) if system else None), contents


class GeminiAdapter(LiveAdapter):
    provider = "google"

    def __init__(
        self,
        *,
        api_key: str,
        http_client: httpx.AsyncClient,
        provenance: Provenance,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        super().__init__(provenance=provenance, breaker=breaker)
        self._http = http_client
        self._client = genai.Client(
            api_key=api_key, http_options=types.HttpOptions(httpx_async_client=http_client)
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _stream(self, request: InferenceRequest, state: StreamState) -> None:
        system, contents = to_gemini_contents(request.messages)
        config = types.GenerateContentConfig(
            max_output_tokens=request.max_output_tokens,
            system_instruction=system,
            # None leaves `generationConfig.temperature` out of the request: the model's default.
            temperature=request.temperature,
        )
        usage: types.GenerateContentResponseUsageMetadata | None = None
        stream = await self._client.aio.models.generate_content_stream(
            model=request.model.provider_model_id,
            # Typed list[Content]; with Pillow installed (python-pptx) the SDK's union no longer admits it.
            contents=cast(types.ContentListUnion, contents),
            config=config,
        )
        async for chunk in stream:
            usage = chunk.usage_metadata or usage
            state.model_version = chunk.model_version or state.model_version
            for candidate in chunk.candidates or []:
                if candidate.finish_reason is not None:
                    state.finish_reason = str(candidate.finish_reason.value).lower()
                for part in (candidate.content.parts if candidate.content else None) or []:
                    if part.text and not part.thought:
                        state.add_text(part.text)
        if usage is not None and usage.prompt_token_count is not None:
            thoughts = usage.thoughts_token_count or 0
            state.usage = TokenUsage(
                input_tokens=usage.prompt_token_count + (usage.tool_use_prompt_token_count or 0),
                output_tokens=(usage.candidates_token_count or 0) + thoughts,
                cached_input_tokens=usage.cached_content_token_count or 0,
                reasoning_tokens=thoughts,
                provenance=self.provenance,
            )

    def _classify(self, exc: Exception) -> ProviderCallError:
        if isinstance(exc, genai_errors.APIError):
            code = int(exc.code or 0)
            if code == 429:
                return ProviderCallError(RATE_LIMITED, retryable=True, status_code=429)
            if code in (401, 403):
                return ProviderCallError(AUTH_FAILED, retryable=False, status_code=code)
            if code >= 500:
                return ProviderCallError(UPSTREAM_ERROR, retryable=True, status_code=code)
            if code == 0:  # error surfaced inside an open stream
                return ProviderCallError(UPSTREAM_ERROR, retryable=True, request_sent=True)
            return ProviderCallError(PROVIDER_REJECTED, retryable=False, status_code=code)
        if isinstance(exc, httpx.TimeoutException):
            return ProviderCallError(TIMEOUT, retryable=True, request_sent=True)
        if isinstance(exc, httpx.TransportError):
            return ProviderCallError(CONNECTION_ERROR, retryable=True)
        return ProviderCallError(UPSTREAM_ERROR, retryable=True, request_sent=True)
