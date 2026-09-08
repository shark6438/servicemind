"""Exercise the real configured model through the Phase 5 governed gateway."""

from __future__ import annotations

import asyncio
import sys
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from core import settings
from core.llm import get_model
from servicemind.model_gateway.contracts import ModelCallContext, ModelPurpose, ModelRisk
from servicemind.model_gateway.gateway import ModelGateway
from servicemind.model_gateway.repository import PostgresModelAuditSink
from servicemind.persistence.database import close_database, tenant_session
from servicemind.persistence.models import ModelInvocationRecord
from servicemind.persistence.repository import ServiceMindRepository

ACME = UUID("11111111-1111-4111-8111-111111111111")


class LiveGatewayResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: str = Field(pattern="^ok$")
    value: int = Field(ge=1, le=1)


async def verify() -> None:
    if settings.DEFAULT_MODEL is None:
        raise RuntimeError("DEFAULT_MODEL is not configured")
    run = await ServiceMindRepository(ACME).create_run(
        user_id="phase5-model-verifier",
        ticket_id=1,
        goal="Verify the governed model gateway",
        request_write=False,
    )
    context = ModelCallContext(
        tenant_id=ACME,
        run_id=run.id,
        task_id="gateway-live",
        agent_role="analysis",
        purpose=ModelPurpose.ANALYSIS,
        risk=ModelRisk.LOW,
        policy_version="phase5-live-v1",
        prompt_version="phase5-live-v1",
        timeout_seconds=60,
        max_cost_usd=0.01,
    )
    gateway = ModelGateway(audit_sink=PostgresModelAuditSink(), max_retries=1)
    result = await gateway.invoke(
        get_model(settings.DEFAULT_MODEL),
        LiveGatewayResult,
        [
            {
                "role": "user",
                "content": "Return JSON with status exactly ok and value exactly 1.",
            }
        ],
        context=context,
    )
    assert result == LiveGatewayResult(status="ok", value=1)
    async with tenant_session(ACME) as session:
        audit = (
            await session.execute(
                select(ModelInvocationRecord)
                .where(
                    ModelInvocationRecord.run_id == run.id,
                    ModelInvocationRecord.task_id == "gateway-live",
                )
                .order_by(ModelInvocationRecord.created_at.desc())
                .limit(1)
            )
        ).scalar_one()
    assert audit.status == "succeeded"
    assert audit.provider == "deepseek"
    assert audit.input_tokens > 0 and audit.output_tokens > 0
    assert audit.token_accounting_source == "provider"
    assert audit.pricing_version == "deepseek-2026-08-16-peak-cache-miss"
    assert audit.prompt_hash and audit.schema_hash
    print(
        "PASS phase5 live model gateway: structured result, tenant route, provider token "
        "accounting, cost provenance, and durable terminal audit"
    )


async def main() -> None:
    try:
        await verify()
    finally:
        await close_database()


if __name__ == "__main__":
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(main(), loop_factory=loop_factory)
