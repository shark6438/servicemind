"""Run a real tenant-scoped knowledge workflow with Phase 5 controls enabled."""

from __future__ import annotations

import asyncio
import json
import selectors
from uuid import UUID

from sqlalchemy import func, select

from core import settings
from servicemind.agents.knowledge import knowledge_agent
from servicemind.orchestration.runtime import start_run
from servicemind.persistence.database import close_database, tenant_session
from servicemind.persistence.models import ContextArtifactRecord, ModelInvocationRecord
from servicemind.persistence.repository import ServiceMindRepository
from servicemind.security.auth import TenantContext

ACME = UUID("11111111-1111-4111-8111-111111111111")


async def verify() -> None:
    settings.SERVICEMIND_CONTEXT_ENABLED = True
    settings.SERVICEMIND_MEMORY_ENABLED = True
    settings.SERVICEMIND_SKILLS_ENABLED = True
    settings.SERVICEMIND_MODEL_GATEWAY_AUDIT_ENABLED = True
    settings.SERVICEMIND_SEMANTIC_CACHE_ENABLED = False
    settings.SERVICEMIND_MODEL_ALLOWED_PROVIDERS = "deepseek"
    settings.SERVICEMIND_MODEL_ALLOWED_MODELS = "deepseek-v4-flash"
    settings.SERVICEMIND_TENANT_MODEL_ALLOWLIST_JSON = json.dumps(
        {
            str(ACME): {
                "providers": ["deepseek"],
                "models": ["deepseek-v4-flash"],
            }
        }
    )
    run = await ServiceMindRepository(ACME).create_run(
        user_id="phase5-workflow-verifier",
        ticket_id=1,
        goal="How to troubleshoot a VPN MFA authentication failure using the runbook?",
        request_write=False,
    )
    state = await start_run(
        run,
        TenantContext(
            tenant_id=ACME,
            user_id="phase5-workflow-verifier",
            username="phase5-workflow-verifier",
            roles={"analyst"},
            allowed_glpi_entity_ids={1},
        ),
    )
    result = state.get("final_result") or {}
    evidence = result.get("evidence") or []
    assert evidence, "live workflow returned no RAG evidence"
    assert result.get("trajectory") == ["router", "knowledge"]
    async with tenant_session(ACME) as session:
        context_count = (
            await session.execute(
                select(func.count())
                .select_from(ContextArtifactRecord)
                .where(ContextArtifactRecord.run_id == run.id)
            )
        ).scalar_one()
        invocation_count = (
            await session.execute(
                select(func.count())
                .select_from(ModelInvocationRecord)
                .where(
                    ModelInvocationRecord.run_id == run.id,
                    ModelInvocationRecord.status == "succeeded",
                )
            )
        ).scalar_one()
    assert context_count == 1
    assert invocation_count >= 1
    print(
        "PASS phase5 live workflow: router -> governed Knowledge context -> RAG -> "
        "model gateway -> durable tenant audit"
    )


async def main() -> None:
    try:
        await verify()
    finally:
        if knowledge_agent._rag is not None:
            await knowledge_agent._rag.index.client.close()
        await close_database()


if __name__ == "__main__":
    asyncio.run(
        main(),
        loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
    )
