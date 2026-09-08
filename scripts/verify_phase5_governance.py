"""Live Phase 5 authority checks against the configured PostgreSQL runtime role."""

from __future__ import annotations

import asyncio
import sys
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from core import settings
from servicemind.context.builder import ContextBuilder
from servicemind.context.contracts import ContextAgent, ContextItem, ContextSource, TrustLabel
from servicemind.context.repository import PostgresContextArtifactSink
from servicemind.memory.contracts import (
    MemoryCandidate,
    MemoryEvidenceRef,
    MemoryQuery,
    MemoryScope,
    MemoryStatus,
    MemoryType,
    SemanticSubtype,
)
from servicemind.memory.repository import PostgresMemoryRepository
from servicemind.memory.service import CachedMemoryEmbeddingProvider, MemoryRetriever, MemoryWriter
from servicemind.model_gateway.contracts import (
    ModelCallContext,
    ModelInvocationAudit,
    ModelPurpose,
    ModelRouteDecision,
)
from servicemind.model_gateway.repository import PostgresModelAuditSink
from servicemind.persistence.database import close_database, global_session, tenant_session
from servicemind.persistence.models import (
    ContextArtifactRecord,
    MemoryEventRecord,
    MemoryRecordRow,
    ModelInvocationRecord,
)
from servicemind.persistence.repository import ServiceMindRepository
from servicemind.rag.models import TeiEmbeddingProvider

ACME = UUID("11111111-1111-4111-8111-111111111111")
GLOBEX = UUID("22222222-2222-4222-8222-222222222222")


def candidate(
    tenant_id: UUID, *, subject: str, run_id: UUID, content: str | None = None
) -> MemoryCandidate:
    return MemoryCandidate(
        tenant_id=tenant_id,
        scope=MemoryScope(),
        memory_type=MemoryType.SEMANTIC,
        semantic_subtype=SemanticSubtype.LEARNED_FACT,
        subject_key=subject,
        content=content or f"Verified Phase 5 acceptance fact for {subject}",
        source_run_id=run_id,
        source_trace_id=f"phase5-live-{subject}",
        evidence_refs=(
            MemoryEvidenceRef(
                evidence_id=f"phase5-{subject}",
                source_ref=f"acceptance://{subject}",
                content_hash="a" * 64,
                verified=True,
            ),
        ),
        confidence=0.99,
        importance=0.8,
        created_by="phase5-live-verifier",
    )


async def assert_append_only(table: str, row_id: UUID) -> None:
    try:
        async with tenant_session(ACME) as session:
            await session.execute(
                text(f'UPDATE "{table}" SET created_at = created_at WHERE id = :id'),
                {"id": row_id},
            )
    except DBAPIError as exc:
        if "append-only" not in str(exc.orig):
            raise
    else:
        raise AssertionError(f"{table} UPDATE unexpectedly succeeded")


