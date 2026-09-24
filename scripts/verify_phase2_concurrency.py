"""Live concurrent-approval/side-effect verification for the local Phase 2 stack.

Two approvals for one run are posted at the same time. The platform's claim is not that
one of them loses -- it is that *one* followup is written, however the race resolves.
That is what this script measures, by counting the followups carrying this run's marker
rather than by counting HTTP statuses.

It had drifted away from the stack it verifies: it posted to a fixed ``127.0.0.1:8080``
(not this API's port -- 8080 is another service on the host, so the request left the
platform entirely) and read ``action_intent.action_hash`` from the create response, which
is written by the workflow *after* the create returns and is therefore ``None`` at that
point. Both are derived from the running configuration now: the base URL from
``core.settings`` unless overridden, and the action hash from a run that has actually
reached ``waiting_approval``.
"""

import argparse
import asyncio
import sys
from uuid import UUID

import httpx
from dotenv import dotenv_values

from core import settings
from servicemind.integrations.glpi.client import GlpiClient
from servicemind.integrations.glpi.models import html_to_text
from servicemind.integrations.glpi.resolver import resolve_glpi_config
from servicemind.security.auth import TenantContext

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")

#: Statuses a concurrent approval may legally answer with. The loser of the race is not an
#: error: a run can only be approved once, and a second identical request arriving after
#: the first has been applied is either the same decision (idempotent, 200) or a conflict
#: against the state the first one produced (409). Which of the two happens depends on
#: whether the second request reads the run before or after the first commits, so the
#: script records what it saw instead of pretending the interleaving is fixed.
ACCEPTED_APPROVAL_STATUSES = frozenset({200, 409})

#: The run's own statuses that mean it has stopped moving.
SETTLED = frozenset({"succeeded", "failed", "cancelled"})

#: The run has to leave ``pending`` and produce its intent before there is a hash to
#: approve; the approval endpoint answers 409 until then.
AWAITING_APPROVAL = "waiting_approval"


async def wait_for_status(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    run_id: str,
    headers: dict[str, str],
    wanted: frozenset[str],
    deadline: float,
) -> dict:
    """Poll one run until its status is one of ``wanted``, and return the last body seen."""
    payload: dict = {}
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(f"{base_url}/v1/servicemind/runs/{run_id}", headers=headers)
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") in wanted:
            return payload
        await asyncio.sleep(0.5)
    return payload


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=f"http://{settings.HOST}:{settings.PORT}",
        help="ServiceMind API root (default: the configured HOST/PORT)",
    )
    parser.add_argument("--ticket-id", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")
    loop = asyncio.get_running_loop()

    local_env = dotenv_values("deploy/glpi/.env")
    async with httpx.AsyncClient(timeout=args.timeout, trust_env=False) as client:

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
            f"{base_url}/v1/servicemind/runs",
            headers=analyst_headers,
            json={
                "ticket_id": args.ticket_id,
                "goal": "Concurrent approval exactly-once verification",
                "request_write": True,
            },
        )
        response.raise_for_status()
        created = response.json()
        run_id = created["id"]
        # Creation returns as soon as the run row exists; the intent is derived by the
        # workflow afterwards. Reading the hash off this response reads a field that has
        # not been written yet.
        awaiting = await wait_for_status(
            client,
            base_url=base_url,
            run_id=run_id,
            headers=analyst_headers,
            wanted=frozenset({AWAITING_APPROVAL}),
            deadline=loop.time() + args.timeout,
        )
        assert awaiting["status"] == AWAITING_APPROVAL, awaiting
        action_hash = awaiting["action_intent"]["action_hash"]
        assert action_hash, awaiting["action_intent"]

        approval_url = f"{base_url}/v1/servicemind/runs/{run_id}/approval"
        approval_body = {
            "decision": "approved",
            "expected_action_hash": action_hash,
            "comment": "Concurrent Phase 2 verification",
        }
        racing = await asyncio.gather(
            *(
                client.post(approval_url, headers=approver_headers, json=approval_body)
                for _ in range(2)
            )
        )
        statuses = [item.status_code for item in racing]
        assert all(item in ACCEPTED_APPROVAL_STATUSES for item in statuses), statuses
        assert 200 in statuses, statuses

        settled = await wait_for_status(
            client,
            base_url=base_url,
            run_id=run_id,
            headers=analyst_headers,
            wanted=SETTLED,
            deadline=loop.time() + args.timeout,
        )
        assert settled["status"] == "succeeded", settled

        # A third approval, after the write has landed, must not write a second followup.
        replay = await client.post(approval_url, headers=approver_headers, json=approval_body)
        assert replay.status_code in ACCEPTED_APPROVAL_STATUSES, replay.status_code

    context = TenantContext(
        tenant_id=TENANT_ID,
        user_id="concurrency-verifier",
        username="concurrency-verifier",
        roles={"viewer", "analyst"},
        allowed_glpi_entity_ids={1},
    )
    marker = f"[ServiceMind run={run_id} action={action_hash[:16]}]"
    async with GlpiClient(await resolve_glpi_config(context)) as glpi:
        matches = [
            followup
            for followup in await glpi.list_ticket_followups(args.ticket_id)
            if marker in html_to_text(followup.content)
        ]
    assert len(matches) == 1, [followup.id for followup in matches]
    print(
        "PASS concurrent approval exactly-once: "
        f"run={run_id} ticket={args.ticket_id} followup={matches[0].id} "
        f"copies={len(matches)} approval_statuses={statuses} replay={replay.status_code}"
    )


if __name__ == "__main__":
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(main(), loop_factory=factory)
