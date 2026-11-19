"""HF Inference Endpoint resolution and status (Endpoints A and B, LLD §4).

An endpoint URL comes from `AIVA_ENDPOINT_URLS` (any OpenAI-compatible vLLM server, e.g. a customer VPC
deployment) or is looked up by `endpoint_name` in `AIVA_ENDPOINT_NAMESPACE` through the HF Endpoints API.

- Pinned weights: an endpoint found through the API must serve exactly the repository and revision pinned
  in `config/models.yaml` (`ModelSpec.served_weights`). Anything else fails closed with
  `ENDPOINT_REVISION_MISMATCH` before a request is sent. Only then is the call labelled with the pinned
  version; a URL override cannot be checked, so its calls carry the name the server reports.
- Cold start: scale-to-zero endpoints keep their URL. A request to a scaled-down endpoint wakes it, and the
  endpoint answers 503 until it is ready (about 200 s for Endpoints A/B). Adapters report that as
  `ENDPOINT_COLD_START` (retryable). The orchestrator's single retry does not wait out a cold start: the CIM
  falls back to the full eligible context, and the sovereign path fails closed with a retryable 503.
- Never wake (ADR-0019): `is_awake` reads an endpoint's state without calling it, so a public visitor's live
  run can skip a scaled-to-zero endpoint instead of waking it (each wake bills the 15-minute idle tail).
- The live demo page (ADR-0021) reads each endpoint's state (`reading`, never the endpoint itself) and may
  wake it (`wake`: a paused endpoint is resumed through the HF Endpoints API; a scaled-to-zero one is woken
  by one request for its model list, which generates no tokens) or scale it to zero now (`scale_to_zero`).
  Both are idempotent: an endpoint already in the wanted state is left alone.
"""

import time
from dataclasses import dataclass
from typing import Literal

import httpx

from aiva_gateway.catalog.models import ModelSpec
from aiva_gateway.inference.base import (
    CONNECTION_ERROR,
    ENDPOINT_COLD_START,
    ENDPOINT_NOT_CONFIGURED,
    ENDPOINT_REVISION_MISMATCH,
    ENDPOINT_UNAVAILABLE,
    ProviderCallError,
)

ENDPOINTS_API = "https://api.endpoints.huggingface.cloud/v2/endpoint"
_STARTING = frozenset({"pending", "initializing", "updating", "scaledToZero"})
_WAKING = frozenset({"pending", "initializing", "updating"})
AWAKE_TTL_SECONDS = 30.0
# The live demo page polls node state; within this many seconds a reading reuses the last HF API answer.
READING_TTL_SECONDS = 10.0
WAKE_REQUEST_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class EndpointStatus:
    model_id: str
    state: str
    url: str | None
    source: str  # "override" | "hf-api"
    checked_at: float
    repository: str | None = None
    revision: str | None = None


@dataclass(frozen=True)
class ResolvedEndpoint:
    url: str
    # `ModelSpec.pinned_version` when the HF Endpoints API confirmed the pinned weights; None for an override.
    verified_version: str | None


NodeState = Literal["ready", "waking", "asleep", "unavailable"]
EndpointAction = Literal["none", "resume", "wake-request", "scale-to-zero"]


@dataclass(frozen=True)
class EndpointReading:
    """An endpoint as the live demo page shows it: one of four states, a short reason, and the HF state."""

    model_id: str
    state: NodeState
    detail: str
    hf_state: str | None = None
    action: EndpointAction = "none"


def _reading(status: EndpointStatus) -> EndpointReading:
    hf_state = status.state
    state: NodeState
    if hf_state == "running":
        state, detail = "ready", "running"
    elif hf_state in _WAKING:
        state, detail = "waking", f"starting ({hf_state})"
    elif hf_state == "scaledToZero":
        state, detail = "asleep", "scaled to zero"
    elif hf_state == "paused":
        state, detail = "asleep", "paused"
    else:
        state, detail = "unavailable", f"endpoint state {hf_state}"
    return EndpointReading(status.model_id, state, detail, hf_state)


