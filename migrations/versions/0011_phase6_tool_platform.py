"""Add Phase 6 governed tool audit, MCP tasks, and reliable transactional outbox."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011_phase6_tool_platform"
down_revision = "0010_memory_records_jsonb_acl"
branch_labels = None
depends_on = None
UUID = postgresql.UUID(as_uuid=True)


def _rls(table: str) -> None:
    expression = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY "{table}_tenant_isolation" ON "{table}" '
        f"USING ({expression}) WITH CHECK ({expression})"
    )


def _timestamps() -> tuple[sa.Column, ...]:
    return (
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def upgrade() -> None:
    op.create_table(
        "tool_policy_decisions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("decision_id", UUID, nullable=False),
        sa.Column("request_id", UUID, nullable=False),
        sa.Column(
            "run_id", UUID, sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("task_id", sa.String(100), nullable=False),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("tool_name", sa.String(160), nullable=False),
        sa.Column("tool_version", sa.String(40), nullable=False),
        sa.Column("tool_checksum", sa.String(64), nullable=False),
        sa.Column("argument_hash", sa.String(64), nullable=False),
        sa.Column("allow", sa.Boolean(), nullable=False),
        sa.Column("requires_approval", sa.Boolean(), nullable=False),
        sa.Column("policy_version", sa.String(200), nullable=False),
        sa.Column("external_decision_id", sa.String(255)),
        sa.Column("reason_codes", postgresql.JSONB(), nullable=False),
        sa.Column("context_manifest", postgresql.JSONB(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("tenant_id", "decision_id"),
    )
    op.create_index(
        "ix_tool_policy_run", "tool_policy_decisions", ["tenant_id", "run_id", "created_at"]
    )
    op.create_table(
        "governed_tool_invocations",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("request_id", UUID, nullable=False),
        sa.Column(
            "run_id", UUID, sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("task_id", sa.String(100), nullable=False),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("tool_name", sa.String(160), nullable=False),
        sa.Column("tool_version", sa.String(40), nullable=False),
        sa.Column("tool_checksum", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("argument_hash", sa.String(64), nullable=False),
        sa.Column("output_hash", sa.String(64)),
        sa.Column("policy_decision_id", UUID, nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("error_code", sa.String(100)),
        *_timestamps(),
        sa.UniqueConstraint("tenant_id", "request_id"),
        sa.CheckConstraint("attempts >= 0 AND attempts <= 5", name="ck_tool_invocation_attempts"),
        sa.CheckConstraint("latency_ms >= 0", name="ck_tool_invocation_latency"),
    )
    op.create_index(
        "ix_governed_tool_run", "governed_tool_invocations", ["tenant_id", "run_id", "created_at"]
    )
    op.create_table(
        "tool_outbox",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("aggregate_type", sa.String(80), nullable=False),
        sa.Column("aggregate_id", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("status", sa.String(30), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("lease_owner", sa.String(255)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(100)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("tenant_id", "idempotency_key"),
        sa.CheckConstraint(
            "status IN ('pending','leased','published','dead')", name="ck_tool_outbox_status"
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_tool_outbox_attempts"),
    )
    op.create_index(
        "ix_tool_outbox_dispatch", "tool_outbox", ["tenant_id", "status", "available_at"]
    )
    op.create_table(
        "mcp_tasks",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("task_id", UUID, nullable=False),
        sa.Column("request_id", UUID, nullable=False),
        sa.Column(
            "run_id", UUID, sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("workflow_task_id", sa.String(100), nullable=False),
        sa.Column("tool_name", sa.String(160), nullable=False),
        sa.Column("argument_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(30), server_default="working", nullable=False),
        sa.Column("output_ciphertext", sa.Text()),
        sa.Column("output_hash", sa.String(64)),
        sa.Column("error_code", sa.String(100)),
        sa.Column(
            "cancellation_requested", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("tenant_id", "task_id"),
        sa.UniqueConstraint("tenant_id", "request_id"),
        sa.CheckConstraint(
            "status IN ('working','completed','failed','cancelled')", name="ck_mcp_task_status"
        ),
    )
    op.create_index("ix_mcp_tasks_status", "mcp_tasks", ["tenant_id", "status", "updated_at"])
    for table in (
        "tool_policy_decisions",
        "governed_tool_invocations",
        "tool_outbox",
        "mcp_tasks",
    ):
        _rls(table)
    for table in ("tool_policy_decisions", "governed_tool_invocations"):
        op.execute(
            f'CREATE TRIGGER "{table}_append_only" BEFORE UPDATE OR DELETE ON "{table}" '
            "FOR EACH ROW EXECUTE FUNCTION servicemind_reject_governance_audit_mutation()"
        )


def downgrade() -> None:
    for table in ("governed_tool_invocations", "tool_policy_decisions"):
        op.execute(f'DROP TRIGGER IF EXISTS "{table}_append_only" ON "{table}"')
    for table in (
        "mcp_tasks",
        "tool_outbox",
        "governed_tool_invocations",
        "tool_policy_decisions",
    ):
        op.drop_table(table)
