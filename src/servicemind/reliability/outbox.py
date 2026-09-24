from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any, Protocol
from uuid import UUID

from servicemind.persistence.models import ToolOutboxRecord
from servicemind.persistence.outbox import ToolOutboxRepository

#: How much of the delivery stream Redis keeps. The stream is a notification feed for
#: integration consumers, and it had no bound at all: an `XADD` without `MAXLEN` grows
#: forever, so a stream whose consumer is slow, absent, or newly added on a busy tenant
#: is a Redis instance that fills its memory and starts refusing writes -- taking the
#: outbox down with it. The bound is approximate (`~`), which lets Redis trim whole
#: macro-nodes in O(1) instead of scanning, so the cost of publishing does not depend on
#: the length of the stream.
#:
#: Trimming is only safe because the stream is not the durable record. The outbox row is:
#: it is committed in the same transaction as the change that raised the event, it is
#: what a consumer that missed an entry reconciles against, and it outlives the stream
#: entry by design. A consumer that needs history beyond this bound reads the table.
STREAM_MAXLEN = 100_000

#: How long a published row is kept before ``prune_published`` removes it. Thirty days is
#: the window in which an operator would still want to ask "was this event delivered?";
#: past it the delivery record has served its purpose, and the durable answer to "what
#: happened" is an audit event rather than a queue row.
PUBLISHED_RETENTION = timedelta(days=30)


class OutboxPublisher(Protocol):
    async def publish(self, event: ToolOutboxRecord) -> str: ...


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

    async def prune(self, tenant_id: UUID, *, retention: timedelta = PUBLISHED_RETENTION) -> int:
        """Retire delivered rows for one tenant.

        Separate from ``once`` on purpose: the relay is called on the order of a second,
        and a table sweep belongs on a clock measured in hours. Running it from a
        schedule rather than from the delivery loop keeps publishing O(claimed events)
        however much history the tenant has accumulated.
        """
        return await ToolOutboxRepository(tenant_id).prune_published(retention=retention)


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
            maxlen=STREAM_MAXLEN,
            approximate=True,
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
