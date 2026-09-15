from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core import settings
from servicemind.mcp.tasks import InMemoryMcpTaskStore, McpTaskStore, StoredMcpTask
from servicemind.persistence.repository import ServiceMindRepository
from servicemind.security.auth import TenantContextDependency
from servicemind.tool_platform.contracts import ToolCall, ToolExecutionResult
from servicemind.tool_platform.gateway import (
    ToolContractViolation,
    ToolGateway,
    ToolPolicyDenied,
)

MCP_PROTOCOL_VERSION = "2026-07-28"
MCP_SERVER_ISSUER = "servicemind://glpi-mcp"
TASK_EXTENSION = "io.modelcontextprotocol/tasks"
GOVERNED_EXECUTION_EXTENSION = "com.servicemind/governed-execution"
SERVER_INFO = {"name": "servicemind-glpi", "version": "1.0.0"}
TASK_TTL_MS = 3_600_000
TASK_POLL_INTERVAL_MS = 1_000
TASK_AUGMENTED_TOOLS = frozenset({"glpi.query_cmdb_dependencies"})

mcp_router = APIRouter(prefix="/v1/servicemind/mcp", tags=["ServiceMind MCP"])
mcp_metadata_router = APIRouter(tags=["ServiceMind MCP Authorization"])
_gateway: ToolGateway | None = None
_task_store: McpTaskStore = InMemoryMcpTaskStore()
_running_tasks: dict[tuple[UUID, UUID], asyncio.Task[None]] = {}
_receipts: OrderedDict[str, str] = OrderedDict()
_RECEIPT_CACHE_LIMIT = 2048


def configure_mcp_gateway(gateway: ToolGateway, task_store: McpTaskStore | None = None) -> None:
    global _gateway, _task_store
    _gateway = gateway
    if task_store is not None:
        _task_store = task_store


