"""OpenTelemetry tracing with W3C ``traceparent`` propagation.

Traces must survive *asynchronous* hops, not just HTTP calls: when a request
publishes an event or enqueues a task, the current ``traceparent`` is copied into
the message, and the consumer continues the same trace. One trace therefore shows
the whole story - API call -> queue -> worker -> LLM call -> billing.
"""

from __future__ import annotations

from collections.abc import Mapping

from opentelemetry import propagate, trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter


def build_tracer_provider(
    service_name: str,
    *,
    environment: str = "local",
    otlp_endpoint: str | None = None,
    exporter: SpanExporter | None = None,
) -> TracerProvider:
    """Create a tracer provider for one service.

    Each service gets its *own* provider (rather than mutating the global one) so
    several services can share a process in the devstack and still report the
    correct ``service.name``.
    """
    resource = Resource.create(
        {"service.name": service_name, "deployment.environment": environment}
    )
    provider = TracerProvider(resource=resource)
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    if otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        endpoint = otlp_endpoint.rstrip("/") + "/v1/traces"
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    return provider


def inject_trace_headers(headers: dict[str, str]) -> dict[str, str]:
    """Add ``traceparent``/``tracestate`` for the current span to outgoing headers."""
    propagate.inject(headers)
    return headers


def current_traceparent() -> str | None:
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier.get("traceparent")


def extract_context(carrier: Mapping[str, str]) -> Context:
    return propagate.extract(dict(carrier))


def context_from_traceparent(traceparent: str | None) -> Context | None:
    if not traceparent:
        return None
    return propagate.extract({"traceparent": traceparent})


def current_trace_ids() -> tuple[str | None, str | None]:
    """Return ``(trace_id, span_id)`` of the active span as hex, for log correlation."""
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None, None
    return f"{span_context.trace_id:032x}", f"{span_context.span_id:016x}"
