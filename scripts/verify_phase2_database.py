"""Fail-fast database controls verification for the local Phase 2 stack."""

import asyncio
import sys
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from servicemind.persistence.database import close_database, global_session, tenant_session
from servicemind.persistence.models import AuditEvent, GlpiIntegration

ACME = UUID("11111111-1111-4111-8111-111111111111")
GLOBEX = UUID("22222222-2222-4222-8222-222222222222")


async def visible_entities(tenant_id: UUID) -> list[int]:
    async with tenant_session(tenant_id) as session:
        return list((await session.execute(select(GlpiIntegration.entity_id))).scalars())


async def verify_append_only_audit() -> None:
    async with tenant_session(ACME) as session:
        audit_id = (
            await session.execute(select(AuditEvent.id).order_by(AuditEvent.created_at).limit(1))
        ).scalar_one_or_none()
    if audit_id is None:
        raise AssertionError("No Acme audit record exists; run the API verification first")
    try:
        async with tenant_session(ACME) as session:
            await session.execute(
                text("UPDATE audit_events SET event_type = 'tampered' WHERE id = :id"),
                {"id": audit_id},
            )
    except DBAPIError as exc:
        if "append-only" not in str(exc.orig):
            raise
    else:
        raise AssertionError("audit_events UPDATE unexpectedly succeeded")


async def main() -> None:
    assert await visible_entities(ACME) == [1]
    assert await visible_entities(GLOBEX) == [2]
    async with global_session() as session:
        without_tenant = (await session.execute(select(GlpiIntegration.entity_id))).scalars().all()
        revision = (
            await session.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()
    assert without_tenant == []
    assert revision == "0003_append_only_audit"
    await verify_append_only_audit()
    print("PASS database: RLS partitions, zero-context denial, migration head, append-only audit")
    await close_database()


if __name__ == "__main__":
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(main(), loop_factory=loop_factory)
