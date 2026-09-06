from servicemind.integrations.glpi.client import GlpiClientConfig
from servicemind.persistence.repository import ServiceMindRepository
from servicemind.security.auth import TenantContext
from servicemind.security.crypto import CredentialCipher


async def resolve_glpi_config(context: TenantContext) -> GlpiClientConfig:
    integration = await ServiceMindRepository(context.tenant_id).get_glpi_integration()
    if integration is None:
        raise RuntimeError("No enabled GLPI integration exists for this tenant")
    if integration.entity_id not in context.allowed_glpi_entity_ids:
        raise PermissionError("GLPI entity is outside the caller's tenant scope")

    cipher = CredentialCipher()
    return GlpiClientConfig(
        base_url=integration.base_url,
        api_version=integration.api_version,
        client_id=cipher.decrypt(integration.client_id_encrypted),
        client_secret=cipher.decrypt(integration.client_secret_encrypted),
        username=cipher.decrypt(integration.username_encrypted),
        password=cipher.decrypt(integration.password_encrypted),
        entity_id=integration.entity_id,
        profile_id=integration.profile_id,
        timeout_seconds=15,
        trust_env=False,
    )
