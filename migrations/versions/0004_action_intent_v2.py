"""Persist the complete reviewed ActionIntent v2 security context.

Revision ID: 0004_action_intent_v2
Revises: 0003_append_only_audit
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_action_intent_v2"
down_revision = "0003_append_only_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "action_intents",
        sa.Column("intent_version", sa.String(length=20), server_default="v1", nullable=False),
    )
    op.add_column(
        "action_intents", sa.Column("policy_version", sa.String(length=100), nullable=True)
    )
    op.add_column(
        "action_intents", sa.Column("review_digest", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "action_intents", sa.Column("evidence_digest", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "action_intents",
        sa.Column(
            "evidence_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "action_intents",
        sa.Column(
            "idempotency_context",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "action_intents", sa.Column("requested_by", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "action_intents", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("action_intents", sa.Column("dry_run_preview", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("action_intents", "dry_run_preview")
    op.drop_column("action_intents", "expires_at")
    op.drop_column("action_intents", "requested_by")
    op.drop_column("action_intents", "idempotency_context")
    op.drop_column("action_intents", "evidence_refs")
    op.drop_column("action_intents", "evidence_digest")
    op.drop_column("action_intents", "review_digest")
    op.drop_column("action_intents", "policy_version")
    op.drop_column("action_intents", "intent_version")
