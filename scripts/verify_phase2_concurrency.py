"""Live concurrent-approval/side-effect verification for the local Phase 2 stack."""

import asyncio
import sys
from uuid import UUID

import httpx
from dotenv import dotenv_values

from servicemind.integrations.glpi.client import GlpiClient
from servicemind.integrations.glpi.models import html_to_text
from servicemind.integrations.glpi.resolver import resolve_glpi_config
from servicemind.security.auth import TenantContext

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")


async def main() -> None:
    local_env = dotenv_values("deploy/glpi/.env")
    async with httpx.AsyncClient(timeout=120, trust_env=False) as client:

        async def token(username: str, password_key: str) -> str:
            response = await client.post(
                "http://127.0.0.1:8090/realms/servicemind/protocol/openid-connect/token",
                data={
                    "grant_type": "password",
                    "client_id": "servicemind-api",
                    "username": username,
                    "password": local_env[password_key],
                },
            )
            response.raise_for_status()
            return str(response.json()["access_token"])

        analyst, approver = await asyncio.gather(
            token("acme-analyst", "ACME_ANALYST_PASSWORD"),
            token("acme-approver", "ACME_APPROVER_PASSWORD"),
        )
        analyst_headers = {"Authorization": f"Bearer {analyst}"}
        approver_headers = {"Authorization": f"Bearer {approver}"}
        response = await client.post(
            "http://127.0.0.1:8080/v1/servicemind/runs",
            headers=analyst_headers,
            json={
                "ticket_id": 2,
                "goal": "Concurrent approval exactly-once verification",
                "request_write": True,
            },
        )
        response.raise_for_status()
        run = response.json()
        assert run["status"] == "waiting_approval"
        action_hash = run["action_intent"]["action_hash"]
        approval_url = f"http://127.0.0.1:8080/v1/servicemind/runs/{run['id']}/approval"
        approval_body = {
            "decision": "approved",
            "expected_action_hash": action_hash,
            "comment": "Concurrent Phase 2 verification",
        }
        responses = await asyncio.gather(
            *(
                client.post(approval_url, headers=approver_headers, json=approval_body)
                for _ in range(2)
            )
        )
        assert all(item.status_code == 200 for item in responses)
        for _ in range(60):
            current = (
                await client.get(
                    f"http://127.0.0.1:8080/v1/servicemind/runs/{run['id']}",
                    headers=analyst_headers,
                )
            ).json()
            if current["status"] in {"succeeded", "failed"}:
                break
            await asyncio.sleep(0.5)
        assert current["status"] == "succeeded", current

    context = TenantContext(
        tenant_id=TENANT_ID,
        user_id="concurrency-verifier",
        username="concurrency-verifier",
        roles={"viewer", "analyst"},
        allowed_glpi_entity_ids={1},
    )
    marker = f"[ServiceMind run={run['id']} action={action_hash[:16]}]"
    async with GlpiClient(await resolve_glpi_config(context)) as glpi:
        matches = [
            followup
            for followup in await glpi.list_ticket_followups(2)
            if marker in html_to_text(followup.content)
        ]
    assert len(matches) == 1
    print(
        "PASS concurrent approval exactly-once: "
        f"run={run['id']} followup={matches[0].id} copies={len(matches)}"
    )


if __name__ == "__main__":
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(main(), loop_factory=factory)
