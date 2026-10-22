import asyncio
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from servicemind.persistence.database import close_database, tenant_session
from servicemind.persistence.models import AgentRun
from servicemind.persistence.repository import ServiceMindRepository

BATCH = Path(__file__).resolve().parent
TENANT = UUID("22222222-2222-4222-8222-222222222222")


async def main():
    fixture = json.loads((BATCH / "native-webhook-fixture.json").read_text())
    started = datetime.now(UTC)
    (BATCH / "webhook-live-start.json").write_text(json.dumps({"started_at": started.isoformat()}))
    command = [
        "docker",
        "exec",
        "-i",
        "-e",
        f"SECURITY_QUEUE_ID={fixture['queue_id']}",
        "-e",
        f"SECURITY_TICKET_ID={fixture['ticket_id']}",
        "servicemind-glpi-glpi-1",
        "php",
    ]
    result = await asyncio.to_thread(
        subprocess.run,
        command,
        input=(BATCH / "native_webhook_delivery.php").read_text(),
        text=True,
        capture_output=True,
        check=True,
    )
    (BATCH / "native-webhook-sender.stdout").write_text(result.stdout)
    (BATCH / "native-webhook-sender.stderr").write_text(result.stderr)
    observed = json.loads(result.stdout)
    frames = {frame["case"]: frame for frame in observed["frames"]}
    assert observed["cra_enabled"]
    assert (
        not frames["wrong-tenant-cra"]["sender_return"]
        and frames["wrong-tenant-cra"]["http_status"] != 202
    )
    for case in ["tampered-body", "expired-signed-frame", "wrong-entity-valid-hmac"]:
        assert frames[case]["http_status"] == 401, frames[case]
    for case in ["native-valid-delivery", "same-signature-retry", "resigned-delivery"]:
        assert frames[case]["http_status"] == 202, frames[case]
    first = frames["native-valid-delivery"]["expected_run_id"]
    second = frames["resigned-delivery"]["expected_run_id"]
    assert frames["same-signature-retry"]["expected_run_id"] == first and first != second
    repository = ServiceMindRepository(TENANT)
    final = {}
    for run_id in [first, second]:
        for _ in range(90):
            run = await repository.get_run(UUID(run_id))
            if run and run.status in {
                "succeeded",
                "failed",
                "cancelled",
                "waiting_review",
                "waiting_approval",
            }:
                break
            await asyncio.sleep(1)
        assert run is not None and run.status == "succeeded", (
            None if run is None else (run.status, run.error)
        )
        events = await repository.list_events(run.id)
        assert sum(event.event_type == "webhook.accepted" for event in events) == 1
        final[run_id] = {
            "status": run.status,
            "ticket_id": run.ticket_id,
            "request_write": run.request_write,
            "event_types": [event.event_type for event in events],
        }
        assert run.ticket_id == fixture["ticket_id"] and not run.request_write
    for tenant in [TENANT, UUID("11111111-1111-4111-8111-111111111111")]:
        async with tenant_session(tenant) as session:
            new = list(
                (
                    await session.scalars(
                        select(AgentRun.id).where(
                            AgentRun.user_id == "glpi-webhook", AgentRun.created_at >= started
                        )
                    )
                ).all()
            )
        assert set(new) == ({UUID(first), UUID(second)} if tenant == TENANT else set()), new
    observed.update(
        {
            "started_at": started.isoformat(),
            "terminal_runs": final,
            "interpretation": "Same signature gives one Run; re-signing creates a distinct Run. HTTP 202 alone is not completion.",
            "verdict": "PASS",
        }
    )
    (BATCH / "webhook-live.json").write_text(json.dumps(observed, indent=2) + "\n")
    print(json.dumps({"verdict": "PASS", "terminal_runs": final}))
    await close_database()


asyncio.run(main())
