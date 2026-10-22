import asyncio
import ipaddress
import os
import re
from urllib.parse import urlparse
from uuid import UUID

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response

from servicemind.harness.webhooks import WebhookValidationError, validate_glpi_headers
from servicemind.interfaces.http.request_limits import RequestLimitsMiddleware
from servicemind.interfaces.http.webhook_limits import WebhookAdmission

WEBHOOK_PATH = "/v1/servicemind/webhooks/glpi"


def create_proxy(
    upstream: str = "http://127.0.0.1:18080",
    allowed_cidr: str = "127.0.0.1/32",
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    parsed = urlparse(upstream)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("Webhook proxy upstream must be a loopback HTTP origin")
    network = ipaddress.ip_network(allowed_cidr, strict=True)
    admission = WebhookAdmission()
    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    application.add_middleware(RequestLimitsMiddleware)

    @application.api_route(
        WEBHOOK_PATH + "/{tenant_id}", methods=["GET", "POST"], dependencies=[Depends(admission)]
    )
    @application.post(WEBHOOK_PATH, dependencies=[Depends(admission)])
    async def forward(request: Request):
        if request.client is None or ipaddress.ip_address(request.client.host) not in network:
            raise HTTPException(403, "Webhook source is outside the configured network")
        target_path = WEBHOOK_PATH
        if "tenant_id" in request.path_params:
            try:
                tenant_id = UUID(request.path_params["tenant_id"])
            except ValueError as exc:
                raise HTTPException(404, "Unknown webhook path") from exc
            target_path += f"/{tenant_id}"
        headers = {"Content-Type": "application/json"}
        params = {}
        if request.method == "GET":
            pairs = list(request.query_params.multi_items())
            if (
                len(pairs) != 1
                or pairs[0][0] != "crc_token"
                or re.fullmatch(r"[0-9a-f]{64}", pairs[0][1]) is None
            ):
                raise HTTPException(401, "Invalid webhook challenge")
            params["crc_token"] = pairs[0][1]
        else:
            if request.query_params:
                raise HTTPException(400, "Unexpected webhook query")
            timestamp = request.headers.get("X-GLPI-timestamp", "")
            signature = request.headers.get("X-GLPI-signature", "")
            try:
                validate_glpi_headers(timestamp, signature)
            except WebhookValidationError as exc:
                raise HTTPException(401, "Invalid webhook headers") from exc
            headers.update({"X-GLPI-timestamp": timestamp, "X-GLPI-signature": signature})
        body = bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > 65_536:
                        raise HTTPException(413, "Webhook payload is too large")
                    body.extend(chunk)
        except TimeoutError as exc:
            raise HTTPException(408, "Webhook body timed out") from exc
        try:
            async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False, timeout=10, transport=transport
            ) as client:
                response = await client.request(
                    request.method,
                    upstream.rstrip("/") + target_path,
                    content=bytes(body),
                    headers=headers,
                    params=params,
                )
        except httpx.HTTPError as exc:
            raise HTTPException(503, "Webhook upstream is unavailable") from exc
        if response.is_redirect or len(response.content) > 65_536:
            raise HTTPException(502, "Invalid webhook upstream response")
        return Response(
            content=response.content,
            status_code=response.status_code,
            media_type="text/plain" if request.method == "GET" else "application/json",
        )

    return application


def main() -> None:
    application = create_proxy(
        upstream=os.getenv("SERVICEMIND_WEBHOOK_PROXY_UPSTREAM", "http://127.0.0.1:18080"),
        allowed_cidr=os.getenv("SERVICEMIND_WEBHOOK_PROXY_ALLOWED_CIDR", "127.0.0.1/32"),
    )
    uvicorn.run(
        application,
        host=os.getenv("SERVICEMIND_WEBHOOK_PROXY_BIND_HOST", "127.0.0.1"),
        port=int(os.getenv("SERVICEMIND_WEBHOOK_PROXY_PORT", "18082")),
        http="h11",
        h11_max_incomplete_event_size=16_384,
        access_log=False,
    )


if __name__ == "__main__":
    main()
