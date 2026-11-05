import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from core import settings

tracer = trace.get_tracer("servicemind.phase2")
_provider: TracerProvider | None = None
_metric_provider: MeterProvider | None = None


def _metrics_endpoint(traces_endpoint: str) -> str:
    """Use the sibling OTLP/HTTP metrics path for the configured trace collector."""
    parts = urlsplit(traces_endpoint)
    path = parts.path.rstrip("/")
    if path.endswith("/v1/traces"):
        path = path[: -len("/v1/traces")]
    return urlunsplit(
        (parts.scheme, parts.netloc, f"{path}/v1/metrics", parts.query, parts.fragment)
    )


def configure_telemetry() -> bool:
    """Enable batched OTLP export when an endpoint is configured.

    Headers, compression and TLS certificate settings follow the standard
    OpenTelemetry environment variables supported by OTLPSpanExporter.
    """
    global _provider, _metric_provider, tracer
    if not settings.SERVICEMIND_OTEL_ENDPOINT:
        return False
    if _provider is not None:
        return True
    resource = Resource.create({SERVICE_NAME: settings.SERVICEMIND_OTEL_SERVICE_NAME})
    _provider = TracerProvider(resource=resource)
    _provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.SERVICEMIND_OTEL_ENDPOINT))
    )
    trace.set_tracer_provider(_provider)
    tracer = trace.get_tracer("servicemind.phase2")
    _metric_provider = MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(
                    endpoint=os.environ.get("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT")
                    or _metrics_endpoint(settings.SERVICEMIND_OTEL_ENDPOINT)
                ),
                export_interval_millis=30_000,
            )
        ],
    )
    metrics.set_meter_provider(_metric_provider)
    return True


def shutdown_telemetry() -> None:
    global _provider, _metric_provider
    if _metric_provider is not None:
        _metric_provider.shutdown()
        _metric_provider = None
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
