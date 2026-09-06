from uuid import UUID

from sqlalchemy import delete, select

from servicemind.domain.knowledge import ChildChunk, KnowledgeDocument, ParentChunk
from servicemind.persistence.database import tenant_session
from servicemind.persistence.models import (
    KnowledgeChildChunkRecord,
    KnowledgeDocumentRecord,
    KnowledgeParentChunkRecord,
)


class KnowledgeRepository:
    """RLS-protected authority for parsed documents and parent/child chunks."""

    async def is_current(self, tenant_id: UUID, document: KnowledgeDocument) -> bool:
        async with tenant_session(tenant_id) as session:
            value = (
                await session.execute(
                    select(KnowledgeDocumentRecord.id).where(
                        KnowledgeDocumentRecord.source == document.provenance.source,
                        KnowledgeDocumentRecord.source_record_id
                        == document.provenance.source_record_id,
                        KnowledgeDocumentRecord.source_version
                        == document.provenance.source_version,
                        KnowledgeDocumentRecord.content_hash == document.provenance.content_hash,
                        KnowledgeDocumentRecord.index_status == "indexed",
                    )
                )
            ).scalar_one_or_none()
            return value is not None

    async def replace(
        self,
        tenant_id: UUID,
        document: KnowledgeDocument,
        parents: list[ParentChunk],
        children: list[ChildChunk],
    ) -> None:
        async with tenant_session(tenant_id) as session:
            existing = (
                await session.execute(
                    select(KnowledgeDocumentRecord.id).where(
                        KnowledgeDocumentRecord.source == document.provenance.source,
                        KnowledgeDocumentRecord.source_record_id
                        == document.provenance.source_record_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                await session.execute(
                    delete(KnowledgeDocumentRecord).where(KnowledgeDocumentRecord.id == existing)
                )
                await session.flush()
            session.add(
                KnowledgeDocumentRecord(
                    id=document.document_id,
                    tenant_id=tenant_id,
                    source=document.provenance.source,
                    source_record_id=document.provenance.source_record_id,
                    source_version=document.provenance.source_version,
                    title=document.title,
                    content=document.content,
                    content_hash=document.provenance.content_hash,
                    document_type=document.document_type,
                    language=document.language,
                    document_metadata=document.metadata,
                    acl=document.acl.model_dump(mode="json"),
                    provenance=document.provenance.model_dump(mode="json"),
                    index_status="pending_index",
                )
            )
            await session.flush()
            session.add_all(
                [
                    KnowledgeParentChunkRecord(
                        id=item.parent_chunk_id,
                        tenant_id=tenant_id,
                        document_id=item.document_id,
                        section_path=item.section_path,
                        block_ids=[str(value) for value in item.block_ids],
                        chunk_order=item.order,
                        content=item.content,
                        token_count=item.token_count,
                    )
                    for item in parents
                ]
            )
            await session.flush()
            session.add_all(
                [
                    KnowledgeChildChunkRecord(
                        id=item.child_chunk_id,
                        tenant_id=tenant_id,
                        document_id=item.document_id,
                        parent_chunk_id=item.parent_chunk_id,
                        section_path=item.section_path,
                        chunk_order=item.order,
                        content=item.content,
                        content_hash=item.content_hash,
                        token_count=item.token_count,
                    )
                    for item in children
                ]
            )

    async def mark_indexed(self, tenant_id: UUID, document_id: UUID) -> None:
        async with tenant_session(tenant_id) as session:
            row = (
                await session.execute(
                    select(KnowledgeDocumentRecord).where(KnowledgeDocumentRecord.id == document_id)
                )
            ).scalar_one()
            row.index_status = "indexed"

    async def reconcile_indexed(self, tenant_id: UUID, document_ids: set[UUID]) -> int:
        if not document_ids:
            return 0
        count = 0
        async with tenant_session(tenant_id) as session:
            rows = (
                await session.execute(
                    select(KnowledgeDocumentRecord).where(
                        KnowledgeDocumentRecord.id.in_(document_ids),
                        KnowledgeDocumentRecord.index_status == "pending_index",
                    )
                )
            ).scalars()
            for row in rows:
                row.index_status = "indexed"
                count += 1
        return count

    async def parents(self, tenant_id: UUID, ids: list[UUID]) -> dict[UUID, str]:
        if not ids:
            return {}
        async with tenant_session(tenant_id) as session:
            rows = (
                await session.execute(
                    select(KnowledgeParentChunkRecord).where(KnowledgeParentChunkRecord.id.in_(ids))
                )
            ).scalars()
            return {row.id: row.content for row in rows}


knowledge_repository = KnowledgeRepository()
