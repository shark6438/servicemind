from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

load_dotenv()

from core import settings  # noqa: E402
from servicemind.persistence.models import Base  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = settings.SERVICEMIND_MIGRATION_DATABASE_URL or settings.SERVICEMIND_DATABASE_URL
if database_url is None:
    raise RuntimeError("ServiceMind migration database URL is not configured")
config.set_main_option("sqlalchemy.url", database_url.get_secret_value().replace("%", "%%"))
target_metadata = Base.metadata

# LangGraph owns and migrates these tables through its checkpoint/store libraries.
# Excluding them prevents ServiceMind's Alembic chain from proposing destructive
# drops for schema that deliberately lives outside Base.metadata.
EXTERNAL_TABLES = {
    "checkpoint_migrations",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoints",
    "store_migrations",
    "store",
}


def include_object(object_, name: str | None, type_: str, reflected: bool, compare_to) -> bool:
    del object_, compare_to
    return not (reflected and type_ == "table" and name in EXTERNAL_TABLES)


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
