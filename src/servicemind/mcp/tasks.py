from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from servicemind.persistence.database import global_session, tenant_session
from servicemind.persistence.models import McpTaskRecord, Tenant
from servicemind.security.crypto import CredentialCipher


class StoredMcpTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: UUID
    status: str
    output: Any | None = None
    output_hash: str | None = None
    error_code: str | None = None
    cancellation_requested: bool = False
    created_at: datetime
    updated_at: datetime


class McpTaskStore(Protocol):
    async def recover_interrupted(self) -> int: ...

    async def heartbeat(self, tenant_id: UUID, task_id: UUID) -> bool: ...

    async def create(
        self,
        tenant_id: UUID,
        *,
        task_id: UUID,
        request_id: UUID,
        run_id: UUID,
        workflow_task_id: str,
        tool_name: str,
        argument_hash: str,
    ) -> StoredMcpTask: ...

    async def get(self, tenant_id: UUID, task_id: UUID) -> StoredMcpTask | None: ...

    async def finish(
        self,
        tenant_id: UUID,
        task_id: UUID,
        *,
        status: str,
        output: Any | None = None,
        output_hash: str | None = None,
        error_code: str | None = None,
    ) -> StoredMcpTask | None: ...

    async def cancel(self, tenant_id: UUID, task_id: UUID) -> StoredMcpTask | None: ...


class InMemoryMcpTaskStore:
    def __init__(self) -> None:
        self.values: dict[tuple[UUID, UUID], StoredMcpTask] = {}
        self.requests: dict[tuple[UUID, UUID], tuple[UUID, tuple[Any, ...]]] = {}

    async def recover_interrupted(self) -> int:
        recovered = 0
        now = datetime.now(UTC)
        for key, current in tuple(self.values.items()):
            if current.status == "working":
                self.values[key] = current.model_copy(
                    update={
                        "status": "failed",
                        "error_code": "Task interrupted by service restart",
                        "updated_at": now,
                    }
                )
                recovered += 1
        return recovered

    async def heartbeat(self, tenant_id: UUID, task_id: UUID) -> bool:
        current = self.values.get((tenant_id, task_id))
        return current is not None and current.status == "working"

    async def create(self, tenant_id: UUID, **values: Any) -> StoredMcpTask:
        request_key = (tenant_id, values["request_id"])
        identity = (
            values["run_id"],
            values["workflow_task_id"],
            values["tool_name"],
            values["argument_hash"],
        )
        previous = self.requests.get(request_key)
        if previous is not None:
            task_id, previous_identity = previous
            if previous_identity != identity:
                raise ValueError("MCP request ID was reused with different task content")
            return self.values[(tenant_id, task_id)]
        now = datetime.now(UTC)
        task = StoredMcpTask(
            task_id=values["task_id"],
            status="working",
            cancellation_requested=False,
            created_at=now,
            updated_at=now,
        )
        key = (tenant_id, task.task_id)
        if key in self.values:
            raise ValueError("MCP task already exists")
        self.values[key] = task
        self.requests[request_key] = (task.task_id, identity)
        return task

    async def get(self, tenant_id: UUID, task_id: UUID) -> StoredMcpTask | None:
        return self.values.get((tenant_id, task_id))

    async def finish(
        self,
        tenant_id: UUID,
        task_id: UUID,
        *,
        status: str,
        output: Any | None = None,
        output_hash: str | None = None,
        error_code: str | None = None,
    ) -> StoredMcpTask | None:
        key = (tenant_id, task_id)
        current = self.values.get(key)
        if current is None or current.status != "working":
            return current
        task = current.model_copy(
            update={
                "status": status,
                "output": output,
                "output_hash": output_hash,
                "error_code": error_code,
                "updated_at": datetime.now(UTC),
            }
        )
        self.values[key] = task
        return task

    async def cancel(self, tenant_id: UUID, task_id: UUID) -> StoredMcpTask | None:
        key = (tenant_id, task_id)
        current = self.values.get(key)
        if current is None or current.status != "working":
            return current
        task = current.model_copy(
            update={
                "status": "cancelled",
                "cancellation_requested": True,
                "updated_at": datetime.now(UTC),
            }
        )
        self.values[key] = task
        return task


