from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from servicemind.mcp.server import (
    GOVERNED_EXECUTION_EXTENSION,
    MCP_SERVER_ISSUER,
    TASK_EXTENSION,
    _resource_arguments,
    configure_mcp_gateway,
    mcp_metadata_router,
    mcp_router,
)
from servicemind.mcp.tasks import InMemoryMcpTaskStore
from servicemind.mcp.transport import MCP_PROTOCOL_VERSION, McpGlpiProvider, StatelessMcpClient
from servicemind.persistence.models import ToolOutboxRecord
from servicemind.reliability.outbox import RedisStreamPublisher
from servicemind.tool_platform.audit import InMemoryToolAuditSink
from servicemind.tool_platform.catalog import build_glpi_registry
from servicemind.tool_platform.contracts import (
    DataClassification,
    ProviderResult,
    RetryPolicy,
    ToolAccess,
    ToolCall,
    ToolDefinition,
    ToolRisk,
    output_hash,
)
from servicemind.tool_platform.gateway import (
    ToolContractViolation,
    ToolGateway,
    ToolPolicyDenied,
    ToolVerificationFailed,
)
from servicemind.tool_platform.policy import DeterministicToolPolicy, OpaToolPolicy
from servicemind.tool_platform.providers import NativeGlpiProvider
from servicemind.tool_platform.registry import ToolRegistry
from servicemind.tool_platform.resilience import (
    CircuitBreaker,
    IdempotencyLedger,
    RateLimitExceeded,
    SlidingWindowRateLimiter,
)

TENANT = UUID("11111111-1111-4111-8111-111111111111")


def definition(*, access: ToolAccess = ToolAccess.READ, attempts: int = 2) -> ToolDefinition:
    return ToolDefinition(
        name="glpi.test_tool",
        version="1.0.0",
        provider="fake_provider",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        read_write_type=access,
        risk_level=ToolRisk.LOW if access is ToolAccess.READ else ToolRisk.HIGH,
        allowed_roles=frozenset({"analyst"}),
        allowed_entities=frozenset({1}),
        requires_approval=access is ToolAccess.WRITE,
        timeout_seconds=1,
        retry_policy=RetryPolicy(max_attempts=attempts),
        idempotency_strategy="request_id",
        verification_strategy="readback",
        data_classification=DataClassification.CONFIDENTIAL,
    )


def call(**updates) -> ToolCall:
    values = {
        "tenant_id": TENANT,
        "run_id": uuid4(),
        "task_id": "T1",
        "user_id": "analyst-1",
        "roles": frozenset({"analyst"}),
        "entity_ids": frozenset({1}),
        "capabilities": frozenset({"glpi.test_tool"}),
        "tool_name": "glpi.test_tool",
        "tool_version": "1.0.0",
        "arguments": {"value": 7},
        "deadline": datetime.now(UTC) + timedelta(minutes=1),
    }
    values.update(updates)
    return ToolCall(**values)


class FakeProvider:
    name = "fake_provider"

    def __init__(self, failures: list[Exception] | None = None, *, verified: bool = True):
        self.failures = list(failures or [])
        self.calls = 0
        self.verified = verified

    async def execute(
        self, definition: ToolDefinition, call: ToolCall
    ) -> ProviderResult:
        del definition
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return ProviderResult(output={"value": call.arguments["value"]})

    async def verify(
        self, definition: ToolDefinition, call: ToolCall, result: ProviderResult
    ) -> bool:
        del definition, call, result
        return self.verified


def gateway(provider: FakeProvider, *, limiter=None, circuit=None):
    registry = ToolRegistry()
    registry.register(definition())
    audit = InMemoryToolAuditSink()
    return (
        ToolGateway(
            registry=registry,
            policy=DeterministicToolPolicy(),
            providers={provider.name: provider},
            audit=audit,
            rate_limiter=limiter,
            circuit_breaker=circuit,
        ),
        audit,
    )


def test_registry_versions_are_immutable_and_visibility_is_scoped() -> None:
    registry = ToolRegistry()
    item = definition()
    registry.register(item)
    registry.register(item)
    with pytest.raises(ValueError, match="checksum conflict"):
        registry.register(item.model_copy(update={"timeout_seconds": 2}))
    assert registry.visible(roles=frozenset({"analyst"}), entity_ids=frozenset({1})) == (item,)
    assert registry.visible(roles=frozenset({"viewer"}), entity_ids=frozenset({1})) == ()
    assert registry.visible(roles=frozenset({"analyst"}), entity_ids=frozenset({2})) == ()


