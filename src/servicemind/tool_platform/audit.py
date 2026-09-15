from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from servicemind.persistence.database import tenant_session
from servicemind.persistence.models import GovernedToolInvocationRecord, ToolPolicyDecisionRecord
from servicemind.tool_platform.contracts import ToolCall, ToolDefinition, ToolPolicyDecision


class ToolInvocationAudit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    tenant_id: UUID
    run_id: UUID
    task_id: str
    user_id: str
    tool_name: str
    tool_version: str
    tool_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider: str
    argument_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    policy_decision_id: UUID
    status: str
    verified: bool = False
    attempts: int = Field(ge=0, le=5)
    latency_ms: float = Field(ge=0)
    error_code: str | None = Field(default=None, max_length=100)


class ToolAuditSink(Protocol):
    async def record_policy(
        self, definition: ToolDefinition, call: ToolCall, decision: ToolPolicyDecision
    ) -> None: ...

    async def record_invocation(self, audit: ToolInvocationAudit) -> None: ...


class InMemoryToolAuditSink:
    def __init__(self) -> None:
        self.policy: list[tuple[ToolDefinition, ToolCall, ToolPolicyDecision]] = []
        self.invocations: list[ToolInvocationAudit] = []

    async def record_policy(
        self, definition: ToolDefinition, call: ToolCall, decision: ToolPolicyDecision
    ) -> None:
        self.policy.append((definition, call, decision))

    async def record_invocation(self, audit: ToolInvocationAudit) -> None:
        self.invocations.append(audit)


class PostgresToolAuditSink:
    async def record_policy(
        self, definition: ToolDefinition, call: ToolCall, decision: ToolPolicyDecision
    ) -> None:
        async with tenant_session(call.tenant_id) as session:
            session.add(
                ToolPolicyDecisionRecord(
                    tenant_id=call.tenant_id,
                    decision_id=decision.decision_id,
                    request_id=call.request_id,
                    run_id=call.run_id,
                    task_id=call.task_id,
                    user_id=call.user_id,
                    tool_name=definition.name,
                    tool_version=definition.version,
                    tool_checksum=definition.checksum,
                    argument_hash=call.argument_hash,
                    allow=decision.allow,
                    requires_approval=decision.requires_approval,
                    policy_version=decision.policy_version,
                    external_decision_id=decision.external_decision_id,
                    reason_codes=list(decision.reason_codes),
                    context_manifest=audit_payload(call),
                )
            )
            await session.flush()

    async def record_invocation(self, audit: ToolInvocationAudit) -> None:
        async with tenant_session(audit.tenant_id) as session:
            await session.execute(
                insert(GovernedToolInvocationRecord)
                .values(**audit.model_dump())
                .on_conflict_do_nothing(index_elements=["tenant_id", "request_id"])
            )

    async def verify_receipt(
        self, tenant_id: UUID, request_id: UUID, expected_output_hash: str
    ) -> bool:
        """Verify an MCP receipt against the durable, RLS-scoped invocation ledger."""
        async with tenant_session(tenant_id) as session:
            value = await session.scalar(
                select(GovernedToolInvocationRecord.output_hash).where(
                    GovernedToolInvocationRecord.request_id == request_id,
                    GovernedToolInvocationRecord.status == "succeeded",
                    GovernedToolInvocationRecord.verified.is_(True),
                )
            )
            return value == expected_output_hash


def audit_payload(call: ToolCall) -> dict[str, Any]:
    """Hash-only representation safe for a durable policy audit table."""
    return {
        "request_id": str(call.request_id),
        "run_id": str(call.run_id),
        "task_id": call.task_id,
        "user_id": call.user_id,
        "tool_name": call.tool_name,
        "tool_version": call.tool_version,
        "argument_hash": call.argument_hash,
        "roles": sorted(call.roles),
        "entity_ids": sorted(call.entity_ids),
        "taint_labels": sorted(call.taint_labels),
    }
