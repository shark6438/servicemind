"""Measure API admission separately from streaming and background task duration."""

import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from servicemind.observability.metrics import record_http_response_start


class HttpMetricsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        recorded = False

        def record(status_code: int) -> None:
            # The framework's route template is bounded by the application code;
            # the raw path can contain arbitrary ticket IDs or request data.
            route = getattr(scope.get("route"), "path", "unmatched")
            record_http_response_start(
                time.perf_counter() - started, scope.get("method", "OTHER"), route, status_code
            )

        async def measured_send(message: Message) -> None:
            nonlocal recorded
            if message["type"] == "http.response.start" and not recorded:
                recorded = True
                record(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, measured_send)
        except Exception:
            if not recorded:
                record(500)
            raise