async def main() -> None:
    run = await ServiceMindRepository(ACME).create_run(
        user_id="phase5-live-verifier",
        ticket_id=1,
        goal="Phase 5 governance verification",
        request_write=False,
    )
    globex_run = await ServiceMindRepository(GLOBEX).create_run(
        user_id="phase5-live-verifier",
        ticket_id=1,
        goal="Phase 5 tenant isolation verification",
        request_write=False,
    )
    subject = f"concurrency-{uuid4()}"
    acme_repository = PostgresMemoryRepository(ACME)
    writer = MemoryWriter(acme_repository)
    writes = await asyncio.gather(
        *(writer.write(candidate(ACME, subject=subject, run_id=run.id)) for _ in range(12))
    )
    memory_ids = {item.memory_id for item in writes if item is not None}
    assert len(memory_ids) == 1, "concurrent replay created duplicate memory"
    acme_memory_id = memory_ids.pop()
    await MemoryWriter(PostgresMemoryRepository(GLOBEX)).write(
        candidate(GLOBEX, subject=subject, run_id=globex_run.id)
    )

    selections = await MemoryRetriever(acme_repository).retrieve(
        MemoryQuery(tenant_id=ACME, text=subject, user_id="phase5-live-verifier")
    )
    matching = [item.memory for item in selections if item.memory.subject_key == subject]
    assert [item.memory_id for item in matching] == [acme_memory_id]
    assert all(item.memory.tenant_id == ACME for item in selections)
    try:
        await acme_repository.candidates(
            MemoryQuery(tenant_id=GLOBEX, text=subject, user_id="phase5-live-verifier")
        )
    except PermissionError:
        pass
    else:
        raise AssertionError("repository accepted a cross-tenant memory query")

    if not settings.SERVICEMIND_EMBEDDING_URL:
        raise RuntimeError("SERVICEMIND_EMBEDDING_URL is required for Phase 5 acceptance")
    relevant = await writer.write(
        candidate(
            ACME,
            subject=f"vector-relevant-{uuid4()}",
            run_id=run.id,
            content=(
                "Verified VPN multi-factor authentication failures are handled by "
                "the Identity Team."
            ),
        )
    )
    await writer.write(
        candidate(
            ACME,
            subject=f"vector-distractor-{uuid4()}",
            run_id=run.id,
            content="Verified printer toner requests are handled by Workplace Services.",
        )
    )
    vector_retriever = MemoryRetriever(
        acme_repository,
        embedding=CachedMemoryEmbeddingProvider(
            TeiEmbeddingProvider(
                settings.SERVICEMIND_EMBEDDING_URL,
                model_revision=settings.SERVICEMIND_EMBEDDING_REVISION,
            )
        ),
        candidate_ceiling=100,
    )
    vector_results = await vector_retriever.retrieve(
        MemoryQuery(
            tenant_id=ACME,
            text="Who owns a failed MFA login for remote VPN access?",
            user_id="phase5-live-verifier",
        )
    )
    assert relevant is not None and vector_results[0].memory.memory_id == relevant.memory_id

    context = ContextBuilder().build(
        tenant_id=ACME,
        run_id=run.id,
        task_id="T1",
        agent=ContextAgent.ANALYSIS,
        items=(
            ContextItem(
                item_id="task",
                source=ContextSource.TASK,
                content="Analyze acceptance evidence",
                allowed_agents=frozenset({ContextAgent.ANALYSIS}),
                trust=TrustLabel.TRUSTED_CONTROL,
                authority=1,
                relevance=1,
                required=True,
                provenance_ref="acceptance://task",
            ),
        ),
        max_input_tokens=2048,
        system_reserve=128,
        output_reserve=256,
    )
    await PostgresContextArtifactSink().record(context)
    model_audit = ModelInvocationAudit(
        context=ModelCallContext(
            tenant_id=ACME,
            run_id=run.id,
            task_id="T1",
            agent_role="analysis",
            purpose=ModelPurpose.ANALYSIS,
            policy_version="phase5-acceptance",
            prompt_version="phase5-acceptance",
        ),
        route=ModelRouteDecision(
            provider="fake",
            model="fake",
            model_revision="acceptance",
            reason="live_schema_verification",
        ),
        prompt_hash="b" * 64,
        schema_hash="c" * 64,
        input_tokens=1,
        output_tokens=1,
        latency_ms=1,
        cost_usd=0,
        token_accounting_source="provider",
        pricing_version="acceptance-test",
        cost_estimate=False,
        attempts=1,
        retries=0,
        status="succeeded",
    )
    await PostgresModelAuditSink().record(model_audit)

    async with global_session() as session:
        revision = (await session.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
        rls = dict(
            (
                await session.execute(
                    text(
                        "SELECT relname, relrowsecurity AND relforcerowsecurity "
                        "FROM pg_class WHERE relname = ANY(:tables)"
                    ),
                    {
                        "tables": [
                            "memory_records",
                            "memory_events",
                            "model_invocations",
                            "context_artifacts",
                        ]
                    },
                )
            ).all()
        )
        zero_context_count = (
            await session.execute(select(func.count()).select_from(MemoryRecordRow))
        ).scalar_one()
        policies = (
            await session.execute(
                text(
                    "SELECT count(*) FROM pg_policies WHERE tablename = ANY(:tables) "
                    "AND policyname LIKE '%tenant_isolation'"
                ),
                {
                    "tables": [
                        "memory_records",
                        "memory_events",
                        "model_invocations",
                        "context_artifacts",
                    ]
                },
            )
        ).scalar_one()
        append_only_triggers = (
            await session.execute(
                text(
                    "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal "
                    "AND tgname = ANY(:triggers)"
                ),
                {
                    "triggers": [
                        "memory_events_append_only",
                        "model_invocations_append_only",
                        "context_artifacts_append_only",
                    ]
                },
            )
        ).scalar_one()
        checks = set(
            (
                await session.execute(
                    text(
                        "SELECT conname FROM pg_constraint WHERE conname = ANY(:constraints)"
                    ),
                    {
                        "constraints": [
                            "ck_memory_records_type",
                            "ck_memory_records_status",
                            "ck_memory_confidence",
                            "ck_memory_importance",
                            "ck_memory_scope_id",
                            "ck_model_tokens",
                            "ck_model_accounting",
                        ]
                    },
                )
            ).scalars()
        )
    assert revision == "0009_model_accounting_provenance"
    assert rls == {
        "memory_records": True,
        "memory_events": True,
        "model_invocations": True,
        "context_artifacts": True,
    }
    assert zero_context_count == 0
    assert policies == 4
    assert append_only_triggers == 3
    assert checks == {
        "ck_memory_records_type",
        "ck_memory_records_status",
        "ck_memory_confidence",
        "ck_memory_importance",
        "ck_memory_scope_id",
        "ck_model_tokens",
        "ck_model_accounting",
    }

    async with tenant_session(ACME) as session:
        event_id = (
            await session.execute(
                select(MemoryEventRecord.id)
                .where(MemoryEventRecord.memory_id == acme_memory_id)
                .limit(1)
            )
        ).scalar_one()
        context_id = (
            await session.execute(
                select(ContextArtifactRecord.id)
                .where(ContextArtifactRecord.run_id == run.id)
                .limit(1)
            )
        ).scalar_one()
        invocation_id = (
            await session.execute(
                select(ModelInvocationRecord.id)
                .where(ModelInvocationRecord.request_id == model_audit.request_id)
                .limit(1)
            )
        ).scalar_one()
        stored = (
            await session.execute(
                select(MemoryRecordRow).where(MemoryRecordRow.id == acme_memory_id)
            )
        ).scalar_one()
    assert stored.status == MemoryStatus.ACTIVE.value
    await assert_append_only("memory_events", event_id)
    await assert_append_only("context_artifacts", context_id)
    await assert_append_only("model_invocations", invocation_id)
    print(
        "PASS phase5 database: migration head, forced RLS, zero-context denial, "
        "tenant isolation, concurrent idempotency, BGE-M3 vector ranking, and "
        "append-only governance audit"
    )
    await close_database()


if __name__ == "__main__":
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(main(), loop_factory=loop_factory)