class PostgresMcpTaskStore:
    def __init__(
        self, cipher: CredentialCipher | None = None, *, lease_seconds: int = 30
    ) -> None:
        self.cipher = cipher or CredentialCipher()
        self.lease_seconds = lease_seconds
        self.worker_id = f"mcp-{uuid4()}"

    async def recover_interrupted(self) -> int:
        """Close only expired tasks, preserving work owned by healthy replicas."""
        async with global_session() as session:
            tenant_ids = tuple((await session.scalars(select(Tenant.id))).all())
        recovered = 0
        for tenant_id in tenant_ids:
            async with tenant_session(tenant_id) as session:
                result = await session.execute(
                    update(McpTaskRecord)
                    .where(
                        McpTaskRecord.status == "working",
                        McpTaskRecord.lease_expires_at <= datetime.now(UTC),
                    )
                    .values(
                        status="failed",
                        error_code="Task interrupted by service restart",
                    )
                    .returning(McpTaskRecord.task_id)
                )
                recovered += len(result.scalars().all())
        return recovered

    async def heartbeat(self, tenant_id: UUID, task_id: UUID) -> bool:
        async with tenant_session(tenant_id) as session:
            result = await session.execute(
                update(McpTaskRecord)
                .where(
                    McpTaskRecord.task_id == task_id,
                    McpTaskRecord.status == "working",
                    McpTaskRecord.lease_owner == self.worker_id,
                )
                .values(lease_expires_at=datetime.now(UTC) + timedelta(seconds=self.lease_seconds))
                .returning(McpTaskRecord.task_id)
            )
            return result.scalar_one_or_none() is not None

    async def create(self, tenant_id: UUID, **values: Any) -> StoredMcpTask:
        async with tenant_session(tenant_id) as session:
            await session.execute(
                insert(McpTaskRecord)
                .values(
                    tenant_id=tenant_id,
                    status="working",
                    lease_owner=self.worker_id,
                    lease_expires_at=datetime.now(UTC) + timedelta(seconds=self.lease_seconds),
                    **values,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "request_id"])
            )
            row = (
                await session.execute(
                    select(McpTaskRecord).where(McpTaskRecord.request_id == values["request_id"])
                )
            ).scalar_one()
            immutable = (
                row.run_id,
                row.workflow_task_id,
                row.tool_name,
                row.argument_hash,
            )
            expected = (
                values["run_id"],
                values["workflow_task_id"],
                values["tool_name"],
                values["argument_hash"],
            )
            if immutable != expected:
                raise ValueError("MCP request ID was reused with different task content")
        return _to_task(row, self.cipher)

    async def get(self, tenant_id: UUID, task_id: UUID) -> StoredMcpTask | None:
        async with tenant_session(tenant_id) as session:
            row = await session.scalar(
                select(McpTaskRecord).where(McpTaskRecord.task_id == task_id)
            )
            if (
                row
                and row.status == "working"
                and row.lease_expires_at <= datetime.now(UTC)
            ):
                row.status = "failed"
                row.error_code = "Task executor lease expired"
                await session.flush()
            return _to_task(row, self.cipher) if row else None

    async def finish(
        self,
        tenant_id: UUID,
        task_id: UUID,
        *,
        status: str,
        output: Any | None = None,
        output_hash: str | None = None,
        error_code: str | None = None,
    ) -> StoredMcpTask | None:
        ciphertext = (
            self.cipher.encrypt(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
            if output is not None
            else None
        )
        async with tenant_session(tenant_id) as session:
            await session.execute(
                update(McpTaskRecord)
                .where(
                    McpTaskRecord.task_id == task_id,
                    McpTaskRecord.status == "working",
                    McpTaskRecord.lease_owner == self.worker_id,
                )
                .values(
                    status=status,
                    output_ciphertext=ciphertext,
                    output_hash=output_hash,
                    error_code=error_code,
                )
            )
            row = await session.scalar(
                select(McpTaskRecord).where(McpTaskRecord.task_id == task_id)
            )
            return _to_task(row, self.cipher) if row else None

    async def cancel(self, tenant_id: UUID, task_id: UUID) -> StoredMcpTask | None:
        async with tenant_session(tenant_id) as session:
            await session.execute(
                update(McpTaskRecord)
                .where(McpTaskRecord.task_id == task_id, McpTaskRecord.status == "working")
                .values(status="cancelled", cancellation_requested=True)
            )
            row = await session.scalar(
                select(McpTaskRecord).where(McpTaskRecord.task_id == task_id)
            )
            return _to_task(row, self.cipher) if row else None


def _to_task(row: McpTaskRecord, cipher: CredentialCipher) -> StoredMcpTask:
    output = json.loads(cipher.decrypt(row.output_ciphertext)) if row.output_ciphertext else None
    return StoredMcpTask(
        task_id=row.task_id,
        status=row.status,
        output=output,
        output_hash=row.output_hash,
        error_code=row.error_code,
        cancellation_requested=row.cancellation_requested,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
