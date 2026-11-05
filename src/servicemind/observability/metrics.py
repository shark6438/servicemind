"""Low-cardinality operational measurements for the Agent execution path.

Identifiers, request text and tenant names are deliberately excluded from metric
attributes. Traces and the audit ledger carry the per-run detail.
"""

from opentelemetry import metrics

_meter = metrics.get_meter("servicemind.execution")

_run_duration = _meter.create_histogram(
    "servicemind.run.duration", unit="s", description="Time spent executing an Agent run"
)
_queue_wait = _meter.create_histogram(
    "servicemind.run.queue_wait", unit="s", description="Time from admission to execution"
)
_recovery_total = _meter.create_counter(
    "servicemind.run.recovery", description="Run recovery outcomes"
)
_route_duration = _meter.create_histogram(
    "servicemind.route.duration", unit="s", description="Time spent deciding a route"
)
_http_response_start = _meter.create_histogram(
    "servicemind.http.response_start.duration",
    unit="s",
    description="Time until HTTP response headers, excluding background execution",
)


def record_run_execution(seconds: float, outcome: str) -> None:
    if seconds >= 0:
        _run_duration.record(
            seconds, {"outcome": _bounded(outcome, {"succeeded", "failed", "paused", "cancelled"})}
        )


def record_queue_wait(seconds: float) -> None:
    if seconds >= 0:
        _queue_wait.record(seconds)


def record_recovery(outcome: str) -> None:
    _recovery_total.add(1, {"outcome": _bounded(outcome, {"recovered", "blocked", "failed"})})


def record_route(seconds: float, route: str, source: str) -> None:
    if seconds >= 0:
        _route_duration.record(
            seconds,
            {
                "route": _bounded(
                    route,
                    {
                        "simple_data_query",
                        "simple_knowledge_query",
                        "complex_workflow",
                        "unsupported",
                    },
                ),
                "source": _bounded(
                    source,
                    {"rule", "jev_confirmed", "jev_override", "rule_fallback", "rule_shadow"},
                ),
            },
        )


def record_http_response_start(seconds: float, method: str, route: str, status_code: int) -> None:
    if seconds >= 0:
        _http_response_start.record(
            seconds,
            {
                "http.request.method": _bounded(
                    method, {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
                ),
                "http.route": route,
                "http.response.status_code": status_code,
            },
        )


def _bounded(value: str, allowed: set[str]) -> str:
    return value if value in allowed else "other"
