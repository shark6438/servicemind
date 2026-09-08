from __future__ import annotations

from typing import Protocol

from servicemind.context.contracts import ContextEnvelope
from servicemind.persistence.database import tenant_session
from servicemind.persistence.models import ContextArtifactRecord


class ContextArtifactSink(Protocol):
    async def record(self, envelope: ContextEnvelope) -> None: ...


class NullContextArtifactSink:
    async def record(self, envelope: ContextEnvelope) -> None:
        del envelope


class PostgresContextArtifactSink:
    async def record(self, envelope: ContextEnvelope) -> None:
        async with tenant_session(envelope.tenant_id) as session:
            session.add(
                ContextArtifactRecord(
                    tenant_id=envelope.tenant_id,
                    run_id=envelope.run_id,
                    task_id=envelope.task_id,
                    agent_role=envelope.agent.value,
                    contract_version=envelope.contract_version,
                    input_hash=envelope.input_hash,
                    selection_manifest=[
                        item.model_dump(mode="json") for item in envelope.selection_manifest
                    ],
                    token_budget=envelope.budget.max_input_tokens,
                    tokens_used=envelope.budget.tokens_used,
                    redaction_count=envelope.redaction_count,
                )
            )
            await session.flush()