def test_write_contract_requires_approval_single_attempt_and_verification() -> None:
    with pytest.raises(ValidationError, match="approval"):
        definition(access=ToolAccess.WRITE, attempts=1).model_copy(
            update={"requires_approval": False},
            deep=True,
        ).__class__.model_validate(
            {
                **definition(access=ToolAccess.WRITE, attempts=1).model_dump(),
                "requires_approval": False,
            }
        )
    with pytest.raises(ValidationError, match="cannot retry"):
        definition(access=ToolAccess.WRITE, attempts=2)


@pytest.mark.asyncio
async def test_gateway_enforces_contract_policy_audit_and_exact_replay() -> None:
    provider = FakeProvider()
    subject, audit = gateway(provider)
    request = call()
    first, second = await asyncio.gather(subject.execute(request), subject.execute(request))
    assert first.output == second.output == {"value": 7}
    assert {first.duplicate_suppressed, second.duplicate_suppressed} == {False, True}
    assert provider.calls == 1
    assert audit.policy and audit.invocations[-1].status == "succeeded"
    assert audit.invocations[-1].argument_hash == request.argument_hash


@pytest.mark.asyncio
async def test_gateway_rejects_schema_capability_role_entity_taint_and_secret() -> None:
    cases = (
        call(arguments={"value": "bad"}),
        call(capabilities=frozenset()),
        call(roles=frozenset({"viewer"})),
        call(entity_ids=frozenset({2})),
        call(taint_labels=frozenset({"prompt_injection"})),
        call(arguments={"value": 7, "password": "secret-value"}),
    )
    for index, request in enumerate(cases):
        provider = FakeProvider()
        subject, _ = gateway(provider)
        expected = ToolContractViolation if index in {0, 5} else ToolPolicyDenied
        with pytest.raises(expected):
            await subject.execute(request)
        assert provider.calls == 0


@pytest.mark.asyncio
async def test_gateway_retries_reads_but_requires_verified_output() -> None:
    timeout = httpx.ReadTimeout("slow")
    provider = FakeProvider([timeout])
    subject, _ = gateway(provider)
    result = await subject.execute(call())
    assert result.attempts == 2 and provider.calls == 2

    bad = FakeProvider(verified=False)
    subject, audit = gateway(bad)
    with pytest.raises(ToolVerificationFailed):
        await subject.execute(call())
    assert audit.invocations[-1].error_code == "verification_failed"


@pytest.mark.asyncio
async def test_rate_limit_and_circuit_breaker_fail_closed() -> None:
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60)
    subject, audit = gateway(FakeProvider(), limiter=limiter)
    await subject.execute(call())
    with pytest.raises(RateLimitExceeded):
        await subject.execute(call())
    assert audit.invocations[-1].error_code == "rate_limit"

    circuit = CircuitBreaker(failure_threshold=1, reset_seconds=60)
    broken, circuit_audit = gateway(FakeProvider([RuntimeError("down")]), circuit=circuit)
    with pytest.raises(RuntimeError):
        await broken.execute(call())
    with pytest.raises(RuntimeError, match="circuit is open"):
        await broken.execute(call())
    assert circuit_audit.invocations[-1].error_code == "circuit_open"


@pytest.mark.asyncio
async def test_idempotency_ledger_is_bounded_and_failed_entries_can_be_released() -> None:
    ledger = IdempotencyLedger(max_entries=1, ttl_seconds=60)
    first = await ledger.lock(TENANT, "first", "a" * 64)
    with pytest.raises(RuntimeError, match="capacity exhausted"):
        await ledger.lock(TENANT, "second", "b" * 64)
    await ledger.abort(TENANT, "first")
    with pytest.raises(ValueError, match="different arguments"):
        await ledger.lock(TENANT, "first", "b" * 64)
    assert await ledger.lock(TENANT, "second", "b" * 64) is not first