async def shutdown_mcp_tasks() -> None:
    tasks = tuple(_running_tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _running_tasks.clear()


class JsonRpcRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jsonrpc: Literal["2.0"]
    id: int | str
    method: str = Field(min_length=1, max_length=160)
    params: dict[str, Any]


class ServiceMindExecutionMeta(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    request_id: UUID = Field(alias="requestId")
    run_id: UUID = Field(alias="runId")
    task_id: str = Field(alias="taskId", min_length=1, max_length=100)
    deadline: datetime


class HeaderMismatchError(ValueError):
    """The MCP method-specific HTTP routing header disagrees with JSON-RPC."""


def _headers(response: Response) -> None:
    response.headers["Mcp-Protocol-Version"] = MCP_PROTOCOL_VERSION
    response.headers["Mcp-Server-Issuer"] = MCP_SERVER_ISSUER
    response.headers["Cache-Control"] = "no-store"


def _public_resource_url(request: Request) -> str:
    configured = settings.SERVICEMIND_MCP_PUBLIC_URL
    if configured:
        return configured.rstrip("/")
    return f"{str(request.base_url).rstrip('/')}/v1/servicemind/mcp"


@mcp_metadata_router.get(
    "/.well-known/oauth-protected-resource", name="mcp_protected_resource_metadata_root"
)
@mcp_metadata_router.get(
    "/.well-known/oauth-protected-resource/v1/servicemind/mcp",
    name="mcp_protected_resource_metadata_path",
)
async def mcp_protected_resource_metadata(request: Request) -> dict[str, Any]:
    """RFC 9728 discovery for the protected HTTP MCP resource."""
    issuer = settings.SERVICEMIND_OIDC_ISSUER
    if not issuer:
        # The MCP endpoint itself is fail-closed without OIDC configuration.
        return {
            "resource": _public_resource_url(request),
            "authorization_servers": [],
            "bearer_methods_supported": ["header"],
        }
    return {
        "resource": _public_resource_url(request),
        "authorization_servers": [issuer.rstrip("/")],
        "bearer_methods_supported": ["header"],
        "resource_name": "ServiceMind governed GLPI MCP",
    }


def _meta() -> dict[str, Any]:
    return {"io.modelcontextprotocol/serverInfo": SERVER_INFO}


def _result(request_id: str | int, value: dict[str, Any]) -> dict[str, Any]:
    value.setdefault("resultType", "complete")
    value.setdefault("_meta", _meta())
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def _error(
    request_id: str | int | None, code: int, message: str, data: Any | None = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    payload: dict[str, Any] = {"jsonrpc": "2.0", "error": error}
    if request_id is not None:
        payload["id"] = request_id
    return payload


def _request_meta(params: dict[str, Any]) -> dict[str, Any]:
    value = params.get("_meta")
    if not isinstance(value, dict):
        raise ValueError("params._meta is required")
    protocol_version = value.get("io.modelcontextprotocol/protocolVersion")
    if not isinstance(protocol_version, str) or not protocol_version:
        raise ValueError("request metadata protocol version is invalid")
    client_info = value.get("io.modelcontextprotocol/clientInfo")
    if client_info is not None and (
        not isinstance(client_info, dict)
        or not all(
            isinstance(client_info.get(field), str) and client_info[field]
            for field in ("name", "version")
        )
    ):
        raise ValueError("request metadata client info is invalid")
    capabilities = value.get("io.modelcontextprotocol/clientCapabilities")
    if not isinstance(capabilities, dict):
        raise ValueError("request metadata client capabilities are invalid")
    return value


def _supports_tasks(meta: dict[str, Any]) -> bool:
    capabilities = meta.get("io.modelcontextprotocol/clientCapabilities", {})
    return isinstance(capabilities, dict) and TASK_EXTENSION in capabilities.get("extensions", {})


def _supports_governed_execution(meta: dict[str, Any]) -> bool:
    capabilities = meta["io.modelcontextprotocol/clientCapabilities"]
    extensions = capabilities.get("extensions", {})
    return isinstance(extensions, dict) and GOVERNED_EXECUTION_EXTENSION in extensions


@mcp_router.post("")
async def mcp_request(
    body: dict[str, Any],
    response: Response,
    context: TenantContextDependency,
    mcp_protocol_version: str | None = Header(default=None, alias="Mcp-Protocol-Version"),
    mcp_method: str | None = Header(default=None, alias="Mcp-Method"),
    mcp_name: str | None = Header(default=None, alias="Mcp-Name"),
) -> dict[str, Any]:
    _headers(response)
    raw_id = body.get("id") if isinstance(body, dict) else None
    if mcp_protocol_version is None:
        response.status_code = 400
        return _error(raw_id, -32020, "Required MCP HTTP header is missing")
    mcp_protocol_version = mcp_protocol_version.strip()
    if mcp_protocol_version != MCP_PROTOCOL_VERSION:
        response.status_code = 400
        return _error(
            raw_id,
            -32022,
            "Unsupported protocol version",
            {"supported": [MCP_PROTOCOL_VERSION], "requested": mcp_protocol_version},
        )
    try:
        request = JsonRpcRequest.model_validate(body)
    except ValidationError:
        response.status_code = 400
        return _error(raw_id, -32600, "Invalid JSON-RPC request")
    if mcp_method is None or request.method != mcp_method.strip():
        response.status_code = 400
        return _error(request.id, -32020, "MCP HTTP header does not match the request")
    try:
        meta = _request_meta(request.params)
    except ValueError:
        response.status_code = 400
        return _error(request.id, -32602, "Invalid MCP request metadata")
    if meta["io.modelcontextprotocol/protocolVersion"] != mcp_protocol_version:
        response.status_code = 400
        return _error(request.id, -32020, "MCP HTTP header does not match the request")
    if _gateway is None:
        response.status_code = 503
        return _error(request.id, -32603, "MCP Tool Gateway is unavailable")

    if request.method == "server/discover":
        if mcp_name is not None:
            response.status_code = 400
            return _error(request.id, -32020, "MCP HTTP header does not match the request")
        return _result(
            request.id,
            {
                "supportedVersions": [MCP_PROTOCOL_VERSION],
                "capabilities": {
                    "tools": {},
                    "resources": {},
                    "extensions": {
                        TASK_EXTENSION: {},
                        GOVERNED_EXECUTION_EXTENSION: {},
                    },
                },
                "instructions": (
                    "Read GLPI resources and submit ActionIntent records. "
                    "Direct GLPI mutation is unavailable."
                ),
                "ttlMs": 60_000,
                "cacheScope": "public",
            },
        )
    if request.method == "tools/list":
        if mcp_name is not None:
            response.status_code = 400
            return _error(request.id, -32020, "MCP HTTP header does not match the request")
        tools = _gateway.registry.visible(
            roles=frozenset(context.roles),
            entity_ids=frozenset(context.allowed_glpi_entity_ids),
        )
        if not _supports_governed_execution(meta):
            tools = tuple(item for item in tools if item.name != "glpi.submit_action_intent")
        return _result(
            request.id,
            {
                "tools": [
                    {
                        "name": item.name.removeprefix("glpi."),
                        "description": f"Governed ServiceMind operation: {item.name}",
                        "inputSchema": item.input_schema,
                        "outputSchema": item.output_schema,
                        "annotations": {
                            "readOnlyHint": item.read_write_type == "read",
                            "destructiveHint": False,
                            "idempotentHint": True,
                        },
                        "_meta": {
                            "com.servicemind/version": item.version,
                            "com.servicemind/risk": item.risk_level,
                        },
                    }
                    for item in tools
                ],
                "ttlMs": 60_000,
                "cacheScope": "private",
            },
        )
    if request.method in {"resources/list", "resources/templates/list"}:
        if mcp_name is not None:
            response.status_code = 400
            return _error(request.id, -32020, "MCP HTTP header does not match the request")
        if request.method == "resources/list":
            return _result(
                request.id,
                {"resources": [], "ttlMs": 60_000, "cacheScope": "private"},
            )
        return _result(
            request.id,
            {
                "resourceTemplates": [
                    {
                        "uriTemplate": f"glpi://{kind}/{{id}}",
                        "name": kind,
                        "mimeType": "application/json",
                    }
                    for kind in ("tickets", "problems", "changes", "cmdb/items", "knowledge")
                ],
                "ttlMs": 60_000,
                "cacheScope": "private",
            },
        )
    if request.method in {"tasks/get", "tasks/update", "tasks/cancel"}:
        return await _task_request(request, context.tenant_id, mcp_name, response)
    if request.method not in {"tools/call", "resources/read"}:
        response.status_code = 404
        return _error(request.id, -32601, "Method not found")

    try:
        call = await _tool_call(request, context, mcp_name)
    except HeaderMismatchError:
        response.status_code = 400
        return _error(request.id, -32020, "MCP HTTP header does not match the request")
    except (KeyError, LookupError, TypeError, ValueError, ValidationError):
        return _error(request.id, -32602, "Invalid MCP method parameters")
    if request.method == "tools/call" and call.tool_name in TASK_AUGMENTED_TOOLS:
        if _supports_tasks(meta):
            mcp_task_id = uuid4()
            task_state = await _task_store.create(
                context.tenant_id,
                task_id=mcp_task_id,
                request_id=call.request_id,
                run_id=call.run_id,
                workflow_task_id=call.task_id,
                tool_name=call.tool_name,
                argument_hash=call.argument_hash,
            )
            if task_state.task_id == mcp_task_id:
                running = asyncio.create_task(
                    _run_task(context.tenant_id, mcp_task_id, call),
                    name=f"mcp-task-{mcp_task_id}",
                )
                _running_tasks[(context.tenant_id, mcp_task_id)] = running
            return _result(request.id, _task_payload(task_state, creation=True))
    try:
        execution = await _gateway.execute(call)
    except ToolPolicyDenied:
        if request.method == "tools/call":
            return _result(request.id, _call_error_result("Tool policy denied the request"))
        return _error(request.id, -32003, "Tool policy denied the request")
    except (LookupError, ToolContractViolation, ValueError):
        return _error(request.id, -32602, "Invalid tool request")
    except Exception:
        if request.method == "tools/call":
            return _result(request.id, _call_error_result("Tool execution failed"))
        return _error(request.id, -32603, "Tool execution failed")
    if request.method == "resources/read":
        uri = str(request.params["uri"])
        return _result(
            request.id,
            {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "application/json",
                        "text": json.dumps(execution.output["resource"], ensure_ascii=False),
                    }
                ],
                "ttlMs": 0,
                "cacheScope": "private",
                "_meta": _receipt_meta(execution),
            },
        )
    return _result(request.id, _call_result(execution))


async def _tool_call(request: JsonRpcRequest, context, mcp_name: str | None) -> ToolCall:
    params = request.params
    meta = _request_meta(params)
    raw_execution = meta.get("com.servicemind/execution")
    if request.method == "tools/call":
        raw_name = str(params["name"])
        if mcp_name is None or mcp_name.strip() != raw_name:
            raise HeaderMismatchError("Mcp-Name does not match tool name")
        tool_name = f"glpi.{raw_name}"
        arguments = params.get("arguments", {})
    else:
        uri = str(params["uri"])
        if mcp_name is None or mcp_name.strip() != uri:
            raise HeaderMismatchError("Mcp-Name does not match resource URI")
        tool_name = "glpi.read_resource"
        arguments = _resource_arguments(uri)
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    assert _gateway is not None
    visible = {
        item.name
        for item in _gateway.registry.visible(
            roles=frozenset(context.roles),
            entity_ids=frozenset(context.allowed_glpi_entity_ids),
        )
    }
    capabilities = visible | ({"glpi.read_resource"} if request.method == "resources/read" else set())
    if tool_name == "glpi.submit_action_intent" and (
        raw_execution is None or not _supports_governed_execution(meta)
    ):
        raise ValueError("ActionIntent requires the governed execution extension")
    if raw_execution is not None:
        execution = ServiceMindExecutionMeta.model_validate(raw_execution)
        # The durable audit foreign key must never reference another tenant's run.
        # Unit gateways use an in-memory audit sink and deliberately have no DB run.
        from servicemind.tool_platform.audit import PostgresToolAuditSink

        if isinstance(_gateway.audit, PostgresToolAuditSink):
            stored_run = await ServiceMindRepository(context.tenant_id).get_run(execution.run_id)
            if stored_run is None:
                raise ValueError("execution run is outside the authenticated tenant")
    else:
        definition = _gateway.registry.resolve(tool_name, "1.0.0")
        ticket_id = arguments.get("ticket_id", arguments.get("resource_id", 0))
        if not isinstance(ticket_id, int) or ticket_id < 0:
            ticket_id = 0
        run = await ServiceMindRepository(context.tenant_id).create_run(
            user_id=context.user_id,
            ticket_id=ticket_id,
            goal=f"MCP read operation: {tool_name}",
            request_write=False,
        )
        execution = ServiceMindExecutionMeta(
            requestId=uuid4(),
            runId=run.id,
            taskId=f"mcp-{uuid4()}",
            deadline=datetime.now(UTC) + timedelta(seconds=definition.timeout_seconds + 5),
        )
    return ToolCall(
        request_id=execution.request_id,
        tenant_id=context.tenant_id,
        run_id=execution.run_id,
        task_id=execution.task_id,
        user_id=context.user_id,
        roles=frozenset(context.roles),
        entity_ids=frozenset(context.allowed_glpi_entity_ids),
        capabilities=frozenset(capabilities),
        tool_name=tool_name,
        tool_version="1.0.0",
        arguments=arguments,
        deadline=execution.deadline,
    )


def _receipt_meta(result: ToolExecutionResult) -> dict[str, Any]:
    receipt_id = str(result.request_id)
    _receipts[receipt_id] = result.output_hash
    _receipts.move_to_end(receipt_id)
    while len(_receipts) > _RECEIPT_CACHE_LIMIT:
        _receipts.popitem(last=False)
    return {
        **_meta(),
        "com.servicemind/receipt": {
            "requestId": receipt_id,
            "outputHash": result.output_hash,
            "verified": result.verified,
        },
    }


def _call_result(result: ToolExecutionResult) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(result.output, ensure_ascii=False, separators=(",", ":")),
            }
        ],
        "structuredContent": result.output,
        "isError": False,
        "_meta": _receipt_meta(result),
    }


