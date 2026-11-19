"""Simulated inference: deterministic placeholder output labelled `simulated`, never shown as measured."""

import time

from aiva_gateway.core.canonical import estimate_tokens
from aiva_gateway.core.enums import Provenance
from aiva_gateway.pipeline.interfaces import InferenceRequest, InferenceResult, TokenUsage


class SimulatedInferenceAdapter:
    def __init__(self, *, unavailable_models: set[str] | None = None) -> None:
        self.unavailable_models = unavailable_models or set()

    async def complete(self, request: InferenceRequest) -> InferenceResult:
        started = time.perf_counter()
        input_tokens = sum(estimate_tokens(m["content"]) for m in request.messages)
        if request.model.id in self.unavailable_models:
            return InferenceResult(
                status="error",
                content="",
                finish_reason=None,
                usage=TokenUsage(provenance=Provenance.SIMULATED),
                latency_ms=(time.perf_counter() - started) * 1000,
                ttft_ms=None,
                provider_model_version=request.model.revision,
                provenance=Provenance.SIMULATED,
                error_code="ENDPOINT_UNAVAILABLE",
                retryable=True,
            )
        content = (
            f"[Simulated response from {request.model.display_name}] This placeholder stands in for a live "
            f"model call; no provider was contacted. The request carried {len(request.messages)} messages "
            f"(about {input_tokens} tokens)."
        )
        output_tokens = min(estimate_tokens(content), request.max_output_tokens)
        elapsed = (time.perf_counter() - started) * 1000
        return InferenceResult(
            status="ok",
            content=content,
            finish_reason="stop",
            usage=TokenUsage(
                input_tokens=input_tokens, output_tokens=output_tokens, provenance=Provenance.SIMULATED
            ),
            latency_ms=elapsed,
            ttft_ms=elapsed,
            provider_model_version=request.model.revision,
            provenance=Provenance.SIMULATED,
        )
