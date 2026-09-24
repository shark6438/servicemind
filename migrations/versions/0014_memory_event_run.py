"""Correlate memory transitions with the run that caused them.

``memory_records.source_run_id`` already answers "which run authored this memory", so a
record's creation could be traced back. A transition on it could not: ``memory_events``
recorded the actor and nothing about the run. The two columns are nullable because three
of the writers -- TTL expiry, procedure support revalidation, and human review -- are not
run-scoped, and naming a run for them would be a fabrication.
"""

import sqlalchemy as sa
from alembic import op

revision = "0014_memory_event_run"
down_revision = "0013_phase6_hash_guards"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "memory_events",
        sa.Column("run_id", sa.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "memory_events",
        sa.Column("trace_id", sa.String(length=255), nullable=True),
    )
    op.create_foreign_key(
        "fk_memory_events_run",
        "memory_events",
        "agent_runs",
        ["run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_memory_events_run",
        "memory_events",
        ["tenant_id", "run_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_memory_events_run", table_name="memory_events")
    op.drop_constraint("fk_memory_events_run", "memory_events", type_="foreignkey")
    op.drop_column("memory_events", "trace_id")
    op.drop_column("memory_events", "run_id")
