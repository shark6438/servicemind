import asyncio
import sys

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres import AsyncPostgresStore
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from core import settings


async def main() -> None:
    if settings.SERVICEMIND_MIGRATION_DATABASE_URL is None:
        raise RuntimeError("SERVICEMIND_MIGRATION_DATABASE_URL is not configured")
    connection_string = settings.SERVICEMIND_MIGRATION_DATABASE_URL.get_secret_value().replace(
        "postgresql+psycopg://", "postgresql://"
    )
    async with AsyncConnectionPool(
        connection_string,
        min_size=1,
        max_size=2,
        kwargs={"autocommit": True, "row_factory": dict_row},
    ) as pool:
        await AsyncPostgresSaver(pool).setup()  # type: ignore[bad-argument-type]
        await AsyncPostgresStore(pool).setup()  # type: ignore[bad-argument-type]
    print("LangGraph PostgreSQL schemas initialized")


if __name__ == "__main__":
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(main(), loop_factory=loop_factory)