def _call_error_result(message: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }


async def _task_request(
    request: JsonRpcRequest,
    tenant_id: UUID,
    mcp_name: str | None,
    response: Response,
) -> dict[str, Any]:
    if not _supports_tasks(_request_meta(request.params)):
        response.status_code = 400
        return _error(
            request.id,
            -32021,
            "Missing required client capability",
            {"requiredCapabilities": {"extensions": {TASK_EXTENSION: {}}}},
        )
    try:
        task_id = UUID(str(request.params["taskId"]))
    except (KeyError, ValueError):
        return _error(request.id, -32602, "Invalid task ID")
    if mcp_name is None or mcp_name.strip() != str(task_id):
        response.status_code = 400
        return _error(request.id, -32020, "MCP HTTP header does not match the request")
    task = await _task_store.get(tenant_id, task_id)
    if task is None:
        return _error(request.id, -32602, "MCP task not found")
    if request.method == "tasks/get":
        return _result(request.id, _task_payload(task))
    if request.method == "tasks/update":
        # ServiceMind tasks never request MRTR input. Accepting arbitrary client
        # output here would let a caller forge a terminal task result.
        return _error(request.id, -32602, "Task has no outstanding input requests")
    running = _running_tasks.get((tenant_id, task_id))
    if running:
        running.cancel()
    await _task_store.cancel(tenant_id, task_id)
    return _result(request.id, {})


