"""Convert memory_records provenance/taint_labels to JSONB.

The governance read path (``PostgresMemoryRepository._read_filters``) narrows
the candidate set with jsonb-only operators (``?`` existence, ``@>``/``<@``
containment, ``= '[]'``). Generic ``JSON`` columns compile neither
``.has_key``/``.contained_by`` (AttributeError at expression build time) nor a
valid empty-array equality, so every ``candidates()``/``revalidate()`` call
crashed. Converting the two columns to JSONB aligns the schema with the
operators already written against them; existing rows are cast in place.

Revision ID: 0010_memory_records_jsonb_acl
Revises: 0009_model_accounting_provenance
"""

import sqlalchemy as sa
from alembic import op

revision = "0010_memory_records_jsonb_acl"
down_revision = "0009_model_accounting_provenance"
branch_labels = None
depends_on = None

_JSONB_COLUMNS = ("provenance", "taint_labels")


def upgrade() -> None:
    for column in _JSONB_COLUMNS:
        op.execute(
            f'ALTER TABLE "memory_records" '
            f'ALTER COLUMN "{column}" TYPE jsonb USING "{column}"::jsonb'
        )


def downgrade() -> None:
    for column in _JSONB_COLUMNS:
        op.execute(
            f'ALTER TABLE "memory_records" '
            f'ALTER COLUMN "{column}" TYPE json USING "{column}"::json'
        )
