"""Make tenant audit records append-only at the database boundary."""

from alembic import op

revision = "0003_append_only_audit"
down_revision = "0002_webhook_secrets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION servicemind_reject_audit_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_append_only
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION servicemind_reject_audit_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_events_append_only ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS servicemind_reject_audit_mutation()")
