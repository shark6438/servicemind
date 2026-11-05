"""Add durable run leases and indexes for ordered tenant scans.

The recovery index is intentionally partial: terminal runs accumulate indefinitely,
whereas only live runs are candidates for restart. The timeline already has a
unique ``(run_id, sequence)`` index, so event paging needs no extra index.
"""

import sqlalchemy as sa
from alembic import op

revision = "0016_run_queue_paging"
down_revision = "0015_run_access_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("worker_lease_owner", sa.String(255), nullable=True))
    op.add_column(
        "agent_runs",
        sa.Column("worker_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_agent_runs_tenant_created_id",
        "agent_runs",
        ["tenant_id", "created_at", "id"],
    )
    op.create_index(
        "ix_agent_runs_recovery",
        "agent_runs",
        ["tenant_id", "created_at", "id"],
        postgresql_where=sa.text("status IN ('pending', 'running', 'waiting_approval')"),
    )


def downgrade() -> None:
    op.drop_index("ix_agent_runs_recovery", table_name="agent_runs")
    op.drop_index("ix_agent_runs_tenant_created_id", table_name="agent_runs")
    op.drop_column("agent_runs", "worker_lease_expires_at")
    op.drop_column("agent_runs", "worker_lease_owner")
