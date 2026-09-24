"""Transactional outbox persistence, owned by the database adapter layer."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from servicemind.persistence.database import tenant_session
from servicemind.persistence.models import ToolOutboxRecord

#: How many rows one ``prune_published`` call removes at most. It is also the caller's
#: stopping rule -- a batch shorter than this means the query found no more -- so it is
#: named here rather than left as a default argument the caller has to re-state and can
#: therefore get out of step with.
PRUNE_BATCH_LIMIT = 1_000


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

    async def prune_published(self, *, retention: timedelta, limit: int = PRUNE_BATCH_LIMIT) -> int:
        """Drop published rows whose delivery record has aged out.

        A published row is the completion record of a delivered event, not work waiting
        to be done: nothing reads this table to deliver it, because delivery happened when
        the event reached the stream. Without a retention the table's steady state is
        "every event this tenant ever raised", which is a capacity problem before it is a
        governance one -- and an unbounded queue table is the kind of thing that is only
        noticed at the worst time.

        Age is measured from ``updated_at``, which the ORM advances on every write and
        which therefore lands on the publication instant, rather than from ``created_at``.
        A row that waited a day in the queue has been *delivered* for zero days, and
        charging it that day would shrink exactly the records that took longest to
        deliver.

        ``dead`` rows are deliberately kept. A row that exhausted its attempts is the
        only evidence that an integration event was never delivered, and deleting it would
        make an undelivered event indistinguishable from one that was never raised. The
        compliance record for an approved action lives in ``audit_events``; this is a
        delivery record, which is why a finite retention is the right shape for it.

        ``limit`` bounds one call so the worker's loop cannot stall behind a backlog.
        """
        cutoff = datetime.now(UTC) - retention
        async with tenant_session(self.tenant_id) as session:
            expired = list(
                (
                    await session.execute(
                        select(ToolOutboxRecord.id)
                        .where(
                            ToolOutboxRecord.status == "published",
                            ToolOutboxRecord.updated_at < cutoff,
                        )
                        .order_by(ToolOutboxRecord.updated_at)
                        .limit(limit)
                    )
                ).scalars()
            )
            if not expired:
                return 0
            await session.execute(delete(ToolOutboxRecord).where(ToolOutboxRecord.id.in_(expired)))
            return len(expired)

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