class EndpointResolver:
    def __init__(
        self,
        *,
        overrides: dict[str, str],
        namespace: str | None,
        token: str | None,
        http: httpx.AsyncClient | None = None,
        ttl_seconds: float = 300.0,
    ) -> None:
        self._overrides = {k: v.rstrip("/") for k, v in overrides.items()}
        self._namespace = namespace
        self._token = token
        self._http = http
        self._ttl = ttl_seconds
        self._cache: dict[str, EndpointStatus] = {}
        self._awake: dict[str, tuple[float, bool]] = {}

    async def status(self, model: ModelSpec) -> EndpointStatus:
        now = time.monotonic()
        if model.id in self._overrides:
            return EndpointStatus(model.id, "configured", self._overrides[model.id], "override", now)
        cached = self._cache.get(model.id)
        if cached and now - cached.checked_at < self._ttl and cached.state == "running":
            return cached
        return await self._fetch(model)

    async def _fetch(self, model: ModelSpec) -> EndpointStatus:
        """The endpoint's state from the HF Endpoints API (never the endpoint itself)."""
        now = time.monotonic()
        if not (self._namespace and self._token and model.endpoint_name and self._http):
            raise ProviderCallError(ENDPOINT_NOT_CONFIGURED, retryable=False)
        try:
            response = await self._http.get(
                f"{ENDPOINTS_API}/{self._namespace}/{model.endpoint_name}",
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=10.0,
            )
        except httpx.HTTPError as exc:
            raise ProviderCallError(CONNECTION_ERROR, retryable=True) from exc
        if response.status_code == 404:
            raise ProviderCallError(ENDPOINT_NOT_CONFIGURED, retryable=False, status_code=404)
        if response.status_code != 200:
            raise ProviderCallError(ENDPOINT_UNAVAILABLE, retryable=True, status_code=response.status_code)
        body = response.json()
        state, served = body.get("status") or {}, body.get("model") or {}
        status = EndpointStatus(
            model.id,
            str(state.get("state", "unknown")),
            state.get("url"),
            "hf-api",
            now,
            repository=served.get("repository"),
            revision=served.get("revision"),
        )
        self._cache[model.id] = status
        return status

    async def is_awake(self, model: ModelSpec, *, ttl_seconds: float = AWAKE_TTL_SECONDS) -> bool:
        """Whether a call would reach a running endpoint without waking it. Reads the HF Endpoints API (never
        the endpoint), cached briefly. A URL override cannot be scaled to zero by HF, and an endpoint that is
        not configured fails closed on its own, so both count as awake; an API error counts as not awake."""
        if model.id in self._overrides or not (self._namespace and self._token and self._http):
            return True
        now = time.monotonic()
        cached = self._awake.get(model.id)
        if cached and now - cached[0] < ttl_seconds:
            return cached[1]
        try:
            awake = (await self._fetch(model)).state == "running"  # fresh: the 300 s cache may be stale
        except ProviderCallError:
            awake = False
        self._awake[model.id] = (now, awake)
        return awake

    @property
    def managed(self) -> bool:
        """Whether endpoint state is read and managed through the HF Endpoints API (a live gateway)."""
        return bool(self._namespace and self._token and self._http)

    async def reading(self, model: ModelSpec) -> EndpointReading:
        """The endpoint's state for the live demo page, from the HF Endpoints API (never the endpoint)."""
        if model.id in self._overrides:
            return EndpointReading(model.id, "ready", "URL override: always on, not managed here")
        if not self.managed or not model.endpoint_name:
            return EndpointReading(model.id, "unavailable", "endpoint not configured on this gateway")
        cached = self._cache.get(model.id)
        if cached is not None and time.monotonic() - cached.checked_at < READING_TTL_SECONDS:
            return _reading(cached)
        try:
            status = await self._fetch(model)
        except ProviderCallError as exc:
            return EndpointReading(model.id, "unavailable", f"HF Endpoints API: {exc.code}")
        return _reading(status)

    async def wake(self, model: ModelSpec) -> EndpointReading:
        """Wake the endpoint without generating tokens. Paused: resumed through the HF Endpoints API. Scaled
        to zero: one request for its model list, which HF answers with 503 while it scales up. Running or
        already starting: nothing to do."""
        current = await self.reading(model)
        if current.hf_state not in {"paused", "scaledToZero"}:
            return current
        assert self._http is not None and self._token is not None
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            if current.hf_state == "paused":
                response = await self._http.post(
                    f"{ENDPOINTS_API}/{self._namespace}/{model.endpoint_name}/resume",
                    headers=headers,
                    timeout=10.0,
                )
                action: EndpointAction = "resume"
                accepted = response.status_code < 300
            else:
                url = (self._cache[model.id].url or "").rstrip("/")
                if not url:
                    return EndpointReading(
                        model.id, "unavailable", "no endpoint URL to wake", current.hf_state
                    )
                response = await self._http.get(
                    f"{url}/v1/models", headers=headers, timeout=WAKE_REQUEST_TIMEOUT_SECONDS
                )
                action = "wake-request"
                accepted = response.status_code < 300 or response.status_code == 503
        except httpx.TimeoutException:
            # The request reached HF, which holds it while the endpoint scales up: the wake is under way.
            action, accepted = ("resume" if current.hf_state == "paused" else "wake-request"), True
        except httpx.HTTPError:
            return EndpointReading(model.id, "unavailable", "wake request failed", current.hf_state)
        self._forget(model)
        if not accepted:
            return EndpointReading(
                model.id, "unavailable", f"wake refused ({response.status_code})", current.hf_state, action
            )
        return EndpointReading(model.id, "waking", "wake requested", current.hf_state, action)

    async def scale_to_zero(self, model: ModelSpec) -> EndpointReading:
        """Scale the endpoint to zero now through the HF Endpoints API. Already asleep: nothing to do."""
        current = await self.reading(model)
        if current.hf_state not in {"running", *_WAKING}:
            return current
        assert self._http is not None and self._token is not None
        try:
            response = await self._http.post(
                f"{ENDPOINTS_API}/{self._namespace}/{model.endpoint_name}/scale-to-zero",
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=10.0,
            )
        except httpx.HTTPError:
            return EndpointReading(model.id, current.state, "scale to zero failed", current.hf_state)
        self._forget(model)
        if response.status_code >= 300:
            return EndpointReading(
                model.id,
                current.state,
                f"scale to zero refused ({response.status_code})",
                current.hf_state,
                "scale-to-zero",
            )
        return EndpointReading(model.id, "asleep", "scaled to zero", "scaledToZero", "scale-to-zero")

    def _forget(self, model: ModelSpec) -> None:
        """Drop cached state after a wake or scale-down, so the next reading and run see HF's own state."""
        self._cache.pop(model.id, None)
        self._awake.pop(model.id, None)

    async def resolve(self, model: ModelSpec) -> ResolvedEndpoint:
        status = await self.status(model)
        verified: str | None = None
        if status.source == "hf-api":
            if (status.repository, status.revision) != model.served_weights:
                raise ProviderCallError(ENDPOINT_REVISION_MISMATCH, retryable=False)
            verified = model.pinned_version
        if status.url and (status.state in _STARTING or status.state in {"running", "configured"}):
            return ResolvedEndpoint(status.url.rstrip("/"), verified)
        if status.state in _STARTING:
            raise ProviderCallError(ENDPOINT_COLD_START, retryable=True)
        raise ProviderCallError(ENDPOINT_UNAVAILABLE, retryable=False)
