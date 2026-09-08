from __future__ import annotations

from typing import Protocol

from servicemind.model_gateway.contracts import ModelInvocationAudit
from servicemind.persistence.database import tenant_session
from servicemind.persistence.models import ModelInvocationRecord


class ModelAuditSink(Protocol):
    async def record(self, audit: ModelInvocationAudit) -> None: ...


class InMemoryModelAuditSink:
    def __init__(self) -> None:
        self.records: list[ModelInvocationAudit] = []

    async def record(self, audit: ModelInvocationAudit) -> None:
        self.records.append(audit)


class NullModelAuditSink:
    async def record(self, audit: ModelInvocationAudit) -> None:
        del audit


class ConfiguredModelAuditSink:
    """Persist scoped production calls when the explicit governance flag is enabled."""

    async def record(self, audit: ModelInvocationAudit) -> None:
        from core import settings

        if (
            settings.SERVICEMIND_MODEL_GATEWAY_AUDIT_ENABLED
            and audit.context.tenant_id.int != 0
            and settings.SERVICEMIND_DATABASE_URL is not None
        ):
            await PostgresModelAuditSink().record(audit)


class PostgresModelAuditSink:
    async def record(self, audit: ModelInvocationAudit) -> None:
        async with tenant_session(audit.context.tenant_id) as session:
            session.add(
                ModelInvocationRecord(
                    tenant_id=audit.context.tenant_id,
                    request_id=audit.request_id,
                    run_id=audit.context.run_id,
                    task_id=audit.context.task_id,
                    agent_role=audit.context.agent_role,
                    purpose=audit.context.purpose.value,
                    provider=audit.route.provider,
                    model=audit.route.model,
                    model_revision=audit.route.model_revision,
                    route_reason=audit.route.reason,
                    prompt_version=audit.context.prompt_version,
                    prompt_hash=audit.prompt_hash,
                    schema_hash=audit.schema_hash,
                    input_tokens=audit.input_tokens,
                    output_tokens=audit.output_tokens,
                    latency_ms=audit.latency_ms,
                    cost_usd=audit.cost_usd,
                    token_accounting_source=audit.token_accounting_source,
                    pricing_version=audit.pricing_version,
                    cost_estimate=audit.cost_estimate,
                    attempts=audit.attempts,
                    retries=audit.retries,
                    fallback_from=audit.fallback_from,
                    status=audit.status,
                    error_code=audit.error_code,
                )
            )
            await session.flush()
