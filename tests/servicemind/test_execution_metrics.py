"""Operational metrics must remain bounded even when runs carry arbitrary identifiers."""

from servicemind.observability import metrics as observed
from servicemind.observability.tracing import _metrics_endpoint


class _Histogram:
    def __init__(self) -> None:
        self.rows: list[tuple[float, dict[str, str] | None]] = []

    def record(self, value: float, attributes: dict[str, str] | None = None) -> None:
        self.rows.append((value, attributes))


class _Counter:
    def __init__(self) -> None:
        self.rows: list[tuple[int, dict[str, str]]] = []

    def add(self, value: int, attributes: dict[str, str]) -> None:
        self.rows.append((value, attributes))


def test_execution_metrics_never_export_run_or_tenant_identifiers(monkeypatch) -> None:
    run, queue, route, recovery = _Histogram(), _Histogram(), _Histogram(), _Counter()
    monkeypatch.setattr(observed, "_run_duration", run)
    monkeypatch.setattr(observed, "_queue_wait", queue)
    monkeypatch.setattr(observed, "_route_duration", route)
    monkeypatch.setattr(observed, "_recovery_total", recovery)

    observed.record_run_execution(1.2, "tenant-secret")
    observed.record_queue_wait(0.4)
    observed.record_route(0.1, "INC-1234", "token-secret")
    observed.record_recovery("run-secret")

    assert run.rows == [(1.2, {"outcome": "other"})]
    assert queue.rows == [(0.4, None)]
    assert route.rows == [(0.1, {"route": "other", "source": "other"})]
    assert recovery.rows == [(1, {"outcome": "other"})]


def test_metrics_endpoint_uses_same_collector_as_traces() -> None:
    assert (
        _metrics_endpoint("http://collector:4318/v1/traces") == "http://collector:4318/v1/metrics"
    )
    assert _metrics_endpoint("http://collector:4318/otlp/v1/traces") == (
        "http://collector:4318/otlp/v1/metrics"
    )
