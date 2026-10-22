import asyncio
import hashlib
import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
from fastapi import FastAPI

from servicemind import api
from servicemind.evaluation.source_revision import source_revision
from servicemind.persistence.database import close_database
from servicemind.persistence.models import RunStatus
from servicemind.persistence.repository import ServiceMindRepository
from servicemind.security.auth import TenantContext, get_tenant_context

BATCH = Path(__file__).resolve().parent
TENANT = UUID("11111111-1111-4111-8111-111111111111")
OTHER = UUID("22222222-2222-4222-8222-222222222222")


async def main():
    inputs = [
        {
            "user_id": "holdout-" + secrets.token_hex(8),
            "entity_ids": [1] if i % 4 in {0, 1} else [2],
            "group_ids": [3] if i % 4 in {0, 2} else [4],
            "tenant_id": str(OTHER if i % 8 == 7 else TENANT),
        }
        for i in range(16)
    ]
    encoded = json.dumps(inputs, sort_keys=True).encode()
    (BATCH / "holdout-inputs.json").write_bytes(encoded)
    nonce = "SYNTHETIC_SCOPE_CANARY_" + secrets.token_hex(16)
    owner = "holdout-owner-" + secrets.token_hex(6)
    repo = ServiceMindRepository(TENANT)
    run = await repo.create_run(
        user_id=owner,
        ticket_id=1,
        goal="Synthetic held-out scope assessment",
        request_write=False,
        access_scope={"entity_ids": [1], "group_ids": [3]},
    )
    await repo.update_run(run.id, RunStatus.SUCCEEDED, result={"canary": nonce})
    app = FastAPI()
    app.include_router(api.phase2_router)

    def context(user_id=owner, tenant_id=TENANT, entities=(1,), groups=(3,)):
        return TenantContext(
            tenant_id=tenant_id,
            user_id=user_id,
            username=user_id,
            roles={"viewer", "analyst"},
            allowed_glpi_entity_ids=set(entities),
            allowed_glpi_group_ids=set(groups),
        )

    async def request(principal):
        app.dependency_overrides[get_tenant_context] = lambda: principal
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            response = await client.get(f"/v1/servicemind/runs/{run.id}")
            return {"status": response.status_code, "canary_observed": nonce in response.text}

    positive_before = await request(context())
    assert positive_before == {"status": 200, "canary_observed": True}

    async def sweep():
        return [
            await request(
                context(
                    user_id=row["user_id"],
                    tenant_id=UUID(row["tenant_id"]),
                    entities=row["entity_ids"],
                    groups=row["group_ids"],
                )
            )
            for row in inputs
        ]

    baseline = await sweep()
    assert all(row == {"status": 404, "canary_observed": False} for row in baseline)
    control = api.require_run_access
    try:
        api.require_run_access = lambda *args: None
        counterfactual = await sweep()
        assert any(row["canary_observed"] for row in counterfactual)
    finally:
        api.require_run_access = control
    restored = await sweep()
    positive_after = await request(context())
    assert all(row == {"status": 404, "canary_observed": False} for row in restored)
    assert positive_before == positive_after
    result = {
        "source_revision": source_revision(Path.cwd()),
        "observed_at": datetime.now(UTC).isoformat(),
        "inputs_sha256": hashlib.sha256(encoded).hexdigest(),
        "generated_after_source_freeze": True,
        "samples": len(inputs),
        "baseline": baseline,
        "counterfactual_control_removed_only_in_isolated_process": counterfactual,
        "restored": restored,
        "positive_before": positive_before,
        "positive_after": positive_after,
        "canary_sha256": hashlib.sha256(nonce.encode()).hexdigest(),
        "synthetic_run_id": str(run.id),
        "interpretation": "Held-out authorization probes; these are not real-model ASR samples.",
        "verdict": "PASS",
    }
    (BATCH / "holdout-observation.json").write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                "verdict": "PASS",
                "samples": len(inputs),
                "counterfactual_leaks": sum(r["canary_observed"] for r in counterfactual),
                "restored_leaks": sum(r["canary_observed"] for r in restored),
            }
        )
    )
    await close_database()


asyncio.run(main())
