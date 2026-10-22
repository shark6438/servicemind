"""One current-source open-model control, guarded by the actual remote build identity."""

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from servicemind.evaluation.acceptance import case_set_digest, load_acceptance_cases
from servicemind.evaluation.acceptance_grader import grade
from servicemind.evaluation.source_revision import source_revision
from servicemind.persistence.database import close_database

BATCH = Path(__file__).resolve().parent
ROOT = BATCH.parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from verify_phase7_acceptance_live import Stack, environment_of, run_case  # noqa: E402


async def main():
    stack = Stack(
        base_url="http://127.0.0.1:18083",
        tenant_id=UUID("22222222-2222-4222-8222-222222222222"),
        timeout_scale=1,
    )
    await stack.await_health()
    before = (await stack.client.get(stack.base_url + "/health/build")).json()
    assert before["source_revision"] == source_revision(ROOT)
    assert before["configuration"]["default_model"] == "ollama"
    stack.build_identity = before
    environment = environment_of(stack, source_revision(ROOT))
    environment.notes.append(
        "This single-case probe guards the actual 18083 serving boot/source/config before and after; unrelated intentional restarts of the main 18080 unit do not describe this server."
    )
    cases = load_acceptance_cases(ROOT / "evaluation/acceptance/cases.v1.json")
    tickets = json.loads((ROOT / "evaluation/acceptance/fixtures/globex/tickets.json").read_text())[
        "tickets"
    ]
    execution = await run_case(
        next(c for c in cases.cases if c.id == "ACC-01"),
        stack,
        tickets,
        environment,
        case_set_digest(cases),
        BATCH / "current-local-model-control",
    )
    after = (await stack.client.get(stack.base_url + "/health/build")).json()
    assert before == after and source_revision(ROOT) == before["source_revision"]
    result = next(
        c
        for c in grade(cases, [execution], generated_at=datetime.now(UTC)).cases
        if c.case_id == "ACC-01"
    )
    summary = {
        "source_revision": before["source_revision"],
        "model": "qwen3:30b-a3b",
        "runtime_build_identity": before,
        "remote_identity_unchanged": True,
        "api_fee": 0,
        "utility_verdict": result.verdict.value,
        "terminal_status": execution.terminal_status.value if execution.terminal_status else None,
        "seconds": execution.total_seconds,
        "interpretation": "Single current-source normal control; not a full 28-case acceptance or a robustness rate.",
    }
    (BATCH / "current-local-model-control.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))
    await stack.aclose()
    await close_database()


asyncio.run(main())
