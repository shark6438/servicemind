"""Live Phase 5 authority checks against the configured PostgreSQL runtime role."""

from __future__ import annotations

import asyncio
import hashlib
import sys
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from core import settings
from servicemind.context.builder import ContextBuilder
from servicemind.context.contracts import ContextAgent, ContextItem, ContextSource, TrustLabel
from servicemind.context.repository import PostgresContextArtifactSink
from servicemind.memory.contracts import (
    CROSS_TICKET_PROCEDURE_POLICY,
    POST_RUN_MEMORY_WRITER,
    POST_RUN_PROCEDURAL_WRITER,
    POST_RUN_TENANT_EPISODE_POLICY,
    MemoryCandidate,
    MemoryEvidenceRef,
    MemoryPatternQuery,
    MemoryQuery,
    MemoryReviewQuery,
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
    second_acme_run = await ServiceMindRepository(ACME).create_run(
        user_id="phase5-live-verifier",
        ticket_id=2,
        goal="Phase 5 procedural review verification",
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

    pattern_key = hashlib.sha256(f"phase5-live:{uuid4()}".encode()).hexdigest()
    episode_records = []
    for ticket_id, source_run in ((1, run), (2, second_acme_run)):
        episode = await writer.write(
            MemoryCandidate(
                tenant_id=ACME,
                scope=MemoryScope(),
                memory_type=MemoryType.EPISODIC,
                subject_key=f"phase5-live-ticket-{ticket_id}-{pattern_key}",
                content=f"Verified cross-ticket procedural support {ticket_id} for {pattern_key}",
                source_run_id=source_run.id,
                source_trace_id=f"phase5-live-procedure-{ticket_id}",
                evidence_refs=(
                    MemoryEvidenceRef(
                        evidence_id=f"phase5-live-procedure-{pattern_key}-{ticket_id}",
                        source_ref=f"acceptance://procedure/{ticket_id}",
                        content_hash=hashlib.sha256(
                            f"evidence:{pattern_key}:{ticket_id}".encode()
                        ).hexdigest(),
                        verified=True,
                    ),
                ),
                final_state_verified=True,
                confidence=0.99,
                importance=0.8,
                provenance={
                    "post_run_scope": POST_RUN_TENANT_EPISODE_POLICY,
                    "procedure_pattern_key": pattern_key,
                    "source_ticket_id": ticket_id,
                    "required_entity_ids": [1],
                    "required_group_ids": [1],
                },
                created_by=POST_RUN_MEMORY_WRITER,
            )
        )
        assert episode is not None and episode.status is MemoryStatus.ACTIVE
        episode_records.append(episode)
    support = await acme_repository.pattern_episodes(
        MemoryPatternQuery(
            tenant_id=ACME,
            pattern_key=pattern_key,
            entity_ids=frozenset({1}),
            group_ids=frozenset({1}),
        )
    )
    assert {record.memory_id for record in support} == {
        record.memory_id for record in episode_records
    }
    procedure = await writer.write(
        MemoryCandidate(
            tenant_id=ACME,
            scope=MemoryScope(),
            memory_type=MemoryType.PROCEDURAL,
            subject_key=f"phase5-live-procedure-{pattern_key}",
            content=f"Verify both independently reviewed incidents before applying {pattern_key}",
            source_run_id=second_acme_run.id,
            source_trace_id="phase5-live-procedural-review",
            evidence_refs=tuple(
                evidence for episode in episode_records for evidence in episode.evidence_refs
            ),
            supporting_episode_ids=tuple(record.memory_id for record in episode_records),
            confidence=0.99,
            importance=0.9,
            provenance={
                "derivation_policy": CROSS_TICKET_PROCEDURE_POLICY,
                "procedure_pattern_key": pattern_key,
                "source_ticket_ids": ["1", "2"],
                "required_entity_ids": [1],
                "required_group_ids": [1],
            },
            created_by=POST_RUN_PROCEDURAL_WRITER,
        )
    )
    assert procedure is not None and procedure.status is MemoryStatus.QUARANTINE
    review_query = MemoryReviewQuery(
        tenant_id=ACME,
        reviewer_id="phase5-live-approver",
        entity_ids=frozenset({1}),
        group_ids=frozenset({1}),
    )
    assert procedure.memory_id in {
        record.memory_id for record in await acme_repository.list_review_queue(review_query)
    }
    denied_review = review_query.model_copy(update={"group_ids": frozenset()})
    assert await acme_repository.get_for_review(denied_review, procedure.memory_id) is None
    try:
        await acme_repository.transition(
            procedure.memory_id,
            MemoryStatus.ACTIVE,
            actor_id="phase5-live-approver",
            reason="HUMAN_REVIEW_ACTIVATE",
            human_review_ref=f"acceptance://review/{pattern_key}",
            review_comment="Live exact-snapshot rejection probe.",
            expected_version=procedure.version,
            expected_content_hash="0" * 64,
            expected_status=MemoryStatus.QUARANTINE,
        )
    except ValueError as exc:
        assert "content changed" in str(exc)
    else:
        raise AssertionError("stale memory review snapshot unexpectedly activated")
    activated_procedure = await acme_repository.transition(
        procedure.memory_id,
        MemoryStatus.ACTIVE,
        actor_id="phase5-live-approver",
        reason="HUMAN_REVIEW_ACTIVATE",
        human_review_ref=f"acceptance://review/{pattern_key}",
        review_comment="Live procedure sources and ACL verified.",
        expected_version=procedure.version,
        expected_content_hash=procedure.content_hash,
        expected_status=MemoryStatus.QUARANTINE,
    )
    assert activated_procedure.status is MemoryStatus.ACTIVE

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

    # A procedure's own 180-day TTL must not let it outlive the 90-day episodes
    # that authorized it. Force both support windows to lapse, then prove that a
    # normal serving read expires the episodes, revokes the procedure, and emits
    # the append-only automatic decision event.
    async with tenant_session(ACME) as session:
        support_rows = list(
            (
                await session.execute(
                    select(MemoryRecordRow)
                    .where(MemoryRecordRow.id.in_([record.memory_id for record in episode_records]))
                    .with_for_update()
                )
            ).scalars()
        )
        lapsed_at = datetime.now(UTC) - timedelta(seconds=1)
        for row in support_rows:
            row.expires_at = lapsed_at
    after_support_expiry = await acme_repository.candidates(
        MemoryQuery(
            tenant_id=ACME,
            text=activated_procedure.content,
            user_id="phase5-live-verifier",
            entity_ids=frozenset({1}),
            group_ids=frozenset({1}),
        )
    )
    assert activated_procedure.memory_id not in {
        record.memory_id for record in after_support_expiry
    }
    async with tenant_session(ACME) as session:
        revoked_procedure = (
            await session.execute(
                select(MemoryRecordRow).where(MemoryRecordRow.id == activated_procedure.memory_id)
            )
        ).scalar_one()
        invalidation_events = list(
            (
                await session.execute(
                    select(MemoryEventRecord).where(
                        MemoryEventRecord.memory_id == activated_procedure.memory_id,
                        MemoryEventRecord.event_type == "memory.revoked",
                    )
                )
            ).scalars()
        )
    assert revoked_procedure.status == MemoryStatus.REVOKED.value
    assert [event.reason_codes for event in invalidation_events] == [
        ["PROCEDURAL_SUPPORT_INVALIDATED"]
    ]

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
        revision = (
            await session.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()
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
            )
            .tuples()
            .all()
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
                    text("SELECT conname FROM pg_constraint WHERE conname = ANY(:constraints)"),
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
    # Phase 5 tables remain valid under the current linear migration head.
    assert revision == "0013_phase6_hash_guards"
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
        "ACL-scoped procedural review, exact-snapshot concurrency control, and "
        "support-lifetime revocation, and append-only governance audit"
    )
    await close_database()


if __name__ == "__main__":
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(main(), loop_factory=loop_factory)
