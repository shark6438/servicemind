from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from core import settings

tracer = trace.get_tracer("servicemind.phase2")
_provider: TracerProvider | None = None


def configure_telemetry() -> bool:
    """Enable batched OTLP export when an endpoint is configured.

    Headers, compression and TLS certificate settings follow the standard
    OpenTelemetry environment variables supported by OTLPSpanExporter.
    """
    global _provider, tracer
    if not settings.SERVICEMIND_OTEL_ENDPOINT:
        return False
    if _provider is not None:
        return True
    _provider = TracerProvider(
        resource=Resource.create({SERVICE_NAME: settings.SERVICEMIND_OTEL_SERVICE_NAME})
    )
    _provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.SERVICEMIND_OTEL_ENDPOINT))
    )
    trace.set_tracer_provider(_provider)
    tracer = trace.get_tracer("servicemind.phase2")
    return True


def shutdown_telemetry() -> None:
    global _provider
    if _provider is not None:
        _provider.shutdown()
        _provider = None


@contextmanager
def phase_span(name: str, **attributes: Any) -> Iterator[trace.Span]:
    """Create a redacted span with stable Agent/ITSM correlation attributes."""
    with tracer.start_as_current_span(name) as span:
        for key, value in attributes.items():
            if value is not None:
                span.set_attribute(key, value)
        yield span
