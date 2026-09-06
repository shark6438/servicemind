import asyncio
import sys
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert

from core import settings
from servicemind.persistence.database import close_database, global_session, tenant_session
from servicemind.persistence.models import GlpiIntegration, Tenant
from servicemind.security.crypto import CredentialCipher

ACME_TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
GLOBEX_TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")


async def seed_tenant(
    tenant_id: UUID,
    slug: str,
    name: str,
    entity_id: int,
    username: str | None,
    password: str | None,
    webhook_secret: str | None,
) -> None:
    if not username or not password or not webhook_secret:
        raise RuntimeError(f"Missing GLPI credentials for tenant {slug}")
    if not settings.GLPI_BASE_URL or not settings.GLPI_CLIENT_ID or not settings.GLPI_CLIENT_SECRET:
        raise RuntimeError("Base GLPI settings are incomplete")

    cipher = CredentialCipher()
    async with global_session() as session:
        await session.execute(
            insert(Tenant)
            .values(id=tenant_id, slug=slug, name=name)
            .on_conflict_do_update(index_elements=["id"], set_={"slug": slug, "name": name})
        )

    values = {
        "tenant_id": tenant_id,
        "base_url": settings.GLPI_BASE_URL,
        "api_version": settings.GLPI_API_VERSION,
        "client_id_encrypted": cipher.encrypt(settings.GLPI_CLIENT_ID.get_secret_value()),
        "client_secret_encrypted": cipher.encrypt(settings.GLPI_CLIENT_SECRET.get_secret_value()),
        "username_encrypted": cipher.encrypt(username),
        "password_encrypted": cipher.encrypt(password),
        "webhook_secret_encrypted": cipher.encrypt(webhook_secret),
        "entity_id": entity_id,
        "profile_id": 6,
        "enabled": True,
    }
    async with tenant_session(tenant_id) as session:
        await session.execute(
            insert(GlpiIntegration)
            .values(**values)
            .on_conflict_do_update(index_elements=["tenant_id"], set_=values)
        )
    print(f"Seeded tenant {slug} with GLPI entity {entity_id}")


async def main() -> None:
    await seed_tenant(
        ACME_TENANT_ID,
        "acme",
        "Acme China",
        1,
        settings.SERVICEMIND_ACME_GLPI_USERNAME,
        settings.SERVICEMIND_ACME_GLPI_PASSWORD.get_secret_value()
        if settings.SERVICEMIND_ACME_GLPI_PASSWORD
        else None,
        settings.SERVICEMIND_ACME_WEBHOOK_SECRET.get_secret_value()
        if settings.SERVICEMIND_ACME_WEBHOOK_SECRET
        else None,
    )
    await seed_tenant(
        GLOBEX_TENANT_ID,
        "globex",
        "Globex China",
        2,
        settings.SERVICEMIND_GLOBEX_GLPI_USERNAME,
        settings.SERVICEMIND_GLOBEX_GLPI_PASSWORD.get_secret_value()
        if settings.SERVICEMIND_GLOBEX_GLPI_PASSWORD
        else None,
        settings.SERVICEMIND_GLOBEX_WEBHOOK_SECRET.get_secret_value()
        if settings.SERVICEMIND_GLOBEX_WEBHOOK_SECRET
        else None,
    )
    await close_database()


if __name__ == "__main__":
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(main(), loop_factory=loop_factory)
