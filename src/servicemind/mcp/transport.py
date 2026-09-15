from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from servicemind.tool_platform.contracts import (
    ProviderResult,
    ToolCall,
    ToolDefinition,
    output_hash,
)

MCP_PROTOCOL_VERSION = "2026-07-28"
TASK_EXTENSION = "io.modelcontextprotocol/tasks"


class StatelessMcpClient:
    """Stateless MCP HTTP client with issuer pinning and no legacy SSE/session path."""

    def __init__(
        self,
        base_url: str,
        *,
        expected_issuer: str,
        token_provider: Callable[[], Awaitable[str]],
        timeout_seconds: float = 20,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("remote MCP endpoints require HTTPS")
        if not expected_issuer:
            raise ValueError("MCP issuer pin is required")
        self.base_url = base_url.rstrip("/")
        self.expected_issuer = expected_issuer
        self.token_provider = token_provider
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def request(
        self, method: str, *, name: str | None, params: dict[str, Any]
    ) -> dict[str, Any]:
        token = await self.token_provider()
        meta = params.setdefault("_meta", {})
        if not isinstance(meta, dict):
            raise ValueError("MCP params._meta must be an object")
        meta.setdefault("io.modelcontextprotocol/protocolVersion", MCP_PROTOCOL_VERSION)
        meta.setdefault(
            "io.modelcontextprotocol/clientInfo",
            {"name": "servicemind-mcp-provider", "version": "1.0.0"},
        )
        meta.setdefault(
            "io.modelcontextprotocol/clientCapabilities",
            {"extensions": {TASK_EXTENSION: {}}},
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Mcp-Protocol-Version": MCP_PROTOCOL_VERSION,
            "Mcp-Method": method,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if name:
            headers["Mcp-Name"] = name
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            transport=self.transport,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            response = await client.post(
                self.base_url,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": str(uuid4()),
                    "method": method,
                    "params": params,
                },
            )
        if response.is_redirect:
            raise PermissionError("MCP redirect refused")
        response.raise_for_status()
        if response.headers.get("Mcp-Session-Id"):
            raise ValueError("stateful MCP session response is not accepted")
        if response.headers.get("Mcp-Server-Issuer") != self.expected_issuer:
            raise PermissionError("MCP server issuer mismatch")
        if response.headers.get("Mcp-Protocol-Version") != MCP_PROTOCOL_VERSION:
            raise ValueError("MCP protocol version mismatch")
        envelope = response.json()
        if not isinstance(envelope, dict):
            raise ValueError("MCP response must be an object")
        if "error" in envelope:
            error = envelope["error"]
            message = error.get("message", "MCP request failed") if isinstance(error, dict) else "MCP request failed"
            raise RuntimeError(str(message))
        payload = envelope.get("result")
        if not isinstance(payload, dict) or "resultType" not in payload:
            raise ValueError("MCP response has no typed result")
        return payload


class McpGlpiProvider:
    name = "mcp_glpi"

    def __init__(self, client: StatelessMcpClient) -> None:
        self.client = client

    async def execute(self, definition: ToolDefinition, call: ToolCall) -> ProviderResult:
        payload = await self.client.request(
            "tools/call",
            name=definition.name.removeprefix("glpi."),
            params={
                "name": definition.name.removeprefix("glpi."),
                "arguments": call.arguments,
                "_meta": {
                    "com.servicemind/execution": {
                        "requestId": str(call.request_id),
                        "runId": str(call.run_id),
                        "taskId": call.task_id,
                        "deadline": call.deadline.isoformat(),
                    }
                },
            },
        )
        if payload.get("resultType") == "task":
            payload = await self._await_task(payload, call.deadline)
        output = payload.get("structuredContent")
        receipt = payload.get("_meta", {}).get("com.servicemind/receipt", {})
        if output is None or not isinstance(receipt, dict):
            raise ValueError("MCP tool response is missing structured content or receipt")
        receipt_id = str(receipt.get("requestId", ""))
        receipt_hash = str(receipt.get("outputHash", ""))
        if receipt_id != str(call.request_id) or len(receipt_hash) != 64:
            raise ValueError("MCP response receipt does not match the request")
        return ProviderResult(
            output=output,
            provider_request_id=f"{receipt_id}:{receipt_hash}:{int(receipt.get('verified') is True)}",
            status_code=200,
        )

    async def _await_task(self, task: dict[str, Any], deadline: datetime) -> dict[str, Any]:
        task_id = str(task.get("taskId", ""))
        if not task_id:
            raise ValueError("MCP task response is missing taskId")
        while datetime.now(UTC) < deadline:
            delay_ms = max(100, min(int(task.get("pollIntervalMs", 1000)), 10_000))
            await asyncio.sleep(delay_ms / 1000)
            task = await self.client.request(
                "tasks/get", name=task_id, params={"taskId": task_id}
            )
            if task.get("status") == "completed":
                result = task.get("result")
                if not isinstance(result, dict):
                    raise ValueError("completed MCP task has no result")
                return result
            if task.get("status") == "failed":
                raise RuntimeError("remote MCP task failed")
            if task.get("status") == "cancelled":
                raise asyncio.CancelledError
            if task.get("status") == "input_required":
                raise RuntimeError("MCP task requires unsupported interactive input")
        try:
            await self.client.request("tasks/cancel", name=task_id, params={"taskId": task_id})
        except Exception:
            pass
        raise TimeoutError("remote MCP task deadline expired")

    async def verify(
        self, definition: ToolDefinition, call: ToolCall, result: ProviderResult
    ) -> bool:
        del definition
        # TLS + issuer pinning authenticates the server. Its receipt binds the
        # request ID, read-back verification result and structured output hash.
        parts = (result.provider_request_id or "").split(":")
        return bool(
            len(parts) == 3
            and parts[0] == str(call.request_id)
            and parts[1] == output_hash(result.output)
            and parts[2] == "1"
        )
