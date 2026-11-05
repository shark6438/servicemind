"""Bind ordinary API retry keys to one tenant/user task atomically."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0017_run_submission_idempotency"
down_revision = "0016_run_queue_paging"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_submissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
        ),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "tenant_id", "user_id", "idempotency_key", name="uq_run_submission_key"
        ),
    )
    op.create_index("ix_run_submissions_tenant_id", "run_submissions", ["tenant_id"])
    op.execute('ALTER TABLE "run_submissions" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE "run_submissions" FORCE ROW LEVEL SECURITY')
    op.execute(
        "CREATE POLICY run_submissions_tenant_isolation ON run_submissions "
        "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
        "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
    )


def downgrade() -> None:
    op.drop_index("ix_run_submissions_tenant_id", table_name="run_submissions")
    op.drop_table("run_submissions")
