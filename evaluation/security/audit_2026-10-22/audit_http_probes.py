import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

import httpx
from fastapi import FastAPI

from servicemind import api
from servicemind.interfaces.http import operations
from servicemind.security.auth import TenantContext, get_tenant_context

TENANT = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
RUN = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


class Repo:
    def __init__(self, tenant_id):
        pass

    async def get_run(self, run_id):
        return SimpleNamespace(
            id=run_id,
            tenant_id=TENANT,
            user_id="another-user",
            ticket_id=42,
            goal="audit fixture",
            request_write=False,
            status="succeeded",
            result={"summary": "AUDIT_CANARY"},
            error=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    async def get_action_intent(self, run_id):
        return None

    async def list_events(self, run_id, after):
        return [
            SimpleNamespace(
                sequence=1,
                event_type="audit.fixture",
                payload={"canary": "AUDIT_CANARY"},
                created_at=datetime.now(UTC),
            )
        ]

    async def get_glpi_integration(self):
        return SimpleNamespace(entity_id=1, webhook_secret_encrypted="fake-encrypted")


async def main():
    app = FastAPI()
    app.include_router(api.phase2_router)
    app.dependency_overrides[get_tenant_context] = lambda: TenantContext(
        tenant_id=TENANT, user_id="no-role", username="no-role", roles=set()
    )
    results = {}
    with (
        patch.object(api, "ServiceMindRepository", Repo),
        patch.object(operations, "ServiceMindRepository", Repo),
        patch.object(
            api, "CredentialCipher", lambda: SimpleNamespace(decrypt=lambda value: "audit-only")
        ),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://audit.invalid",
        ) as client:
            for suffix in ["", "/events", "/timeline"]:
                response = await client.get(f"/v1/servicemind/runs/{RUN}{suffix}")
                results["no_role" + (suffix or "/detail")] = {
                    "status": response.status_code,
                    "contains_fixture_canary": "AUDIT_CANARY" in response.text,
                }
            response = await client.post(
                "/v1/servicemind/webhooks/glpi",
                headers={"X-GLPI-timestamp": "9" * 5000, "X-GLPI-signature": "0" * 64},
                json={
                    "tenant_id": str(TENANT),
                    "event": "new",
                    "item": {"id": 5, "entity": {"id": 1}},
                },
            )
            results["webhook_5000_digit_timestamp"] = {"status": response.status_code}
    Path("audit-http-probes.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))


asyncio.run(main())
