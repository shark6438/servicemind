"""Record token and pricing provenance for every governed model invocation."""

import sqlalchemy as sa
from alembic import op

revision = "0009_model_accounting_provenance"
down_revision = "0008_phase5_governance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_invocations",
        sa.Column(
            "token_accounting_source",
            sa.String(40),
            server_default="estimated",
            nullable=False,
        ),
    )
    op.add_column(
        "model_invocations",
        sa.Column(
            "pricing_version",
            sa.String(100),
            server_default="unconfigured",
            nullable=False,
        ),
    )
    op.add_column(
        "model_invocations",
        sa.Column("cost_estimate", sa.Boolean(), server_default=sa.true(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("model_invocations", "cost_estimate")
    op.drop_column("model_invocations", "pricing_version")
    op.drop_column("model_invocations", "token_accounting_source")
