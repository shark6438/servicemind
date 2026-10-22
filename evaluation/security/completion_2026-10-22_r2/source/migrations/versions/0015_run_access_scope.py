"""Persist the maximum data scope a run's readers must hold."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015_run_access_scope"
down_revision = "0014_memory_event_run"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("access_scope", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "access_scope")
