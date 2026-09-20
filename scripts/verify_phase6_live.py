"""Live Phase 6 checks across PostgreSQL, OPA, Redis, and the tool gateway."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from redis.asyncio import Redis
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from core import settings
from servicemind.mcp.tasks import PostgresMcpTaskStore
from servicemind.persistence.database import close_database, global_session, tenant_session
from servicemind.persistence.models import (
    GovernedToolInvocationRecord,
    McpTaskRecord,
    ToolOutboxRecord,
    ToolPolicyDecisionRecord,
)
from servicemind.persistence.repository import ServiceMindRepository
from servicemind.reliability.outbox import ToolOutboxRepository
from servicemind.tool_platform.audit import PostgresToolAuditSink
from servicemind.tool_platform.catalog import build_glpi_registry
from servicemind.tool_platform.contracts import ProviderResult, ToolCall, ToolDefinition
from servicemind.tool_platform.gateway import ToolGateway
from servicemind.tool_platform.policy import OpaToolPolicy
from servicemind.tool_platform.resilience import RateLimitExceeded, RedisTenantRateLimiter

ACME = UUID("11111111-1111-4111-8111-111111111111")
GLOBEX = UUID("22222222-2222-4222-8222-222222222222")


class AcceptanceProvider:
    name = "native_glpi"

    async def execute(self, definition: ToolDefinition, call: ToolCall) -> ProviderResult:
        del definition, call
        return ProviderResult(output={"tickets": [{"id": 1, "name": "Phase 6 probe"}]})

    async def verify(
        self, definition: ToolDefinition, call: ToolCall, result: ProviderResult
    ) -> bool:
        del definition, call
        return result.output == {"tickets": [{"id": 1, "name": "Phase 6 probe"}]}


async def assert_append_only(table: str, row_id: UUID) -> None:
    try:
        async with tenant_session(ACME) as session:
            await session.execute(
                text(f'UPDATE "{table}" SET created_at = created_at WHERE id = :id'),
                {"id": row_id},
            )
    except DBAPIError as exc:
        if "append-only" not in str(exc.orig):
            raise
    else:
        raise AssertionError(f"{table} UPDATE unexpectedly succeeded")


def decode_fields(fields: dict[Any, Any]) -> dict[str, str]:
    return {
        (key.decode() if isinstance(key, bytes) else str(key)): (
            value.decode() if isinstance(value, bytes) else str(value)
        )
        for key, value in fields.items()
    }


async def wait_for_outbox_delivery(
    idempotency_key: str, redis: Redis, *, timeout_seconds: float = 10
) -> dict[str, str]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        async with tenant_session(ACME) as session:
            status = await session.scalar(
                select(ToolOutboxRecord.status).where(
                    ToolOutboxRecord.idempotency_key == idempotency_key
                )
            )
        if status == "published":
            for message_id, values in await redis.xrevrange("servicemind:tool-events", count=100):
                fields = decode_fields(values)
                if fields.get("idempotency_key") == idempotency_key:
                    fields["_message_id"] = (
                        message_id.decode() if isinstance(message_id, bytes) else str(message_id)
                    )
                    return fields
        await asyncio.sleep(0.2)
    raise AssertionError("outbox worker did not publish the acceptance event")


async def main() -> None:
    if not settings.SERVICEMIND_OPA_URL or not settings.SERVICEMIND_REDIS_URL:
        raise RuntimeError("Phase 6 live verification requires OPA and Redis")
    redis = Redis.from_url(
        settings.SERVICEMIND_REDIS_URL.get_secret_value(),
        decode_responses=False,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    try:
        await redis.ping()
        run = await ServiceMindRepository(ACME).create_run(
            user_id="phase6-live-verifier",
            ticket_id=1,
            goal="Phase 6 governed tool platform verification",
            request_write=False,
        )
        provider = AcceptanceProvider()
        audit = PostgresToolAuditSink()
        gateway = ToolGateway(
            registry=build_glpi_registry(provider.name),
            policy=OpaToolPolicy(
                settings.SERVICEMIND_OPA_URL,
                decision_path=settings.SERVICEMIND_OPA_DECISION_PATH,
                policy_version=settings.SERVICEMIND_TOOL_POLICY_VERSION,
            ),
            providers={provider.name: provider},
            audit=audit,
        )
        request_id = uuid4()
        result = await gateway.execute(
            ToolCall(
                request_id=request_id,
                tenant_id=ACME,
                run_id=run.id,
                task_id="phase6-live-tool",
                user_id="phase6-live-verifier",
                roles=frozenset({"analyst"}),
                entity_ids=frozenset({1}),
                capabilities=frozenset({"glpi.search_tickets"}),
                tool_name="glpi.search_tickets",
                tool_version="1.0.0",
                arguments={"query": "phase6", "limit": 1},
                deadline=datetime.now(UTC) + timedelta(seconds=20),
            )
        )
        assert result.verified
        assert await audit.verify_receipt(ACME, request_id, result.output_hash)

        async with tenant_session(ACME) as session:
            decision_id, invocation_id = (
                await session.execute(
                    select(ToolPolicyDecisionRecord.id, GovernedToolInvocationRecord.id)
                    .join(
                        GovernedToolInvocationRecord,
                        GovernedToolInvocationRecord.policy_decision_id
                        == ToolPolicyDecisionRecord.decision_id,
                    )
                    .where(ToolPolicyDecisionRecord.request_id == request_id)
                )
            ).one()
        await assert_append_only("tool_policy_decisions", decision_id)
        await assert_append_only("governed_tool_invocations", invocation_id)

        confidential_marker = f"confidential-phase6-{uuid4()}"
        task_store = PostgresMcpTaskStore(lease_seconds=5)
        task_id = uuid4()
        await task_store.create(
            ACME,
            task_id=task_id,
            request_id=uuid4(),
            run_id=run.id,
            workflow_task_id="phase6-live-mcp",
            tool_name="glpi.query_cmdb_dependencies",
            argument_hash="a" * 64,
        )
        await task_store.finish(
            ACME,
            task_id,
            status="completed",
            output={"value": confidential_marker},
            output_hash="b" * 64,
        )
        stored_task = await task_store.get(ACME, task_id)
        assert stored_task is not None and stored_task.output == {"value": confidential_marker}
        assert await task_store.get(GLOBEX, task_id) is None
        async with tenant_session(ACME) as session:
            ciphertext = await session.scalar(
                select(McpTaskRecord.output_ciphertext).where(McpTaskRecord.task_id == task_id)
            )
        assert ciphertext and confidential_marker not in ciphertext

        expired_store = PostgresMcpTaskStore(lease_seconds=1)
        expired_task_id = uuid4()
        await expired_store.create(
            ACME,
            task_id=expired_task_id,
            request_id=uuid4(),
            run_id=run.id,
            workflow_task_id="phase6-live-expired",
            tool_name="glpi.query_cmdb_dependencies",
            argument_hash="c" * 64,
        )
        await asyncio.sleep(1.1)
        assert await PostgresMcpTaskStore().recover_interrupted() >= 1
        expired = await expired_store.get(ACME, expired_task_id)
        assert expired is not None and expired.status == "failed"

        event_key = f"phase6-live:{uuid4()}"
        async with tenant_session(ACME) as session:
            await ToolOutboxRepository.enqueue_in_transaction(
                session,
                tenant_id=ACME,
                aggregate_type="Acceptance",
                aggregate_id=str(run.id),
                event_type="phase6.acceptance",
                payload={"confidential": confidential_marker},
                idempotency_key=event_key,
            )
        stream_fields = await wait_for_outbox_delivery(event_key, redis)
        assert "payload" not in stream_fields
        assert confidential_marker not in "".join(stream_fields.values())
        await redis.xdel("servicemind:tool-events", stream_fields.pop("_message_id"))

        limiter_key = f"phase6-live-{uuid4()}"
        limiter = RedisTenantRateLimiter(redis, limit=1, window_seconds=5)
        await limiter.acquire(ACME, limiter_key)
        try:
            await limiter.acquire(ACME, limiter_key)
        except RateLimitExceeded:
            pass
        else:
            raise AssertionError("Redis tenant rate limit did not deny the second call")
        await redis.delete(f"servicemind:tool-rate:{ACME}:{limiter_key}")

        tables = [
            "tool_policy_decisions",
            "governed_tool_invocations",
            "tool_outbox",
            "mcp_tasks",
        ]
        async with global_session() as session:
            revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
            rls_rows = (
                await session.execute(
                    text(
                        "SELECT relname, relrowsecurity AND relforcerowsecurity "
                        "FROM pg_class WHERE relname = ANY(:tables)"
                    ),
                    {"tables": tables},
                )
            ).all()
            rls = {str(row[0]): bool(row[1]) for row in rls_rows}
            policy_count = await session.scalar(
                text(
                    "SELECT count(*) FROM pg_policies WHERE tablename = ANY(:tables) "
                    "AND policyname LIKE '%tenant_isolation'"
                ),
                {"tables": tables},
            )
            zero_context = await session.scalar(
                select(func.count()).select_from(GovernedToolInvocationRecord)
            )
        assert revision == "0013_phase6_hash_guards"
        assert rls == {name: True for name in tables}
        assert policy_count == 4
        assert zero_context == 0
        print(
            "PASS phase6 live: OPA gateway/audit/receipt, append-only+RLS, encrypted durable "
            "MCP task leases, Redis rate limit, PostgreSQL outbox -> metadata-only Redis stream"
        )
    finally:
        await redis.aclose()
        await close_database()


if __name__ == "__main__":
    asyncio.run(main())
