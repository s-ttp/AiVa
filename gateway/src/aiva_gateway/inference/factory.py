"""Build inference adapters for the configured mode (ADR-0009).

| Mode | Upstream adapters | Provenance |
|---|---|---|
| simulated (default) | SimulatedInferenceAdapter | simulated |
| live | provider SDKs over the network | measured |
| record | live, and every exchange is saved to `recordings_dir` | measured |
| replay | provider SDKs over recorded exchanges only; a missing recording fails | replayed |

Models listed in `simulated_models` stay simulated in every mode. A model without credentials or an
endpoint gets an adapter that fails closed with a stable error code; nothing falls back to another model.
"""

import httpx
import httpx2

from aiva_gateway.catalog import Catalog
from aiva_gateway.catalog.models import ModelSpec
from aiva_gateway.config import Settings
from aiva_gateway.core.enums import Provenance
from aiva_gateway.inference.anthropic_adapter import AnthropicAdapter
from aiva_gateway.inference.base import (
    CREDENTIALS_MISSING,
    CircuitBreaker,
    UnavailableAdapter,
)
from aiva_gateway.inference.endpoints import EndpointResolver, ResolvedEndpoint
from aiva_gateway.inference.gemini_adapter import GeminiAdapter
from aiva_gateway.inference.openai_compatible import HF_ROUTER_URL, OPENAI_URL, OpenAICompatibleAdapter
from aiva_gateway.inference.recording import (
    Httpx2RecordingTransport,
    Httpx2ReplayTransport,
    HttpxRecordingTransport,
    HttpxReplayTransport,
    RecordingStore,
)
from aiva_gateway.inference.registry import AdapterRegistry
from aiva_gateway.inference.replay import RecordedEndpointResolver, ReplayOrSimulated, replay_settings
from aiva_gateway.inference.simulated import SimulatedInferenceAdapter
from aiva_gateway.pipeline.interfaces import InferenceAdapter

MODES = ("simulated", "live", "replay", "record")
REPLAY_CREDENTIAL = "replay-mode-no-credential"


def validate_mode(settings: Settings) -> str:
    if settings.inference_mode not in MODES:
        raise ValueError(f"inference_mode must be one of {MODES}, got {settings.inference_mode!r}")
    return settings.inference_mode


def provider_provenance(mode: str) -> Provenance:
    return Provenance.REPLAYED if mode == "replay" else Provenance.MEASURED


def _secret(settings: Settings, name: str) -> str | None:
    if settings.inference_mode == "replay":
        return REPLAY_CREDENTIAL
    value = getattr(settings, name)
    return value.get_secret_value() if value is not None else None


def _store(settings: Settings, label: str) -> RecordingStore:
    return RecordingStore(settings.recordings_dir, label=label)


def httpx2_client(settings: Settings, label: str) -> httpx2.AsyncClient:
    timeout = httpx2.Timeout(
        settings.provider_read_timeout_seconds, connect=settings.provider_connect_timeout_seconds
    )
    if settings.inference_mode == "replay":
        return httpx2.AsyncClient(timeout=timeout, transport=Httpx2ReplayTransport(_store(settings, label)))
    if settings.inference_mode == "record":
        return httpx2.AsyncClient(
            timeout=timeout, transport=Httpx2RecordingTransport(_store(settings, label))
        )
    return httpx2.AsyncClient(timeout=timeout)  # proxy and CA bundle come from the environment


def httpx_client(settings: Settings, label: str) -> httpx.AsyncClient:
    timeout = httpx.Timeout(
        settings.provider_read_timeout_seconds, connect=settings.provider_connect_timeout_seconds
    )
    if settings.inference_mode == "replay":
        return httpx.AsyncClient(timeout=timeout, transport=HttpxReplayTransport(_store(settings, label)))
    if settings.inference_mode == "record":
        return httpx.AsyncClient(timeout=timeout, transport=HttpxRecordingTransport(_store(settings, label)))
    return httpx.AsyncClient(timeout=timeout)


def build_resolver(settings: Settings, http: httpx.AsyncClient | None) -> EndpointResolver:
    token = settings.hf_token.get_secret_value() if settings.hf_token else None
    live = settings.inference_mode in ("live", "record")
    return EndpointResolver(
        overrides=settings.endpoint_urls,
        namespace=settings.endpoint_namespace if live else None,
        token=token if live else None,
        http=http if live else None,
    )


def served_model_name(model: ModelSpec) -> str:
    """The vLLM `--served-model-name` (`adapter` in config), else the repo id."""
    return model.adapter or model.provider_model_id


