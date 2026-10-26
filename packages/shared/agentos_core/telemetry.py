"""OpenTelemetry setup. Exports via OTLP/HTTP only when explicitly enabled.

Span attributes carry identifiers (run id, node key, tool name, model) and
counts, never prompt or document content, so exported traces are privacy-aware.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from agentos_core.config import get_settings

_configured = False


def configure_tracing(service: str) -> None:
    global _configured
    if _configured:
        return
    settings = get_settings()
    provider = TracerProvider(
        resource=Resource.create({"service.name": f"{settings.otel_service_name}-{service}"})
    )
    if settings.otel_enabled and settings.otel_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{settings.otel_endpoint.rstrip('/')}/v1/traces"))
        )
    # Without an exporter spans are still created, so trace ids are recorded on events.
    trace.set_tracer_provider(provider)
    _configured = True
