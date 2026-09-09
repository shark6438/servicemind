"""Create ServiceMind Phase 2 enterprise core tables and RLS policies.

This revision intentionally declares its schema explicitly. Historical migrations must
not import mutable application ORM metadata, otherwise a fresh install can produce a
different database than an already-upgraded environment.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_phase2"
down_revision = None
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
RLS_TABLES = [
    "tenant_memberships",
    "glpi_integrations",
    "agent_runs",
    "run_events",
    "action_intents",
    "approvals",
    "tool_invocations",
    "audit_events",
    "idempotency_records",
]


def _uuid(name: str, *, primary_key: bool = False, nullable: bool = False) -> sa.Column:
    return sa.Column(name, UUID, primary_key=primary_key, nullable=nullable)


def _created_at(name: str = "created_at") -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "tenants",
        _uuid("id", primary_key=True),
        sa.Column("slug", sa.String(80), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        _created_at(),
    )
    op.create_table(
        "tenant_memberships",
        _uuid("id", primary_key=True),
        _uuid("tenant_id"),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("roles", sa.JSON(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.UniqueConstraint("tenant_id", "user_id"),
    )
    op.create_index("ix_tenant_memberships_tenant_id", "tenant_memberships", ["tenant_id"])
    op.create_index("ix_tenant_memberships_user_id", "tenant_memberships", ["user_id"])

    op.create_table(
        "glpi_integrations",
        _uuid("id", primary_key=True),
        _uuid("tenant_id"),
        sa.Column("base_url", sa.String(500), nullable=False),
        sa.Column("api_version", sa.String(20), nullable=False),
        sa.Column("client_id_encrypted", sa.Text(), nullable=False),
        sa.Column("client_secret_encrypted", sa.Text(), nullable=False),
        sa.Column("username_encrypted", sa.Text(), nullable=False),
        sa.Column("password_encrypted", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        _created_at(),
        _created_at("updated_at"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.UniqueConstraint("tenant_id"),
    )
    op.create_index("ix_glpi_integrations_tenant_id", "glpi_integrations", ["tenant_id"])

    op.create_table(
        "agent_runs",
        _uuid("id", primary_key=True),
        _uuid("tenant_id"),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("thread_id", sa.String(255), nullable=False, unique=True),
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("request_write", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        _created_at(),
        _created_at("updated_at"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
    )
    op.create_index("ix_agent_runs_tenant_id", "agent_runs", ["tenant_id"])
    op.create_index("ix_agent_runs_user_id", "agent_runs", ["user_id"])
    op.create_index("ix_agent_runs_status", "agent_runs", ["status"])

    op.create_table(
        "run_events",
        _uuid("id", primary_key=True),
        _uuid("tenant_id"),
        _uuid("run_id"),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"]),
        sa.UniqueConstraint("run_id", "sequence"),
    )
    op.create_index("ix_run_events_tenant_id", "run_events", ["tenant_id"])
    op.create_index("ix_run_events_run_id", "run_events", ["run_id"])

    op.create_table(
        "action_intents",
        _uuid("id", primary_key=True),
        _uuid("tenant_id"),
        _uuid("run_id"),
        sa.Column("action_type", sa.String(80), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=False),
        sa.Column("risk_level", sa.String(20), nullable=False),
        sa.Column("requires_approval", sa.Boolean(), nullable=False),
        sa.Column("action_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"]),
        sa.UniqueConstraint("run_id"),
    )
    op.create_index("ix_action_intents_tenant_id", "action_intents", ["tenant_id"])
    op.create_index("ix_action_intents_action_hash", "action_intents", ["action_hash"])

    op.create_table(
        "approvals",
        _uuid("id", primary_key=True),
        _uuid("tenant_id"),
        _uuid("run_id"),
        _uuid("action_intent_id"),
        sa.Column("decision", sa.String(20), nullable=False),
        sa.Column("decided_by", sa.String(255), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        _created_at("decided_at"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"]),
        sa.ForeignKeyConstraint(["action_intent_id"], ["action_intents.id"]),
        sa.UniqueConstraint("action_intent_id"),
    )
    op.create_index("ix_approvals_tenant_id", "approvals", ["tenant_id"])
    op.create_index("ix_approvals_run_id", "approvals", ["run_id"])

    op.create_table(
        "tool_invocations",
        _uuid("id", primary_key=True),
        _uuid("tenant_id"),
        _uuid("run_id"),
        sa.Column("tool_name", sa.String(160), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("result_ref", sa.String(500), nullable=True),
        _created_at(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"]),
    )
    op.create_index("ix_tool_invocations_tenant_id", "tool_invocations", ["tenant_id"])
    op.create_index("ix_tool_invocations_run_id", "tool_invocations", ["run_id"])

    op.create_table(
        "audit_events",
        _uuid("id", primary_key=True),
        _uuid("tenant_id"),
        sa.Column("run_id", UUID, nullable=True),
        sa.Column("actor_id", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("resource_type", sa.String(100), nullable=False),
        sa.Column("resource_id", sa.String(255), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
    )
    op.create_index("ix_audit_events_tenant_id", "audit_events", ["tenant_id"])
    op.create_index("ix_audit_events_run_id", "audit_events", ["run_id"])

    op.create_table(
        "idempotency_records",
        _uuid("id", primary_key=True),
        _uuid("tenant_id"),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("action_hash", sa.String(64), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("completed", sa.Boolean(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.UniqueConstraint("tenant_id", "idempotency_key"),
    )
    op.create_index("ix_idempotency_records_tenant_id", "idempotency_records", ["tenant_id"])

    tenant_expression = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
    for table in RLS_TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY "{table}_tenant_isolation" ON "{table}" '
            f"USING ({tenant_expression}) WITH CHECK ({tenant_expression})"
        )


def downgrade() -> None:
    for table in reversed(RLS_TABLES):
        op.drop_table(table)
    op.drop_table("tenants")
