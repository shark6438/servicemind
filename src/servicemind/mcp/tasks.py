from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert

from servicemind.persistence.database import global_session, tenant_session
from servicemind.persistence.models import McpTaskRecord, Tenant
from servicemind.security.crypto import CredentialCipher

#: How long a task stays queryable after it was created. The server advertises this to
#: every MCP client as ``ttlMs`` on every task payload, which makes it a promise about how
#: long a result can still be fetched -- and until now nothing kept it. A client that read
#: the TTL and a platform that ignored it disagreed about whether a task still existed,
#: and the in-memory store, which is the default, kept every task and every idempotency
#: record its process had ever created, each one holding the full tool output, with
#: nothing ever released. One number, declared where the retention is enforced and quoted
#: by the payload that announces it, so the two cannot drift apart again.
TASK_TTL_MS = 3_600_000


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


def _expired(task: StoredMcpTask, now: datetime) -> bool:
    return task.created_at + timedelta(milliseconds=TASK_TTL_MS) <= now


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

    def _forget_expired(self, now: datetime) -> None:
        """Release every task past ``TASK_TTL_MS``, and its idempotency record with it.

        The two maps have to be released together. ``create`` resolves a repeated request
        id through ``requests`` and then indexes ``values`` by the task id it finds there,
        so forgetting a task while keeping its request record turns a late retry -- the
        exact thing idempotency exists for -- into a ``KeyError``.
        """
        expired = {key for key, task in self.values.items() if _expired(task, now)}
        if not expired:
            return
        for key in expired:
            del self.values[key]
        for request_key, (task_id, _identity) in tuple(self.requests.items()):
            if (request_key[0], task_id) in expired:
                del self.requests[request_key]

    async def recover_interrupted(self) -> int:
        recovered = 0
        now = datetime.now(UTC)
        self._forget_expired(now)
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
        self._forget_expired(datetime.now(UTC))
        current = self.values.get((tenant_id, task_id))
        return current is not None and current.status == "working"

    async def create(self, tenant_id: UUID, **values: Any) -> StoredMcpTask:
        self._forget_expired(datetime.now(UTC))
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
        self._forget_expired(datetime.now(UTC))
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
        self._forget_expired(datetime.now(UTC))
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
        self._forget_expired(datetime.now(UTC))
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
    def __init__(self, cipher: CredentialCipher | None = None, *, lease_seconds: int = 30) -> None:
        self.cipher = cipher or CredentialCipher()
        self.lease_seconds = lease_seconds
        self.worker_id = f"mcp-{uuid4()}"

    async def recover_interrupted(self) -> int:
        """Close expired tasks and release finished ones past their TTL.

        Closing preserves work owned by healthy replicas: only tasks whose lease has run
        out are failed. Releasing is the other half -- a row answers a task lookup for
        ``TASK_TTL_MS`` and then has to stop existing, or the table accumulates every task
        the deployment has ever run, each holding an encrypted tool output.
        """
        async with global_session() as session:
            tenant_ids = tuple((await session.scalars(select(Tenant.id))).all())
        recovered = 0
        now = datetime.now(UTC)
        for tenant_id in tenant_ids:
            async with tenant_session(tenant_id) as session:
                result = await session.execute(
                    update(McpTaskRecord)
                    .where(
                        McpTaskRecord.status == "working",
                        McpTaskRecord.lease_expires_at <= now,
                    )
                    .values(
                        status="failed",
                        error_code="Task interrupted by service restart",
                    )
                    .returning(McpTaskRecord.task_id)
                )
                recovered += len(result.scalars().all())
                await session.execute(
                    delete(McpTaskRecord).where(
                        McpTaskRecord.created_at <= now - timedelta(milliseconds=TASK_TTL_MS)
                    )
                )
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
            if row and _record_expired(row, datetime.now(UTC)):
                # Past the TTL the server advertised for it, the task is no longer a task
                # -- answering with its output would keep a result retrievable for longer
                # than the client was told, which is the disagreement the TTL exists to
                # settle.
                return None
            if row and row.status == "working" and row.lease_expires_at <= datetime.now(UTC):
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


def _record_expired(row: McpTaskRecord, now: datetime) -> bool:
    return row.created_at + timedelta(milliseconds=TASK_TTL_MS) <= now


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
