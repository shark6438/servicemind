from uuid import UUID

from sqlalchemy import select

from servicemind.persistence.database import tenant_session
from servicemind.persistence.models import GlpiIntegration, McpTaskRecord
from servicemind.security.crypto import CredentialCipher

CREDENTIAL_FIELDS = (
    "client_id_encrypted",
    "client_secret_encrypted",
    "username_encrypted",
    "password_encrypted",
    "webhook_secret_encrypted",
)


async def rotate_tenant_credentials(tenant_id: UUID, *, dry_run: bool = True) -> int:
    cipher = CredentialCipher()
    count = 0
    async with tenant_session(tenant_id) as session:
        for model, fields in (
            (GlpiIntegration, CREDENTIAL_FIELDS),
            (McpTaskRecord, ("output_ciphertext",)),
        ):
            records = await session.scalars(
                select(model).where(model.tenant_id == tenant_id).with_for_update()
            )
            for record in records:
                for field in fields:
                    value = getattr(record, field)
                    if value is None:
                        continue
                    rotated = cipher.rotate(value)
                    if not dry_run:
                        setattr(record, field, rotated)
                    count += 1
    return count
