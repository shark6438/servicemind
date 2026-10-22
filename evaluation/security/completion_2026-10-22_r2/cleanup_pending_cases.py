"""Cancel only the exact synthetic Runs created by this batch's live samplers."""

import asyncio
import json
import sys
from pathlib import Path
from uuid import UUID

from servicemind.persistence.database import close_database
from servicemind.persistence.repository import ServiceMindRepository

BATCH = Path(__file__).resolve().parent
sys.path.insert(0, str(BATCH.parents[2] / "scripts"))
from verify_phase7_acceptance_live import SUBJECT_PASSWORD_VARS, Stack  # noqa: E402


async def main():
    stack = Stack(
        base_url="http://127.0.0.1:18080",
        tenant_id=UUID("22222222-2222-4222-8222-222222222222"),
        timeout_scale=1,
    )
    owners = {}
    for subject in SUBJECT_PASSWORD_VARS:
        context = await stack.context(subject)
        owners[context.user_id] = subject
    ids = set()
    for path in [
        *BATCH.glob("*/ACC-*.json"),
        *(BATCH.parent / "completion_2026-10-22").glob("*/ACC-*.json"),
    ]:
        replay = json.loads(path.read_text())
        ids.update(replay.get("case_run_ids", []))
        if replay.get("run_id"):
            ids.add(replay["run_id"])
    rows = []
    for tenant in [
        UUID("22222222-2222-4222-8222-222222222222"),
        UUID("11111111-1111-4111-8111-111111111111"),
    ]:
        repository = ServiceMindRepository(tenant)
        for run_id in sorted(ids):
            run = await repository.get_run(UUID(run_id))
            if run is None or run.status not in {
                "pending",
                "running",
                "waiting_approval",
                "waiting_review",
            }:
                continue
            subject = owners.get(run.user_id)
            assert subject, f"Synthetic run owner not mapped: {run_id}"
            response = await stack.request(
                subject, "POST", f"{stack.base_url}/v1/servicemind/runs/{run_id}:cancel"
            )
            rows.append(
                {
                    "run_id": run_id,
                    "prior_status": run.status,
                    "http_status": response.status_code,
                    "final_status": response.json().get("status"),
                }
            )
            assert response.status_code == 200 and response.json()["status"] == "cancelled", run_id
    (BATCH / "pending-case-cleanup.json").write_text(
        json.dumps(
            {
                "verdict": "PASS",
                "exact_fixture_ids_examined": len(ids),
                "cancelled": rows,
                "scope": "Only IDs in this batch's ACC replays; immutable ledgers and terminal results retained.",
            },
            indent=2,
        )
        + "\n"
    )
    print(
        json.dumps(
            {"verdict": "PASS", "exact_fixture_ids_examined": len(ids), "cancelled": len(rows)}
        )
    )
    await stack.aclose()
    await close_database()


asyncio.run(main())
