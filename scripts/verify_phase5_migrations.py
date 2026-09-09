"""Prove the complete Alembic chain on an isolated disposable PostgreSQL database."""

from __future__ import annotations

import os
import subprocess
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

from core import settings


def main() -> None:
    if settings.SERVICEMIND_MIGRATION_DATABASE_URL is None:
        raise RuntimeError("SERVICEMIND_MIGRATION_DATABASE_URL is required")
    source = make_url(settings.SERVICEMIND_MIGRATION_DATABASE_URL.get_secret_value())
    database_name = f"servicemind_phase5_{uuid4().hex[:12]}"
    admin_url = source.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )
    migration_url = source.set(database=database_name).render_as_string(hide_password=False)
    runtime_url = source.set(drivername="postgresql", database=database_name).render_as_string(
        hide_password=False
    )
    with psycopg.connect(admin_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    try:
        environment = os.environ.copy()
        environment["SERVICEMIND_MIGRATION_DATABASE_URL"] = migration_url
        environment["SERVICEMIND_DATABASE_URL"] = migration_url
        for alembic_args in (("upgrade", "head"), ("downgrade", "base"), ("upgrade", "head")):
            subprocess.run(
                [".venv/bin/alembic", *alembic_args],
                check=True,
                env=environment,
                stdout=subprocess.DEVNULL,
            )
        with psycopg.connect(runtime_url) as connection:
            revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
            table_count = connection.execute(
                "SELECT count(*) FROM pg_class WHERE relname IN "
                "('memory_records','memory_events','model_invocations','context_artifacts')"
            ).fetchone()
        assert revision is not None and revision[0] == "0009_model_accounting_provenance"
        assert table_count is not None and table_count[0] == 4
        print(
            "PASS phase5 fresh migration: upgrade -> full downgrade -> upgrade, head=0009, tables=4"
        )
    finally:
        with psycopg.connect(admin_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database_name))
            )


if __name__ == "__main__":
    main()
