"""Add distributed leases to durable MCP tasks."""

import sqlalchemy as sa
from alembic import op

revision = "0012_mcp_task_leases"
down_revision = "0011_phase6_tool_platform"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "mcp_tasks",
        sa.Column("lease_owner", sa.String(100), server_default="migration", nullable=False),
    )
    op.add_column(
        "mcp_tasks",
        sa.Column(
            "lease_expires_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.alter_column("mcp_tasks", "lease_owner", server_default=None)
    op.alter_column("mcp_tasks", "lease_expires_at", server_default=None)
    op.create_index(
        "ix_mcp_tasks_lease",
        "mcp_tasks",
        ["tenant_id", "status", "lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_mcp_tasks_lease", table_name="mcp_tasks")
    op.drop_column("mcp_tasks", "lease_expires_at")
    op.drop_column("mcp_tasks", "lease_owner")
