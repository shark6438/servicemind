from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core import settings


def _database_url() -> str:
    if settings.SERVICEMIND_DATABASE_URL is None:
        raise RuntimeError("SERVICEMIND_DATABASE_URL is not configured")
    return settings.SERVICEMIND_DATABASE_URL.get_secret_value()


engine = create_async_engine(_database_url(), pool_pre_ping=True, pool_size=5, max_overflow=5)
session_factory = async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def tenant_session(tenant_id: UUID) -> AsyncIterator[AsyncSession]:
    """Open a transaction whose RLS tenant cannot be changed by application queries."""
    async with session_factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )
        yield session


@asynccontextmanager
async def global_session() -> AsyncIterator[AsyncSession]:
    """Open a transaction for non-RLS tables such as the tenant catalog."""
    async with session_factory() as session, session.begin():
        yield session


async def close_database() -> None:
    await engine.dispose()
