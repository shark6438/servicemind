"""Add per-tenant encrypted GLPI webhook secrets."""

from alembic import op

revision = "0002_webhook_secrets"
down_revision = "0001_phase2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE glpi_integrations ADD COLUMN IF NOT EXISTS webhook_secret_encrypted TEXT"
    )
    op.execute(
        "UPDATE glpi_integrations SET webhook_secret_encrypted = '' "
        "WHERE webhook_secret_encrypted IS NULL"
    )
    op.execute("ALTER TABLE glpi_integrations ALTER COLUMN webhook_secret_encrypted SET NOT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE glpi_integrations DROP COLUMN IF EXISTS webhook_secret_encrypted")
