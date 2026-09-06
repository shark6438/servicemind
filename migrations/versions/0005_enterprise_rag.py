"""Create RLS-protected enterprise RAG authority tables."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_enterprise_rag"
down_revision = "0004_action_intent_v2"
branch_labels = None
depends_on = None
UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "knowledge_documents",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("source_record_id", sa.String(500), nullable=False),
        sa.Column("source_version", sa.String(200), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("document_type", sa.String(100), nullable=False),
        sa.Column("language", sa.String(20), nullable=False),
        sa.Column("document_metadata", sa.JSON(), nullable=False),
        sa.Column("acl", sa.JSON(), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("tenant_id", "source", "source_record_id"),
    )
    op.create_table(
        "knowledge_parent_chunks",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "document_id",
            UUID,
            sa.ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("section_path", sa.JSON(), nullable=False),
        sa.Column("block_ids", sa.JSON(), nullable=False),
        sa.Column("chunk_order", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
    )
    op.create_table(
        "knowledge_child_chunks",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "document_id",
            UUID,
            sa.ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "parent_chunk_id",
            UUID,
            sa.ForeignKey("knowledge_parent_chunks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("section_path", sa.JSON(), nullable=False),
        sa.Column("chunk_order", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
    )
    op.create_table(
        "knowledge_ingestion_jobs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(100)),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    for table in (
        "knowledge_documents",
        "knowledge_parent_chunks",
        "knowledge_child_chunks",
        "knowledge_ingestion_jobs",
    ):
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        expr = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
        op.execute(
            f'CREATE POLICY "{table}_tenant_isolation" ON "{table}" USING ({expr}) WITH CHECK ({expr})'
        )


def downgrade() -> None:
    for table in (
        "knowledge_ingestion_jobs",
        "knowledge_child_chunks",
        "knowledge_parent_chunks",
        "knowledge_documents",
    ):
        op.drop_table(table)
