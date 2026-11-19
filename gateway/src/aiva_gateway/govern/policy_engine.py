"""OPA client. Every failure to obtain a decision fails closed (LLD §17)."""

from typing import Any

import httpx

from aiva_gateway.contracts.policy import PolicyDecision, PolicyInput
from aiva_gateway.core.errors import DependencyUnavailableError

DECISION_PATH = "/v1/data/aiva/decision"
SIMULATE_PATH = "/v1/data/aiva/simulate"
BUNDLE_PATH = "/v1/data/aiva_bundle"
_UNAVAILABLE_CODE = "POLICY_ENGINE_UNAVAILABLE"
_UNAVAILABLE_REMEDIATION = "Retry later. No model was called."


class OpaPolicyEngine:
    def __init__(self, base_url: str, timeout_seconds: float) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout_seconds)

    async def evaluate(self, policy_input: PolicyInput) -> PolicyDecision:
        return await self._decide(DECISION_PATH, {"input": policy_input.model_dump(mode="json")})

    async def simulate(self, bundle: dict[str, Any], policy_input: PolicyInput) -> PolicyDecision:
        """Evaluate a candidate bundle (draft or archived) without loading it (policy simulator)."""
        payload = {"input": {"bundle": bundle, "case": policy_input.model_dump(mode="json")}}
        return await self._decide(SIMULATE_PATH, payload)

    async def _decide(self, path: str, payload: dict[str, Any]) -> PolicyDecision:
        try:
            response = await self._client.post(path, json=payload)
            response.raise_for_status()
            result = response.json().get("result")
        except (httpx.HTTPError, ValueError) as exc:
            raise DependencyUnavailableError(
                "The policy engine is unavailable; the request was not processed.",
                code=_UNAVAILABLE_CODE,
                remediation=_UNAVAILABLE_REMEDIATION,
            ) from exc
        if not isinstance(result, dict):
            raise DependencyUnavailableError(
                "The policy engine returned no decision; the request was not processed.",
                code=_UNAVAILABLE_CODE,
                remediation=_UNAVAILABLE_REMEDIATION,
            )
        return PolicyDecision.model_validate(result)

    async def publish_bundle(self, bundle: dict[str, Any]) -> None:
        try:
            response = await self._client.put(BUNDLE_PATH, json=bundle)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise DependencyUnavailableError(
                "The policy engine is unavailable; the policy version was not published.",
                code="POLICY_ENGINE_UNAVAILABLE",
                remediation="Retry when the policy engine is healthy. Nothing was changed.",
            ) from exc

    async def loaded_bundle_hash(self) -> str | None:
        response = await self._client.get(f"{BUNDLE_PATH}/meta/bundle_hash")
        response.raise_for_status()
        value = response.json().get("result")
        return value if isinstance(value, str) else None

    async def ready(self) -> bool:
        try:
            response = await self._client.get("/health")
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def aclose(self) -> None:
        await self._client.aclose()
