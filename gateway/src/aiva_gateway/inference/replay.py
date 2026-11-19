"""Per-run replay (ADR-0019): recorded exchanges only, never the network.

The gateway builds this adapter set at startup next to the configured one, so each run can execute in
`replay` whatever the process's `AIVA_INFERENCE_MODE`.

- Exchanges come from the recordings shipped with the gateway (ADR-0009), through replay transports and
  placeholder credentials. Nothing in this set can open a connection.
- A replay miss (for example a free-text prompt) is answered by the simulated adapter for the same model,
  labelled `simulated`. It is not a fallback to another model: the model, the policy checks before the
  call and its metering are unchanged.
- Endpoints A/B resolve to the URL their exchanges were recorded against, read from the recordings. The HF
  Endpoints API is never called; an endpoint without recordings is a replay miss.

The configured set stays strict: when the process itself runs in `replay` (CI), a miss fails, so stale
recordings are caught.
"""

import json
import time
from pathlib import Path
from urllib.parse import urlsplit

from aiva_gateway.catalog.models import ModelSpec
from aiva_gateway.config import Settings
from aiva_gateway.contracts.cim import CimRequest
from aiva_gateway.inference.base import REPLAY_MISS, ReplayMissError
from aiva_gateway.inference.endpoints import EndpointResolver, EndpointStatus
from aiva_gateway.pipeline.interfaces import (
    CimCallResult,
    ContextIntelligence,
    InferenceAdapter,
    InferenceRequest,
    InferenceResult,
)


def replay_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"inference_mode": "replay"})


class RecordedEndpointResolver(EndpointResolver):
    """Endpoints A/B in replay: the URL their exchanges were recorded against. Never the HF Endpoints API."""

    def __init__(self, recordings_dir: Path, *, overrides: dict[str, str]) -> None:
        super().__init__(overrides=overrides, namespace=None, token=None, http=None)
        self._recordings_dir = recordings_dir
        self._recorded: dict[str, str | None] = {}

    def _recorded_url(self, model_id: str) -> str | None:
        if model_id not in self._recorded:
            url = None
            for path in sorted(self._recordings_dir.glob(f"{model_id}-*.json")):
                recorded = str(json.loads(path.read_text(encoding="utf-8"))["request"]["url"])
                parts = urlsplit(recorded)
                url = f"{parts.scheme}://{parts.netloc}"
                break
            self._recorded[model_id] = url
        return self._recorded[model_id]

    async def status(self, model: ModelSpec) -> EndpointStatus:
        if model.id in self._overrides:
            return await super().status(model)
        url = self._recorded_url(model.id)
        if url is None:
            raise ReplayMissError(f"no recordings for endpoint {model.id}")
        return EndpointStatus(model.id, "configured", url, "recording", time.monotonic())


class ReplayOrSimulated:
    """A recorded exchange when there is one, else the simulated adapter's answer (labelled `simulated`)."""

    def __init__(self, replay: InferenceAdapter, simulated: InferenceAdapter) -> None:
        self._replay = replay
        self._simulated = simulated

    async def complete(self, request: InferenceRequest) -> InferenceResult:
        result = await self._replay.complete(request)
        if result.status != "ok" and result.error_code == REPLAY_MISS:
            return await self._simulated.complete(request)
        return result

    async def aclose(self) -> None:
        close = getattr(self._replay, "aclose", None)
        if close is not None:
            await close()


class ReplayOrSimulatedCim:
    """The CIM's recorded package when there is one, else the simulated CIM's (labelled `simulated`)."""

    def __init__(self, replay: ContextIntelligence, simulated: ContextIntelligence) -> None:
        self._replay = replay
        self._simulated = simulated

    async def package(self, request: CimRequest, model: ModelSpec) -> CimCallResult:
        result = await self._replay.package(request, model)
        if result.response is None and result.error_code == REPLAY_MISS:
            return await self._simulated.package(request, model)
        return result

    async def aclose(self) -> None:
        close = getattr(self._replay, "aclose", None)
        if close is not None:
            await close()
