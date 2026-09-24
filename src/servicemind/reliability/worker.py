from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import select

from core import settings
from servicemind.persistence.database import close_database, global_session
from servicemind.persistence.models import Tenant
from servicemind.persistence.outbox import PRUNE_BATCH_LIMIT
from servicemind.reliability.outbox import OutboxRelay, RedisStreamPublisher

logger = logging.getLogger("servicemind.reliability.worker")

#: How often the delivery loop sweeps aged-out published rows. The loop itself runs
#: about once a second; retention is measured in weeks, so an hour between sweeps is
#: already far finer than the thing it is maintaining. It is a separate cadence rather
#: than a per-iteration step so that the cost of publishing an event does not grow with
#: the history behind it.
PRUNE_INTERVAL_SECONDS = 3_600


async def _tenant_ids():
    async with global_session() as session:
        return tuple((await session.scalars(select(Tenant.id).order_by(Tenant.id))).all())


#: Batches of ``prune_published``'s default page size the sweep will take in one go. A
#: tenant that has been accumulating for years holds more aged-out rows than one page,
#: and a sweep that removed a thousand an hour would need weeks to catch up on its own
#: schedule -- so the sweep repeats until a batch comes back short. The ceiling is what
#: keeps that from turning into an unbounded pause in a delivery loop that also has
#: events to publish: fifty thousand rows per sweep, once an hour, is faster than the
#: rate at which approvals create them.
PRUNE_BATCHES_PER_SWEEP = 50


async def _prune_expired(relay: OutboxRelay, tenant_ids: tuple[UUID, ...]) -> None:
    for tenant_id in tenant_ids:
        try:
            for _ in range(PRUNE_BATCHES_PER_SWEEP):
                pruned = await relay.prune(tenant_id)
                if pruned < PRUNE_BATCH_LIMIT:
                    break
                logger.info(
                    "Outbox retention removed %d delivered rows for tenant_id=%s",
                    pruned,
                    tenant_id,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Outbox retention failed for tenant_id=%s", tenant_id)


async def run() -> None:
    if settings.SERVICEMIND_REDIS_URL is None:
        raise RuntimeError("SERVICEMIND_REDIS_URL is required by the outbox worker")
    client = Redis.from_url(
        settings.SERVICEMIND_REDIS_URL.get_secret_value(),
        decode_responses=False,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(name, stop.set)
    relay = OutboxRelay(RedisStreamPublisher(client), worker_id=f"outbox-{id(stop)}")
    try:
        await client.ping()
        # Sweep at start-up as well as on the interval: a process that has been down for
        # a month is exactly when the most rows have aged out, and waiting an hour to
        # notice would be waiting for no reason.
        next_prune = loop.time()
        while not stop.is_set():
            processed = 0
            tenant_ids = await _tenant_ids()
            for tenant_id in tenant_ids:
                try:
                    processed += await relay.once(tenant_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Outbox relay failed for tenant_id=%s", tenant_id)
            if loop.time() >= next_prune:
                next_prune = loop.time() + PRUNE_INTERVAL_SECONDS
                await _prune_expired(relay, tenant_ids)
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.2 if processed else 1.0)
            except TimeoutError:
                pass
    finally:
        await client.aclose()
        await close_database()


def main() -> None:
    logging.basicConfig(level=settings.LOG_LEVEL.value)
    asyncio.run(run())


if __name__ == "__main__":
    main()
