from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_REVIEW = "waiting_review"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ActionStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    #: The platform took the action back before any human ruled on it, because the
    #: evidence it was derived from stopped being the requester's to read. Distinct
    #: from REJECTED, which is a person saying no to something they were shown, and
    #: from FAILED, which is execution going wrong. Nothing was decided and nothing
    #: was attempted; the run re-derives the action under its narrowed scope and asks
    #: again. Only a PROPOSED action can be withdrawn -- once a decision or an
    #: execution has touched it, it is no longer the platform's to take back.
    WITHDRAWN = "withdrawn"


class IngestionJobStatus(StrEnum):
    """Lifecycle of one (tenant, source) ingestion run in ``knowledge_ingestion_jobs``.

    A run is opened as ``running`` by ``begin_ingestion_job`` (attempts += 1) and
    closed by ``complete_ingestion_job`` as ``succeeded`` or ``failed``. Rows are
    keyed on ``(tenant_id, source)``: the register keeps the *latest* state of each
    source, so a crashed pipeline never orphans a half-written document set and the
    publish gate still has PostgreSQL's ``count_pending`` as its authority.
    """

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TenantMembership(Base):
    __tablename__ = "tenant_memberships"
    __table_args__ = (UniqueConstraint("tenant_id", "user_id"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    roles: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class GlpiIntegration(Base):
    __tablename__ = "glpi_integrations"
    __table_args__ = (
        UniqueConstraint("tenant_id"),
        Index("ix_glpi_integrations_tenant_id", "tenant_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    api_version: Mapped[str] = mapped_column(String(20), default="v2.3", nullable=False)
    client_id_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    client_secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    username_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    password_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    webhook_secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    profile_id: Mapped[int] = mapped_column(Integer, default=6, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    thread_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    ticket_id: Mapped[int] = mapped_column(Integer, nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    request_write: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(
        String(40), default=RunStatus.PENDING.value, nullable=False, index=True
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (UniqueConstraint("run_id", "sequence"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent_runs.id"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ActionIntentRecord(Base):
    __tablename__ = "action_intents"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent_runs.id"), unique=True, nullable=False
    )
    action_type: Mapped[str] = mapped_column(String(80), nullable=False)
    target_id: Mapped[int] = mapped_column(Integer, nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    action_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    intent_version: Mapped[str] = mapped_column(
        String(20), default="v1", server_default="v1", nullable=False
    )
    policy_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    review_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_refs: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    idempotency_context: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict, nullable=False)
    requested_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dry_run_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(40), default=ActionStatus.PROPOSED.value, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (UniqueConstraint("action_intent_id"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent_runs.id"), nullable=False, index=True
    )
    action_intent_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("action_intents.id"), nullable=False
    )
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    decided_by: Mapped[str] = mapped_column(String(255), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ToolInvocation(Base):
    __tablename__ = "tool_invocations"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent_runs.id"), nullable=False, index=True
    )
    tool_name: Mapped[str] = mapped_column(String(160), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    result_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    run_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True, index=True)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    action_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class KnowledgeDocumentRecord(Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source", "source_record_id"),
        # The publish gate (``count_pending``) and reconciliation query inside the
        # RLS tenant window by ``tenant_id AND index_status``. This composite is
        # owned by migration 0006; declared here (not as column ``index=True``) so
        # create_all and the live schema agree instead of racing over the same name.
        Index("ix_knowledge_documents_index_status", "tenant_id", "index_status"),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    source_record_id: Mapped[str] = mapped_column(String(500), nullable=False)
    source_version: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    document_type: Mapped[str] = mapped_column(String(100), nullable=False)
    language: Mapped[str] = mapped_column(String(20), nullable=False)
    document_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    acl: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    index_status: Mapped[str] = mapped_column(
        String(40), default="pending_index", server_default="pending_index", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class KnowledgeParentChunkRecord(Base):
    __tablename__ = "knowledge_parent_chunks"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    document_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    section_path: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    block_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    chunk_order: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)


class KnowledgeChildChunkRecord(Base):
    __tablename__ = "knowledge_child_chunks"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    document_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_chunk_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("knowledge_parent_chunks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    section_path: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    chunk_order: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)


class KnowledgeIngestionJob(Base):
    __tablename__ = "knowledge_ingestion_jobs"
    __table_args__ = (
        # One live register row per source: re-ingesting a source advances the same
        # row (attempts += 1) instead of appending an audit log. Source history is
        # the documents' own (versioned) rows; this table is the status register.
        UniqueConstraint("tenant_id", "source"),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(40),
        default=IngestionJobStatus.RUNNING.value,
        server_default=IngestionJobStatus.RUNNING.value,
        nullable=False,
        index=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class MemoryRecordRow(Base):
    __tablename__ = "memory_records"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key"),
        UniqueConstraint(
            "tenant_id", "scope_type", "scope_id", "memory_type", "subject_key", "version"
        ),
        CheckConstraint(
            "memory_type IN ('semantic', 'episodic', 'procedural')",
            name="ck_memory_records_type",
        ),
        CheckConstraint(
            "status IN ('candidate', 'quarantine', 'active', 'superseded', 'revoked', 'expired')",
            name="ck_memory_records_status",
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_memory_confidence"),
        CheckConstraint("importance >= 0 AND importance <= 1", name="ck_memory_importance"),
        CheckConstraint(
            "(scope_type = 'tenant' AND scope_id IS NULL) OR "
            "(scope_type <> 'tenant' AND scope_id IS NOT NULL)",
            name="ck_memory_scope_id",
        ),
        Index(
            "ix_memory_records_retrieval",
            "tenant_id",
            "status",
            "memory_type",
            "scope_type",
            "scope_id",
        ),
        Index(
            "ix_memory_records_subject",
            "tenant_id",
            "scope_type",
            "scope_id",
            "memory_type",
            "subject_key",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    lineage_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(20), nullable=False)
    scope_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    memory_type: Mapped[str] = mapped_column(String(20), nullable=False)
    semantic_subtype: Mapped[str | None] = mapped_column(String(30), nullable=True)
    subject_key: Mapped[str] = mapped_column(String(500), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_run_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    source_trace_id: Mapped[str] = mapped_column(String(255), nullable=False)
    evidence_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    supporting_episode_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    importance: Mapped[float] = mapped_column(Float, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    # JSONB (not JSON): the governance read path narrows by jsonb operators
    # (`?`, `@>`, `[]` equality) over these columns (memory/repository.py
    # ``_read_filters``); generic JSON columns have no such operators and the
    # ACL pre-filter was an AttributeError at expression build time.
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    taint_labels: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    consent_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    activation_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class MemoryEventRecord(Base):
    """The append-only ledger of memory transitions.

    ``run_id``/``trace_id`` are the correlation this ledger was missing. A record knows
    which run authored it (``memory_records.source_run_id``), so the *creation* of a
    memory could always be traced back; a later transition on it could not. The gap
    matters most where it is least visible: a revocation is the one event an operator
    has to explain, and "somebody or something revoked this" is not an explanation.

    Both are nullable, and that is the honest encoding rather than a shortcut. Two of
    the six writers are maintenance that no run caused (TTL expiry, procedure support
    revalidation) and one is a human review decision taken outside any run; a NOT NULL
    column would force those to name a run that does not exist, which is the same class
    of error as a check that reports success for work it never did.
    """

    __tablename__ = "memory_events"
    __table_args__ = (
        # "What did this run do to the tenant's memory?" is the question the correlation
        # exists to answer, and it is asked per tenant over one run's events.
        Index("ix_memory_events_run", "tenant_id", "run_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    memory_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("memory_records.id", ondelete="CASCADE"), nullable=False
    )
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    # ``SET NULL`` for the same reason ``memory_records.source_run_id`` uses it: this is
    # an audit ledger, and a row that outlives the run it names must keep stating what
    # happened even after the run row itself is gone.
    run_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    trace_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ModelInvocationRecord(Base):
    __tablename__ = "model_invocations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "request_id"),
        CheckConstraint("input_tokens >= 0 AND output_tokens >= 0", name="ck_model_tokens"),
        CheckConstraint("latency_ms >= 0 AND cost_usd >= 0", name="ck_model_accounting"),
        Index("ix_model_invocations_run", "tenant_id", "run_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    request_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    run_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    task_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    agent_role: Mapped[str] = mapped_column(String(80), nullable=False)
    purpose: Mapped[str] = mapped_column(String(80), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    model_revision: Mapped[str] = mapped_column(String(255), nullable=False)
    route_reason: Mapped[str] = mapped_column(String(500), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    token_accounting_source: Mapped[str] = mapped_column(
        String(40), default="estimated", nullable=False
    )
    pricing_version: Mapped[str] = mapped_column(
        String(100), default="unconfigured", nullable=False
    )
    cost_estimate: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    retries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fallback_from: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ContextArtifactRecord(Base):
    __tablename__ = "context_artifacts"
    __table_args__ = (Index("ix_context_artifacts_run", "tenant_id", "run_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[str] = mapped_column(String(100), nullable=False)
    agent_role: Mapped[str] = mapped_column(String(80), nullable=False)
    contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    selection_manifest: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    token_budget: Mapped[int] = mapped_column(Integer, nullable=False)
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False)
    redaction_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ToolPolicyDecisionRecord(Base):
    __tablename__ = "tool_policy_decisions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "decision_id"),
        Index("ix_tool_policy_run", "tenant_id", "run_id", "created_at"),
        CheckConstraint(
            "tool_checksum ~ '^[0-9a-f]{64}$' AND argument_hash ~ '^[0-9a-f]{64}$'",
            name="ck_tool_policy_hashes",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    decision_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    request_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[str] = mapped_column(String(100), nullable=False)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(160), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(40), nullable=False)
    tool_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    argument_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    allow: Mapped[bool] = mapped_column(Boolean, nullable=False)
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(200), nullable=False)
    external_decision_id: Mapped[str | None] = mapped_column(String(255))
    reason_codes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    context_manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class GovernedToolInvocationRecord(Base):
    __tablename__ = "governed_tool_invocations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "request_id"),
        Index("ix_governed_tool_run", "tenant_id", "run_id", "created_at"),
        CheckConstraint("attempts >= 0 AND attempts <= 5", name="ck_tool_invocation_attempts"),
        CheckConstraint("latency_ms >= 0", name="ck_tool_invocation_latency"),
        CheckConstraint(
            "status IN ('succeeded','failed','denied','cancelled')",
            name="ck_tool_invocation_status",
        ),
        CheckConstraint(
            "tool_checksum ~ '^[0-9a-f]{64}$' AND argument_hash ~ '^[0-9a-f]{64}$' "
            "AND (output_hash IS NULL OR output_hash ~ '^[0-9a-f]{64}$')",
            name="ck_tool_invocation_hashes",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    request_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[str] = mapped_column(String(100), nullable=False)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(160), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(40), nullable=False)
    tool_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    argument_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_hash: Mapped[str | None] = mapped_column(String(64))
    policy_decision_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ToolOutboxRecord(Base):
    __tablename__ = "tool_outbox"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key"),
        Index("ix_tool_outbox_dispatch", "tenant_id", "status", "available_at"),
        CheckConstraint(
            "status IN ('pending','leased','published','dead')", name="ck_tool_outbox_status"
        ),
        CheckConstraint("attempts >= 0", name="ck_tool_outbox_attempts"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    aggregate_type: Mapped[str] = mapped_column(String(80), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class McpTaskRecord(Base):
    """Durable, tenant-scoped state for the stateless MCP Tasks extension.

    Tool output can contain confidential GLPI data, so only ciphertext is stored.
    The hashes remain queryable for integrity checks without exposing the payload.
    """

    __tablename__ = "mcp_tasks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "task_id"),
        UniqueConstraint("tenant_id", "request_id"),
        Index("ix_mcp_tasks_status", "tenant_id", "status", "updated_at"),
        Index("ix_mcp_tasks_lease", "tenant_id", "status", "lease_expires_at"),
        CheckConstraint(
            "status IN ('working','completed','failed','cancelled')", name="ck_mcp_task_status"
        ),
        CheckConstraint(
            "argument_hash ~ '^[0-9a-f]{64}$' "
            "AND (output_hash IS NULL OR output_hash ~ '^[0-9a-f]{64}$')",
            name="ck_mcp_task_hashes",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    task_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    request_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    workflow_task_id: Mapped[str] = mapped_column(String(100), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(160), nullable=False)
    argument_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="working", nullable=False)
    output_ciphertext: Mapped[str | None] = mapped_column(Text)
    output_hash: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(100))
    cancellation_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    lease_owner: Mapped[str] = mapped_column(String(100), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
