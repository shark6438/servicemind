"""Close the model-vs-DB drift on the enterprise RAG authority tables.

Migration 0005 created only ``ix_{table}_tenant_id`` per table while the ORM
declared further column indexes; ``metadata.create_all`` and the live schema
therefore disagreed. This adds the missing single-column indexes (all serve a
lookup the repository actually issues) and keys ``knowledge_ingestion_jobs`` on
``(tenant_id, source)`` so it behaves as a per-source status register rather than
an unbounded log. All four tables are empty at authoring time, so adding a unique
constraint is a no-op on data.

``ix_knowledge_documents_index_status`` is intentionally NOT rebuilt: 0006 already
created it as the composite ``(tenant_id, index_status)`` that the ORM now declares
in ``__table_args__`` (the publish gate queries inside the RLS tenant window).
"""

from alembic import op

revision = "0007_rag_index_fixes"
down_revision = "0006_rag_index_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_knowledge_documents_content_hash",
        "knowledge_documents",
        ["content_hash"],
    )
    op.create_index(
        "ix_knowledge_parent_chunks_document_id",
        "knowledge_parent_chunks",
        ["document_id"],
    )
    op.create_index(
        "ix_knowledge_child_chunks_document_id",
        "knowledge_child_chunks",
        ["document_id"],
    )
    op.create_index(
        "ix_knowledge_child_chunks_parent_chunk_id",
        "knowledge_child_chunks",
        ["parent_chunk_id"],
    )
    op.create_index(
        "ix_knowledge_ingestion_jobs_status",
        "knowledge_ingestion_jobs",
        ["status"],
    )
    op.create_unique_constraint(
        "uq_knowledge_ingestion_jobs_tenant_source",
        "knowledge_ingestion_jobs",
        ["tenant_id", "source"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_knowledge_ingestion_jobs_tenant_source",
        "knowledge_ingestion_jobs",
        type_="unique",
    )
    op.drop_index("ix_knowledge_ingestion_jobs_status", table_name="knowledge_ingestion_jobs")
    op.drop_index("ix_knowledge_child_chunks_parent_chunk_id", table_name="knowledge_child_chunks")
    op.drop_index("ix_knowledge_child_chunks_document_id", table_name="knowledge_child_chunks")
    op.drop_index("ix_knowledge_parent_chunks_document_id", table_name="knowledge_parent_chunks")
    op.drop_index("ix_knowledge_documents_content_hash", table_name="knowledge_documents")
