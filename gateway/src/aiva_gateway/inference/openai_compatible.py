"""OpenAI-compatible chat completions via the official OpenAI SDK (streamed for TTFT).

Serves three routes:
- OpenAI's own API (`https://api.openai.com/v1`) for the answer-quality grader (ADR-0023), with the key from
  `AIVA_OPENAI_API_KEY`. Its reasoning models take `max_completion_tokens`, not `max_tokens`.
- HF Inference Providers router (`https://router.huggingface.co/v1`, model `<repo>:<provider>`), used for
  DeepSeek-V4.1-Flash with the provider pinned in `config/models.yaml`.
- vLLM on HF Inference Endpoints (`<endpoint-url>/v1`) for GCT-Qwen (Endpoint B) and the CIM (Endpoint A).
  The served model name is the vLLM `--served-model-name` (`adapter` in config), else the repo id. When
  the resolver confirmed the endpoint's weights, the call is labelled with the pinned version
  (`<repo>@<revision[:12]>`); otherwise with the name the server reports.

`stream_options.include_usage` asks the server for usage on the final chunk; vLLM and the router's
providers report it. If a server omits it, tokens are estimated and labelled `estimated`.
"""

from collections.abc import Awaitable, Callable
from typing import Any

import httpx2
import openai

from aiva_gateway.core.enums import Provenance
from aiva_gateway.inference.base import (
    AUTH_FAILED,
    CONNECTION_ERROR,
    ENDPOINT_COLD_START,
    ENDPOINT_UNAVAILABLE,
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
from aiva_gateway.inference.endpoints import ResolvedEndpoint
from aiva_gateway.pipeline.interfaces import InferenceRequest, TokenUsage

HF_ROUTER_URL = "https://router.huggingface.co/v1"
OPENAI_URL = "https://api.openai.com/v1"
EndpointSource = Callable[[], Awaitable[ResolvedEndpoint]]


def to_openai_messages(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    # A bare `tool` message needs a tool_call_id; tool output is passed as user content instead.
    return [{"role": "user" if m["role"] == "tool" else m["role"], "content": m["content"]} for m in messages]


class OpenAICompatibleAdapter(LiveAdapter):
    provider = "openai-compatible"

    def __init__(
        self,
        *,
        api_key: str,
        http_client: httpx2.AsyncClient,
        provenance: Provenance,
        served_model: str,
        base_url: str | EndpointSource,
        dedicated_endpoint: bool = False,
        version_suffix: str | None = None,
        fallback_version: str | None = None,
        extra_params: dict[str, Any] | None = None,
        breaker: CircuitBreaker | None = None,
        max_tokens_param: str = "max_tokens",
    ) -> None:
        super().__init__(provenance=provenance, breaker=breaker)
        self._api_key = api_key
        self._http = http_client
        self._base_url = base_url
        self._clients: dict[str, openai.AsyncOpenAI] = {}
        self.served_model = served_model
        self.dedicated_endpoint = dedicated_endpoint
        self.version_suffix = version_suffix
        # Recorded when the call fails before the server names its model (e.g. the pinned version).
        self.fallback_version = fallback_version
        self.extra_params = extra_params or {}
        self.max_tokens_param = max_tokens_param

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _endpoint(self) -> ResolvedEndpoint:
        if isinstance(self._base_url, str):
            return ResolvedEndpoint(self._base_url, None)
        return await self._base_url()

    def _client(self, base: str) -> openai.AsyncOpenAI:
        if not base.rstrip("/").endswith("/v1"):
            base = f"{base.rstrip('/')}/v1"
        client = self._clients.get(base)
        if client is None:
            client = openai.AsyncOpenAI(
                api_key=self._api_key, base_url=base, http_client=self._http, max_retries=0
            )
            self._clients[base] = client
        return client

    async def _stream(self, request: InferenceRequest, state: StreamState) -> None:
        state.model_version = self.fallback_version
        endpoint = await self._endpoint()
        verified = endpoint.verified_version
        if verified:
            state.model_version = verified
        client = self._client(endpoint.url)
        params: dict[str, Any] = {
            "model": self.served_model,
            "messages": to_openai_messages(request.messages),
            self.max_tokens_param: request.max_output_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
            **self.extra_params,
        }
        if request.response_format is not None:
            params["response_format"] = request.response_format
        if request.temperature is not None:
            params["temperature"] = request.temperature
        stream = await client.chat.completions.create(**params)
        async for chunk in stream:
            if not verified:
                state.model_version = chunk.model or state.model_version
            for choice in chunk.choices:
                state.add_text(choice.delta.content)
                if choice.finish_reason:
                    state.finish_reason = choice.finish_reason
            if chunk.usage is not None:
                usage = chunk.usage
                prompt_details = usage.prompt_tokens_details
                completion_details = usage.completion_tokens_details
                state.usage = TokenUsage(
                    input_tokens=usage.prompt_tokens,
                    output_tokens=usage.completion_tokens,
                    cached_input_tokens=(prompt_details.cached_tokens or 0) if prompt_details else 0,
                    reasoning_tokens=(completion_details.reasoning_tokens or 0) if completion_details else 0,
                    provenance=self.provenance,
                )
        if state.model_version and self.version_suffix and not verified:
            state.model_version = f"{state.model_version}:{self.version_suffix}"

    def _classify(self, exc: Exception) -> ProviderCallError:
        if isinstance(exc, openai.APITimeoutError):
            return ProviderCallError(TIMEOUT, retryable=True, request_sent=True)
        if isinstance(exc, openai.APIConnectionError):
            return ProviderCallError(CONNECTION_ERROR, retryable=True)
        if isinstance(exc, openai.RateLimitError):
            retry_after = parse_retry_after(exc.response.headers.get("retry-after"))
            return ProviderCallError(
                RATE_LIMITED, retryable=True, status_code=429, retry_after_seconds=retry_after
            )
        if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
            return ProviderCallError(AUTH_FAILED, retryable=False, status_code=exc.status_code)
        if isinstance(exc, openai.APIStatusError):
            status = exc.status_code
            if self.dedicated_endpoint and status in (502, 503, 504):
                return ProviderCallError(ENDPOINT_COLD_START, retryable=True, status_code=status)
            if self.dedicated_endpoint and status == 404:
                return ProviderCallError(ENDPOINT_UNAVAILABLE, retryable=False, status_code=status)
            if status < 400:
                return ProviderCallError(UPSTREAM_ERROR, retryable=True, request_sent=True)
            if status >= 500:
                return ProviderCallError(UPSTREAM_ERROR, retryable=True, status_code=status)
            return ProviderCallError(PROVIDER_REJECTED, retryable=False, status_code=status)
        if isinstance(exc, httpx2.TimeoutException):
            return ProviderCallError(TIMEOUT, retryable=True, request_sent=True)
        if isinstance(exc, httpx2.TransportError):
            return ProviderCallError(CONNECTION_ERROR, retryable=True)
        return ProviderCallError(UPSTREAM_ERROR, retryable=True, request_sent=True)
