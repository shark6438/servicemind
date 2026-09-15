from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from servicemind.persistence.database import tenant_session
from servicemind.persistence.models import ToolOutboxRecord


class OutboxPublisher(Protocol):
    async def publish(self, event: ToolOutboxRecord) -> str: ...


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


class OutboxRelay:
    def __init__(self, publisher: OutboxPublisher, *, worker_id: str) -> None:
        self.publisher, self.worker_id = publisher, worker_id

    async def once(self, tenant_id: UUID) -> int:
        repository = ToolOutboxRepository(tenant_id)
        events = await repository.claim(self.worker_id)
        for event in events:
            try:
                await self.publisher.publish(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await repository.failed(event.id, self.worker_id, type(exc).__name__)
            else:
                await repository.published(event.id, self.worker_id)
        return len(events)


class RedisStreamPublisher:
    def __init__(self, client: Any, *, stream: str = "servicemind:tool-events") -> None:
        self.client, self.stream = client, stream

    async def publish(self, event: ToolOutboxRecord) -> str:
        # Raw arguments/results never enter Redis; payload is a durable record
        # reference plus bounded event metadata.
        message_id = await self.client.xadd(
            self.stream,
            {
                "event_id": str(event.id),
                "tenant_id": str(event.tenant_id),
                "event_type": event.event_type,
                "aggregate_type": event.aggregate_type,
                "aggregate_id": event.aggregate_id,
                "idempotency_key": event.idempotency_key,
            },
        )
        return message_id.decode() if isinstance(message_id, bytes) else str(message_id)


class RedisStreamConsumer:
    """At-least-once consumer with PEL recovery; handlers must be idempotent."""

    def __init__(self, client: Any, *, stream: str, group: str, consumer: str) -> None:
        self.client, self.stream, self.group, self.consumer = client, stream, group, consumer

    async def ensure_group(self) -> None:
        try:
            await self.client.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def once(self, handler: Callable[[dict[str, str]], Awaitable[None]]) -> int:
        await self.ensure_group()
        batches = await self.client.xreadgroup(
            self.group, self.consumer, {self.stream: ">"}, count=10, block=1000
        )
        processed = 0
        for _, messages in batches:
            for message_id, fields in messages:
                decoded = {
                    (key.decode() if isinstance(key, bytes) else str(key)): (
                        value.decode() if isinstance(value, bytes) else str(value)
                    )
                    for key, value in fields.items()
                }
                await handler(decoded)
                await self.client.xack(self.stream, self.group, message_id)
                processed += 1
        return processed

    async def reclaim(self, min_idle_ms: int = 30_000) -> int:
        await self.ensure_group()
        result = await self.client.xautoclaim(
            self.stream, self.group, self.consumer, min_idle_ms, "0-0", count=100
        )
        return len(result[1]) if len(result) > 1 else 0
