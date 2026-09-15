from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress

from redis.asyncio import Redis
from sqlalchemy import select

from core import settings
from servicemind.persistence.database import close_database, global_session
from servicemind.persistence.models import Tenant
from servicemind.reliability.outbox import OutboxRelay, RedisStreamPublisher

logger = logging.getLogger("servicemind.reliability.worker")


async def _tenant_ids():
    async with global_session() as session:
        return tuple((await session.scalars(select(Tenant.id).order_by(Tenant.id))).all())


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
        while not stop.is_set():
            processed = 0
            for tenant_id in await _tenant_ids():
                try:
                    processed += await relay.once(tenant_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Outbox relay failed for tenant_id=%s", tenant_id)
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
