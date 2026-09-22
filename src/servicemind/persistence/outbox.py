"""Transactional outbox persistence, owned by the database adapter layer."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from servicemind.persistence.database import tenant_session
from servicemind.persistence.models import ToolOutboxRecord


class ToolOutboxRepository:
    def __init__(self, tenant_id: UUID) -> None:
        self.tenant_id = tenant_id

    @staticmethod
    async def enqueue_in_transaction(
        session: AsyncSession,
        *,
        tenant_id: UUID,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> None:
        await session.execute(
            insert(ToolOutboxRecord)
            .values(
                id=uuid4(),
                tenant_id=tenant_id,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                event_type=event_type,
                payload=payload,
                idempotency_key=idempotency_key,
                status="pending",
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "idempotency_key"])
        )

    async def claim(
        self, worker_id: str, *, limit: int = 20, lease_seconds: int = 30
    ) -> list[ToolOutboxRecord]:
        now = datetime.now(UTC)
        async with tenant_session(self.tenant_id) as session:
            rows = list(
                (
                    await session.execute(
                        select(ToolOutboxRecord)
                        .where(
                            ToolOutboxRecord.available_at <= now,
                            or_(
                                ToolOutboxRecord.status == "pending",
                                (ToolOutboxRecord.status == "leased")
                                & (ToolOutboxRecord.lease_expires_at < now),
                            ),
                        )
                        .order_by(ToolOutboxRecord.created_at, ToolOutboxRecord.id)
                        .with_for_update(skip_locked=True)
                        .limit(limit)
                    )
                ).scalars()
            )
            for row in rows:
                row.status = "leased"
                row.lease_owner = worker_id
                row.lease_expires_at = now + timedelta(seconds=lease_seconds)
                row.attempts += 1
            await session.flush()
            return rows

    async def published(self, event_id: UUID, worker_id: str) -> None:
        async with tenant_session(self.tenant_id) as session:
            row = (
                await session.execute(
                    select(ToolOutboxRecord)
                    .where(ToolOutboxRecord.id == event_id)
                    .with_for_update()
                )
            ).scalar_one()
            if row.status != "leased" or row.lease_owner != worker_id:
                raise PermissionError("outbox lease is not owned by this worker")
            row.status = "published"
            row.lease_owner = None
            row.lease_expires_at = None
            await session.flush()

    async def failed(
        self, event_id: UUID, worker_id: str, error_code: str, *, max_attempts: int = 8
    ) -> str:
        async with tenant_session(self.tenant_id) as session:
            row = (
                await session.execute(
                    select(ToolOutboxRecord)
                    .where(ToolOutboxRecord.id == event_id)
                    .with_for_update()
                )
            ).scalar_one()
            if row.status != "leased" or row.lease_owner != worker_id:
                raise PermissionError("outbox lease is not owned by this worker")
            row.error_code = error_code[:100]
            row.lease_owner = None
            row.lease_expires_at = None
            if row.attempts >= max_attempts:
                row.status = "dead"
            else:
                row.status = "pending"
                ceiling = min(2 ** max(row.attempts - 1, 0), 300)
                row.available_at = datetime.now(UTC) + timedelta(seconds=random.uniform(0, ceiling))
            await session.flush()
            return row.status
