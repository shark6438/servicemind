from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class RequestLimitsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = scope.get("headers", [])
            size = sum(len(k) + len(v) for k, v in headers)
            size += len(scope.get("raw_path", b"")) + len(scope.get("query_string", b""))
            if len(headers) > 100 or size > 16_384:
                await JSONResponse({"detail": "Request headers are too large"}, status_code=431)(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)