@pytest.mark.asyncio
async def test_mcp_tasks_are_tenant_scoped_and_terminal_state_is_immutable() -> None:
    store = InMemoryMcpTaskStore()
    task_id, request_id, run_id = uuid4(), uuid4(), uuid4()
    created = await store.create(
        TENANT,
        task_id=task_id,
        request_id=request_id,
        run_id=run_id,
        workflow_task_id="T1",
        tool_name="glpi.search_tickets",
        argument_hash="a" * 64,
    )
    assert created.status == "working"
    replay = await store.create(
        TENANT,
        task_id=uuid4(),
        request_id=request_id,
        run_id=run_id,
        workflow_task_id="T1",
        tool_name="glpi.search_tickets",
        argument_hash="a" * 64,
    )
    assert replay.task_id == task_id
    assert await store.heartbeat(TENANT, task_id)
    assert await store.get(uuid4(), task_id) is None
    completed = await store.finish(
        TENANT,
        task_id,
        status="completed",
        output={"tickets": []},
        output_hash="b" * 64,
    )
    assert completed is not None and completed.status == "completed"
    unchanged = await store.cancel(TENANT, task_id)
    assert unchanged == completed

    interrupted_id = uuid4()
    await store.create(
        TENANT,
        task_id=interrupted_id,
        request_id=uuid4(),
        run_id=run_id,
        workflow_task_id="T2",
        tool_name="glpi.query_cmdb_dependencies",
        argument_hash="c" * 64,
    )
    assert await store.recover_interrupted() == 1
    interrupted = await store.get(TENANT, interrupted_id)
    assert interrupted is not None and interrupted.status == "failed"
    assert interrupted.error_code == "Task interrupted by service restart"


@pytest.mark.asyncio
async def test_opa_decision_is_validated_and_unavailability_denies() -> None:
    request = call()
    item = definition()

    def handler(message: httpx.Request) -> httpx.Response:
        payload = json.loads(message.content)
        assert "arguments_hash" in payload["input"] and "arguments" not in payload["input"]
        return httpx.Response(
            200,
            headers={"X-OPA-Decision-ID": "opa-42"},
            json={
                "result": {
                    "allow": True,
                    "allowed_fields": ["value"],
                    "reason_codes": ["OPA_ALLOWED"],
                    "policy_version": "bundle-42",
                }
            },
        )

    policy = OpaToolPolicy(
        "http://opa.internal",
        policy_version="fallback",
        transport=httpx.MockTransport(handler),
    )
    decision = await policy.decide(item, request)
    assert decision.allow and decision.external_decision_id == "opa-42"
    assert decision.policy_version == "bundle-42"

    unavailable = OpaToolPolicy(
        "http://opa.internal",
        policy_version="v1",
        transport=httpx.MockTransport(lambda request: httpx.Response(503)),
    )
    denied = await unavailable.decide(item, request)
    assert not denied.allow and denied.reason_codes == ("POLICY_ENGINE_UNAVAILABLE_OR_INVALID",)


@pytest.mark.asyncio
async def test_native_and_stateless_mcp_provider_contract_parity() -> None:
    output = {"tickets": [{"id": 42, "name": "VPN incident"}]}

    class Backend:
        async def invoke(
            self, name: str, arguments: dict[str, Any], call: ToolCall
        ) -> Any:
            del name, arguments, call
            return output

        async def verify(
            self, name: str, arguments: dict[str, Any], output: Any, call: ToolCall
        ) -> bool:
            del name, arguments, call
            return output == {"tickets": [{"id": 42, "name": "VPN incident"}]}

    native = NativeGlpiProvider(Backend())
    native_definition = build_glpi_registry("native_glpi").resolve("glpi.search_tickets", "1.0.0")
    request = call(
        tool_name="glpi.search_tickets",
        capabilities=frozenset({"glpi.search_tickets"}),
        arguments={"query": "vpn", "limit": 5},
    )
    native_result = await native.execute(native_definition, request)
    assert await native.verify(native_definition, request, native_result)

    seen_headers = []

    def handler(message: httpx.Request) -> httpx.Response:
        seen_headers.append(message.headers)
        body = json.loads(message.content)
        assert body["jsonrpc"] == "2.0" and body["method"] == "tools/call"
        assert body["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] == (
            MCP_PROTOCOL_VERSION
        )
        receipt_id = body["params"]["_meta"]["com.servicemind/execution"]["requestId"]
        return httpx.Response(
            200,
            headers={
                "Mcp-Protocol-Version": MCP_PROTOCOL_VERSION,
                "Mcp-Server-Issuer": "issuer-1",
            },
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "resultType": "complete",
                    "content": [{"type": "text", "text": json.dumps(output)}],
                    "structuredContent": output,
                    "_meta": {
                        "com.servicemind/receipt": {
                            "requestId": receipt_id,
                            "outputHash": output_hash(output),
                            "verified": True,
                        }
                    },
                },
            },
        )

    async def token() -> str:
        return "service-token"

    mcp = McpGlpiProvider(
        StatelessMcpClient(
            "https://mcp.internal/glpi",
            expected_issuer="issuer-1",
            token_provider=token,
            transport=httpx.MockTransport(handler),
        )
    )
    mcp_definition = build_glpi_registry("mcp_glpi").resolve("glpi.search_tickets", "1.0.0")
    mcp_result = await mcp.execute(mcp_definition, request)
    assert await mcp.verify(mcp_definition, request, mcp_result)
    assert native_result.output == mcp_result.output
    assert all("mcp-session-id" not in headers for headers in seen_headers)


