"""Create Phase 5 governed memory, model, and context authority tables."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_phase5_governance"
down_revision = "0007_rag_index_fixes"
branch_labels = None
depends_on = None
UUID = postgresql.UUID(as_uuid=True)


def _enable_rls(table: str) -> None:
    expression = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY "{table}_tenant_isolation" ON "{table}" '
        f"USING ({expression}) WITH CHECK ({expression})"
    )


def upgrade() -> None:
    op.create_table(
        "memory_records",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("lineage_id", UUID, nullable=False),
        sa.Column("scope_type", sa.String(20), nullable=False),
        sa.Column("scope_id", sa.String(255)),
        sa.Column("memory_type", sa.String(20), nullable=False),
        sa.Column("semantic_subtype", sa.String(30)),
        sa.Column("subject_key", sa.String(500), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("source_run_id", UUID, sa.ForeignKey("agent_runs.id", ondelete="SET NULL")),
        sa.Column("source_trace_id", sa.String(255), nullable=False),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("supporting_episode_ids", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("importance", sa.Float(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("taint_labels", sa.JSON(), nullable=False),
        sa.Column("consent_ref", sa.String(500)),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("activation_reason", sa.String(1000)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("tenant_id", "idempotency_key"),
        sa.UniqueConstraint(
            "tenant_id", "scope_type", "scope_id", "memory_type", "subject_key", "version"
        ),
        sa.CheckConstraint(
            "memory_type IN ('semantic', 'episodic', 'procedural')", name="ck_memory_records_type"
        ),
        sa.CheckConstraint(
            "status IN ('candidate', 'quarantine', 'active', 'superseded', 'revoked', 'expired')",
            name="ck_memory_records_status",
        ),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_memory_confidence"),
        sa.CheckConstraint("importance >= 0 AND importance <= 1", name="ck_memory_importance"),
        sa.CheckConstraint(
            "(scope_type = 'tenant' AND scope_id IS NULL) OR "
            "(scope_type <> 'tenant' AND scope_id IS NOT NULL)",
            name="ck_memory_scope_id",
        ),
    )
    op.create_index(
        "ix_memory_records_retrieval",
        "memory_records",
        ["tenant_id", "status", "memory_type", "scope_type", "scope_id"],
    )
    op.create_index(
        "ix_memory_records_subject",
        "memory_records",
        ["tenant_id", "scope_type", "scope_id", "memory_type", "subject_key"],
    )
    op.create_table(
        "memory_events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "memory_id",
            UUID,
            sa.ForeignKey("memory_records.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("actor_id", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_memory_events_tenant_id", "memory_events", ["tenant_id"])

    op.create_table(
        "model_invocations",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("request_id", UUID, nullable=False),
        sa.Column("run_id", UUID, sa.ForeignKey("agent_runs.id", ondelete="SET NULL")),
        sa.Column("task_id", sa.String(100)),
        sa.Column("agent_role", sa.String(80), nullable=False),
        sa.Column("purpose", sa.String(80), nullable=False),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("model_revision", sa.String(255), nullable=False),
        sa.Column("route_reason", sa.String(500), nullable=False),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.Column("prompt_hash", sa.String(64), nullable=False),
        sa.Column("schema_hash", sa.String(64), nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("cost_usd", sa.Float(), server_default="0", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="1", nullable=False),
        sa.Column("retries", sa.Integer(), server_default="0", nullable=False),
        sa.Column("fallback_from", sa.String(255)),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("error_code", sa.String(100)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("tenant_id", "request_id"),
        sa.CheckConstraint("input_tokens >= 0 AND output_tokens >= 0", name="ck_model_tokens"),
        sa.CheckConstraint("latency_ms >= 0 AND cost_usd >= 0", name="ck_model_accounting"),
    )
    op.create_index(
        "ix_model_invocations_run",
        "model_invocations",
        ["tenant_id", "run_id", "created_at"],
    )
    op.create_table(
        "context_artifacts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "run_id", UUID, sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("task_id", sa.String(100), nullable=False),
        sa.Column("agent_role", sa.String(80), nullable=False),
        sa.Column("contract_version", sa.String(100), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("selection_manifest", sa.JSON(), nullable=False),
        sa.Column("token_budget", sa.Integer(), nullable=False),
        sa.Column("tokens_used", sa.Integer(), nullable=False),
        sa.Column("redaction_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_context_artifacts_run",
        "context_artifacts",
        ["tenant_id", "run_id", "created_at"],
    )

    for table in ("memory_records", "memory_events", "model_invocations", "context_artifacts"):
        _enable_rls(table)

    op.execute(
        """
        CREATE OR REPLACE FUNCTION servicemind_reject_governance_audit_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'governance audit records are append-only';
        END;
        $$
        """
    )
    for table in ("memory_events", "model_invocations", "context_artifacts"):
        op.execute(
            f'CREATE TRIGGER "{table}_append_only" BEFORE UPDATE OR DELETE ON "{table}" '
            "FOR EACH ROW EXECUTE FUNCTION servicemind_reject_governance_audit_mutation()"
        )


def downgrade() -> None:
    for table in ("context_artifacts", "model_invocations", "memory_events"):
        op.execute(f'DROP TRIGGER IF EXISTS "{table}_append_only" ON "{table}"')
    op.execute("DROP FUNCTION IF EXISTS servicemind_reject_governance_audit_mutation()")
    for table in ("context_artifacts", "model_invocations", "memory_events", "memory_records"):
        op.drop_table(table)
