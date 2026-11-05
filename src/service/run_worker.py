"""Process bootstrap for ServiceMind's independently deployed Agent worker."""

import asyncio
import logging
import signal
from contextlib import suppress

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from core import settings
from core.settings import DatabaseType
from memory import initialize_database
from servicemind.observability.tracing import configure_telemetry, shutdown_telemetry
from servicemind.orchestration.run_worker import RunWorker
from servicemind.orchestration.supervisor_workflow import configure_supervisor_checkpointer
from servicemind.orchestration.workflow import configure_phase2_checkpointer
from servicemind.persistence.database import close_database
from servicemind.persistence.run_queue import guard_engine


async def serve() -> None:
    if settings.SERVICEMIND_RUN_EXECUTION_MODE != "worker":
        raise RuntimeError("Run worker requires SERVICEMIND_RUN_EXECUTION_MODE=worker")
    if settings.DATABASE_TYPE != DatabaseType.POSTGRES:
        raise RuntimeError("Run worker requires PostgreSQL checkpointer and run database")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    configure_telemetry()
    try:
        async with initialize_database() as saver:
            if not isinstance(saver, AsyncPostgresSaver):
                raise RuntimeError("Run worker requires an asynchronous PostgreSQL checkpointer")
            if settings.POSTGRES_AUTO_SETUP:
                await saver.setup()
            configure_phase2_checkpointer(saver)
            configure_supervisor_checkpointer(saver)
            worker = RunWorker(
                per_tenant_limit=settings.SERVICEMIND_RUN_WORKER_PER_TENANT_LIMIT,
                concurrency=settings.SERVICEMIND_RUN_WORKER_CONCURRENCY,
                lease_seconds=settings.SERVICEMIND_RUN_WORKER_LEASE_SECONDS,
                poll_seconds=settings.SERVICEMIND_RUN_WORKER_POLL_SECONDS,
                shutdown_timeout=settings.GRACEFUL_SHUTDOWN_TIMEOUT,
            )
            await worker.run(stop)
    finally:
        await close_database()
        await guard_engine.dispose()
        shutdown_telemetry()


def main() -> None:
    logging.basicConfig(level=settings.LOG_LEVEL.value)
    asyncio.run(serve())


if __name__ == "__main__":
    main()
