"""Track PostgreSQL to OpenSearch projection state for idempotent RAG ingestion."""

import sqlalchemy as sa
from alembic import op

revision = "0006_rag_index_state"
down_revision = "0005_enterprise_rag"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowledge_documents",
        sa.Column(
            "index_status",
            sa.String(length=40),
            server_default="pending_index",
            nullable=False,
        ),
    )
    op.create_index(
        "ix_knowledge_documents_index_status",
        "knowledge_documents",
        ["tenant_id", "index_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_documents_index_status", table_name="knowledge_documents")
    op.drop_column("knowledge_documents", "index_status")