# Qwen3 chat templates think unless told otherwise. Endpoint B disables it by default; the request says so
# too, so a redeploy without that default cannot add thinking tokens to answers. Other templates ignore it.
NO_THINKING = {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}


def build_openai_compatible(
    model: ModelSpec,
    settings: Settings,
    resolver: EndpointResolver,
    *,
    extra_params: dict[str, object] | None = None,
) -> InferenceAdapter:
    token = _secret(settings, "hf_token")
    if token is None:
        return UnavailableAdapter(CREDENTIALS_MISSING)
    breaker = CircuitBreaker(
        failure_threshold=settings.breaker_failure_threshold, reset_seconds=settings.breaker_reset_seconds
    )
    provenance = provider_provenance(settings.inference_mode)
    http = httpx2_client(settings, model.id)
    if model.provider == "hf-router":
        route = model.provider_route
        return OpenAICompatibleAdapter(
            api_key=token,
            http_client=http,
            provenance=provenance,
            served_model=f"{model.provider_model_id}:{route}" if route else model.provider_model_id,
            base_url=HF_ROUTER_URL,
            version_suffix=route,
            extra_params=dict(extra_params or {}),
            breaker=breaker,
        )

    async def endpoint() -> ResolvedEndpoint:
        return await resolver.resolve(model)

    params = dict(extra_params or {})
    if model.role == "upstream":
        params = {**NO_THINKING, **params}
    return OpenAICompatibleAdapter(
        api_key=token,
        http_client=http,
        provenance=provenance,
        served_model=served_model_name(model),
        base_url=endpoint,
        dedicated_endpoint=True,
        fallback_version=model.pinned_version,
        extra_params=params,
        breaker=breaker,
    )


def build_adapter(model: ModelSpec, settings: Settings, resolver: EndpointResolver) -> InferenceAdapter:
    mode = settings.inference_mode
    if mode == "simulated" or model.id in settings.simulated_models or model.provider == "simulated":
        return SimulatedInferenceAdapter()
    provenance = provider_provenance(mode)
    breaker = CircuitBreaker(
        failure_threshold=settings.breaker_failure_threshold, reset_seconds=settings.breaker_reset_seconds
    )
    if model.provider == "anthropic":
        key = _secret(settings, "anthropic_api_key")
        if key is None:
            return UnavailableAdapter(CREDENTIALS_MISSING)
        return AnthropicAdapter(
            api_key=key,
            http_client=httpx2_client(settings, model.id),
            provenance=provenance,
            effort=settings.anthropic_effort,
            breaker=breaker,
        )
    if model.provider == "google":
        key = _secret(settings, "gemini_api_key")
        if key is None:
            return UnavailableAdapter(CREDENTIALS_MISSING)
        return GeminiAdapter(
            api_key=key, http_client=httpx_client(settings, model.id), provenance=provenance, breaker=breaker
        )
    if model.provider == "openai":
        key = _secret(settings, "openai_api_key")
        if key is None:
            return UnavailableAdapter(CREDENTIALS_MISSING)
        return OpenAICompatibleAdapter(
            api_key=key,
            http_client=httpx2_client(settings, model.id),
            provenance=provenance,
            served_model=model.provider_model_id,
            base_url=OPENAI_URL,
            max_tokens_param="max_completion_tokens",
            breaker=breaker,
        )
    return build_openai_compatible(model, settings, resolver)


def build_adapter_registry(
    settings: Settings, catalog: Catalog, resolver: EndpointResolver
) -> AdapterRegistry:
    validate_mode(settings)
    adapters = {
        model.id: build_adapter(model, settings, resolver)
        for model in catalog.models.models
        if model.role in ("upstream", "grader")
    }
    return AdapterRegistry(catalog, adapters=adapters, default=SimulatedInferenceAdapter())


def build_replay_resolver(settings: Settings) -> RecordedEndpointResolver:
    return RecordedEndpointResolver(settings.recordings_dir, overrides=settings.endpoint_urls)


def build_replay_registry(
    settings: Settings, catalog: Catalog, resolver: EndpointResolver
) -> AdapterRegistry:
    """Per-run replay (ADR-0019): every upstream model's replay adapter, a miss answered by the simulated
    adapter. Built next to the configured registry whatever `inference_mode` is; never opens a connection."""
    replay = replay_settings(settings)
    simulated = SimulatedInferenceAdapter()
    adapters: dict[str, InferenceAdapter] = {
        model.id: ReplayOrSimulated(build_adapter(model, replay, resolver), simulated)
        for model in catalog.models.models
        if model.role in ("upstream", "grader")
    }
    return AdapterRegistry(catalog, adapters=adapters, default=simulated)