@pytest.mark.asyncio
async def test_mcp_rejects_session_header_and_wrong_issuer() -> None:
    async def token() -> str:
        return "token"

    for headers, error in (
        (
            {
                "Mcp-Protocol-Version": MCP_PROTOCOL_VERSION,
                "Mcp-Server-Issuer": "issuer",
                "Mcp-Session-Id": "legacy",
            },
            ValueError,
        ),
        (
            {"Mcp-Protocol-Version": MCP_PROTOCOL_VERSION, "Mcp-Server-Issuer": "evil"},
            PermissionError,
        ),
    ):
        client = StatelessMcpClient(
            "https://mcp.internal/glpi",
            expected_issuer="issuer",
            token_provider=token,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, headers=headers, json={})),
        )
        with pytest.raises(error):
            await client.request("tools/list", name=None, params={})


@pytest.mark.asyncio
async def test_mcp_server_uses_jsonrpc_discovery_headers_and_typed_results(monkeypatch) -> None:
    from servicemind.security.auth import TenantContext, get_tenant_context

    subject, _ = gateway(FakeProvider())
    configure_mcp_gateway(subject, InMemoryMcpTaskStore())
    app = FastAPI()
    app.include_router(mcp_router)
    app.dependency_overrides[get_tenant_context] = lambda: TenantContext(
        tenant_id=TENANT,
        user_id="analyst-1",
        username="analyst-1",
        roles={"analyst"},
        allowed_glpi_entity_ids={1},
    )

    class AutoRunRepository:
        def __init__(self, tenant_id) -> None:
            assert tenant_id == TENANT

        async def create_run(self, **kwargs):
            assert kwargs["request_write"] is False
            return SimpleNamespace(id=uuid4())

    monkeypatch.setattr("servicemind.mcp.server.ServiceMindRepository", AutoRunRepository)
    headers = {
        "Mcp-Protocol-Version": MCP_PROTOCOL_VERSION,
        "Mcp-Method": "server/discover",
    }
    meta = {
        "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientInfo": {"name": "phase6-test", "version": "1.0"},
        "io.modelcontextprotocol/clientCapabilities": {
            "extensions": {TASK_EXTENSION: {}, GOVERNED_EXECUTION_EXTENSION: {}}
        },
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/servicemind/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "server/discover",
                "params": {"_meta": meta},
            },
        )
        payload = response.json()
        assert response.status_code == 200
        assert response.headers["Mcp-Server-Issuer"] == MCP_SERVER_ISSUER
        assert payload["result"]["resultType"] == "complete"
        assert payload["result"]["supportedVersions"] == [MCP_PROTOCOL_VERSION]
        assert TASK_EXTENSION in payload["result"]["capabilities"]["extensions"]

        optional_client_info_meta = dict(meta)
        optional_client_info_meta.pop("io.modelcontextprotocol/clientInfo")
        response = await client.post(
            "/v1/servicemind/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": "optional-client-info",
                "method": "server/discover",
                "params": {"_meta": optional_client_info_meta},
            },
        )
        assert response.status_code == 200
        assert response.json()["result"]["supportedVersions"] == [MCP_PROTOCOL_VERSION]

        mismatched_headers = {**headers, "Mcp-Name": "must-not-be-present"}
        response = await client.post(
            "/v1/servicemind/mcp",
            headers=mismatched_headers,
            json={
                "jsonrpc": "2.0",
                "id": "header-mismatch",
                "method": "server/discover",
                "params": {"_meta": meta},
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == -32020

        missing_method_headers = {
            key: value for key, value in headers.items() if key != "Mcp-Method"
        }
        response = await client.post(
            "/v1/servicemind/mcp",
            headers=missing_method_headers,
            json={
                "jsonrpc": "2.0",
                "id": "missing-method-header",
                "method": "server/discover",
                "params": {"_meta": meta},
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == -32020

        response = await client.post(
            "/v1/servicemind/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": "version-header-mismatch",
                "method": "server/discover",
                "params": {
                    "_meta": {
                        **meta,
                        "io.modelcontextprotocol/protocolVersion": "2025-11-25",
                    }
                },
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == -32020

        unknown_headers = {**headers, "Mcp-Method": "initialize"}
        response = await client.post(
            "/v1/servicemind/mcp",
            headers=unknown_headers,
            json={
                "jsonrpc": "2.0",
                "id": "removed-method",
                "method": "initialize",
                "params": {"_meta": meta},
            },
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == -32601

        headers["Mcp-Method"] = "tools/call"
        headers["Mcp-Name"] = "test_tool"
        response = await client.post(
            "/v1/servicemind/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": "call-1",
                "method": "tools/call",
                "params": {
                    "name": "test_tool",
                    "arguments": {"value": 7},
                    "_meta": {
                        **meta,
                        "com.servicemind/execution": {
                            "requestId": str(uuid4()),
                            "runId": str(uuid4()),
                            "taskId": "T1",
                            "deadline": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
                        },
                    },
                },
            },
        )
        result = response.json()["result"]
        assert result["resultType"] == "complete"
        assert result["structuredContent"] == {"value": 7}
        assert result["_meta"]["com.servicemind/receipt"]["verified"] is True

        standard_meta = {
            **meta,
            "io.modelcontextprotocol/clientCapabilities": {"extensions": {}},
        }
        headers.pop("Mcp-Name")
        headers["Mcp-Method"] = "tools/list"
        response = await client.post(
            "/v1/servicemind/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {"_meta": standard_meta},
            },
        )
        assert "submit_action_intent" not in {
            item["name"] for item in response.json()["result"]["tools"]
        }
        headers["Mcp-Method"] = "tools/call"
        headers["Mcp-Name"] = "test_tool"
        response = await client.post(
            "/v1/servicemind/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "test_tool",
                    "arguments": {"value": 8},
                    "_meta": standard_meta,
                },
            },
        )
        assert response.json()["result"]["structuredContent"] == {"value": 8}


@pytest.mark.asyncio
async def test_mcp_oauth_resource_discovery_and_401_challenge(monkeypatch) -> None:
    from core import settings

    monkeypatch.setattr(
        settings,
        "SERVICEMIND_MCP_PUBLIC_URL",
        "https://mcp.example.test/v1/servicemind/mcp",
    )
    monkeypatch.setattr(settings, "SERVICEMIND_OIDC_ISSUER", "https://id.example.test/realm")
    app = FastAPI()
    app.include_router(mcp_router)
    app.include_router(mcp_metadata_router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://mcp.example.test"
    ) as client:
        metadata = await client.get(
            "/.well-known/oauth-protected-resource/v1/servicemind/mcp"
        )
        assert metadata.json() == {
            "resource": "https://mcp.example.test/v1/servicemind/mcp",
            "authorization_servers": ["https://id.example.test/realm"],
            "bearer_methods_supported": ["header"],
            "resource_name": "ServiceMind governed GLPI MCP",
        }
        unauthorized = await client.post(
            "/v1/servicemind/mcp",
            headers={
                "Mcp-Protocol-Version": MCP_PROTOCOL_VERSION,
                "Mcp-Method": "server/discover",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "server/discover",
                "params": {"_meta": {}},
            },
        )
    assert unauthorized.status_code == 401
    assert unauthorized.headers["www-authenticate"] == (
        'Bearer resource_metadata="https://mcp.example.test/'
        '.well-known/oauth-protected-resource/v1/servicemind/mcp"'
    )


def test_mcp_resources_are_strict_and_direct_update_is_not_exposed() -> None:
    assert _resource_arguments("glpi://tickets/42") == {
        "resource_type": "ticket",
        "resource_id": 42,
    }
    for uri in ("glpi://tickets/0", "glpi://tickets/../../etc/passwd", "https://evil"):
        with pytest.raises(ValueError):
            _resource_arguments(uri)
    names = {item.name for item in build_glpi_registry("native_glpi").visible(
        roles=frozenset({"analyst"}), entity_ids=frozenset({1})
    )}
    assert "glpi.submit_action_intent" in names
    assert all("direct_update" not in name for name in names)


@pytest.mark.asyncio
async def test_redis_outbox_message_contains_references_only() -> None:
    class Redis:
        async def xadd(self, stream, fields):
            self.stream, self.fields = stream, fields
            return b"1-0"

    client = Redis()
    event = ToolOutboxRecord(
        id=uuid4(),
        tenant_id=TENANT,
        event_type="action.approved",
        aggregate_type="ActionIntent",
        aggregate_id="intent-1",
        idempotency_key="key-1",
        payload={"password": "must-not-enter-redis"},
    )
    assert await RedisStreamPublisher(client).publish(event) == "1-0"
    assert "password" not in str(client.fields)