def _task_payload(task: StoredMcpTask, *, creation: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "resultType": "task" if creation else "complete",
        "taskId": str(task.task_id),
        "status": task.status,
        "createdAt": task.created_at.isoformat(),
        "lastUpdatedAt": task.updated_at.isoformat(),
        "ttlMs": TASK_TTL_MS,
        "pollIntervalMs": TASK_POLL_INTERVAL_MS,
    }
    if task.status == "completed":
        payload["result"] = task.output
    elif task.status == "failed":
        payload["error"] = {"code": -32603, "message": task.error_code or "Task failed"}
    return payload


async def _run_task(tenant_id: UUID, task_id: UUID, call: ToolCall) -> None:
    assert _gateway is not None
    execution_task = asyncio.create_task(_gateway.execute(call))
    try:
        while not execution_task.done():
            done, _ = await asyncio.wait({execution_task}, timeout=1)
            if done:
                break
            if not await _task_store.heartbeat(tenant_id, task_id):
                execution_task.cancel()
                with suppress(asyncio.CancelledError):
                    await execution_task
                return
        result = await execution_task
        await _task_store.finish(
            tenant_id,
            task_id,
            status="completed",
            output=_call_result(result),
            output_hash=result.output_hash,
        )
    except asyncio.CancelledError:
        execution_task.cancel()
        with suppress(asyncio.CancelledError):
            await execution_task
        await _task_store.cancel(tenant_id, task_id)
        raise
    except Exception:
        await _task_store.finish(
            tenant_id, task_id, status="failed", error_code="Tool execution failed"
        )
    finally:
        _running_tasks.pop((tenant_id, task_id), None)


def _resource_arguments(uri: str) -> dict[str, Any]:
    prefixes = {
        "glpi://tickets/": "ticket",
        "glpi://problems/": "problem",
        "glpi://changes/": "change",
        "glpi://cmdb/items/": "cmdb",
        "glpi://knowledge/": "knowledge",
    }
    for prefix, kind in prefixes.items():
        if uri.startswith(prefix):
            suffix = uri.removeprefix(prefix)
            if suffix.isdecimal() and int(suffix) > 0:
                return {"resource_type": kind, "resource_id": int(suffix)}
    raise ValueError("unsupported or malformed GLPI resource URI")
