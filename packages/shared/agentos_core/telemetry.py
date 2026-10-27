"""OpenTelemetry setup. Exports via OTLP/HTTP only when explicitly enabled.

Span attributes carry identifiers (run id, node key, tool name, model) and
counts, never prompt or document content, so exported traces are privacy-aware.
"""

from __future__ import annotations

from typing import Any

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


def context_from_traceparent(traceparent: str | None) -> Any:
    """An OpenTelemetry context whose current span is the remote span named by a W3C ``traceparent`` (or None)."""
    if not traceparent:
        return None
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    ctx = TraceContextTextMapPropagator().extract({"traceparent": traceparent})
    return ctx if trace.get_current_span(ctx).get_span_context().is_valid else None


def link_to(traceparent: str | None) -> list[Any]:
    """A span link to a run's root span, for operations that happen in a different trace (approvals, deployments)."""
    ctx = context_from_traceparent(traceparent)
    return [trace.Link(trace.get_current_span(ctx).get_span_context())] if ctx is not None else []
