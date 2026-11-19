"""Tracing setup. Spans are captured per trace for persistence; OTLP export is optional."""

import os
import threading
from collections import defaultdict
from typing import Any

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

STAGES = (
    "ingress",
    "policy",
    "cache",
    "rlcd1",
    "retrieval",
    "cim",
    "context_guard",
    "rlcd2",
    "quota",
    "preflight",
    "inference",
    "assurance",
    "reconciliation",
)

_ATTRIBUTE_PREFIX = "aiva."
_MAX_ATTRIBUTE_LENGTH = 200


def redact_attributes(attributes: Any) -> dict[str, Any]:
    """Keep only allowlisted `aiva.*` attributes with short primitive values. Prompts never qualify."""
    safe: dict[str, Any] = {}
    for key, value in dict(attributes or {}).items():
        if not key.startswith(_ATTRIBUTE_PREFIX):
            continue
        if isinstance(value, str):
            safe[key] = value[:_MAX_ATTRIBUTE_LENGTH]
        elif isinstance(value, bool | int | float):
            safe[key] = value
        elif isinstance(value, tuple | list) and all(isinstance(v, str | bool | int | float) for v in value):
            safe[key] = [v[:_MAX_ATTRIBUTE_LENGTH] if isinstance(v, str) else v for v in value][:50]
    return safe


class RunSpanCollector(SpanProcessor):
    """Holds finished spans in memory per trace until the recorder takes them."""

    def __init__(self) -> None:
        self._spans: dict[int, list[ReadableSpan]] = defaultdict(list)
        self._lock = threading.Lock()

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        return None

    def on_end(self, span: ReadableSpan) -> None:
        if span.context is None:
            return
        with self._lock:
            self._spans[span.context.trace_id].append(span)

    def take(self, trace_id: int) -> list[ReadableSpan]:
        with self._lock:
            return self._spans.pop(trace_id, [])

    def shutdown(self) -> None:
        with self._lock:
            self._spans.clear()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


_provider: TracerProvider | None = None
_collector = RunSpanCollector()


def get_collector() -> RunSpanCollector:
    return _collector


def get_tracer() -> trace.Tracer:
    global _provider
    if _provider is None:
        _provider = TracerProvider()
        _provider.add_span_processor(_collector)
        if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            _provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    return _provider.get_tracer("aiva_gateway")


def format_trace_id(trace_id: int) -> str:
    return format(trace_id, "032x")


def format_span_id(span_id: int) -> str:
    return format(span_id, "016x")
