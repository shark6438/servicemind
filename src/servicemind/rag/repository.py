from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult

from servicemind.domain.knowledge import ChildChunk, KnowledgeDocument, ParentChunk
from servicemind.persistence.database import tenant_session
from servicemind.persistence.models import (
    IngestionJobStatus,
    KnowledgeChildChunkRecord,
    KnowledgeDocumentRecord,
    KnowledgeIngestionJob,
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
    ) -> tuple[KnowledgeDocument, list[ParentChunk], list[ChildChunk]]:
        """Atomically replace a source document and its chunks inside one RLS transaction.

        Uses ``INSERT ... ON CONFLICT DO UPDATE`` keyed on
        ``(tenant_id, source, source_record_id)`` so a re-ingestion never destroys the
        previously committed row (delete-before-insert race). The persisted ``document_id``
        is kept stable across versions so PostgreSQL and OpenSearch projection always agree.
        Returns normalized copies that callers MUST feed to the search index afterwards.
        """
        async with tenant_session(tenant_id) as session:
            statement = pg_insert(KnowledgeDocumentRecord).values(
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
            conflict_target = [
                KnowledgeDocumentRecord.tenant_id,
                KnowledgeDocumentRecord.source,
                KnowledgeDocumentRecord.source_record_id,
            ]
            upsert = statement.on_conflict_do_update(
                index_elements=conflict_target,
                set_={
                    "id": KnowledgeDocumentRecord.id,
                    "source_version": statement.excluded.source_version,
                    "title": statement.excluded.title,
                    "content": statement.excluded.content,
                    "content_hash": statement.excluded.content_hash,
                    "document_type": statement.excluded.document_type,
                    "language": statement.excluded.language,
                    "document_metadata": statement.excluded.document_metadata,
                    "acl": statement.excluded.acl,
                    "provenance": statement.excluded.provenance,
                    "index_status": "pending_index",
                },
            ).returning(KnowledgeDocumentRecord.id)
            canonical_id = (await session.execute(upsert)).scalar_one()

            # Chunks are version-owned children of the document; clear the previous
            # generation (child FK cascades to parent via ondelete=CASCADE is not
            # guaranteed across both hops on every driver, so delete both explicitly).
            await session.execute(
                delete(KnowledgeChildChunkRecord).where(
                    KnowledgeChildChunkRecord.document_id == canonical_id
                )
            )
            await session.execute(
                delete(KnowledgeParentChunkRecord).where(
                    KnowledgeParentChunkRecord.document_id == canonical_id
                )
            )
            await session.flush()

            document = document.model_copy(update={"document_id": canonical_id})
            parents = [
                parent.model_copy(update={"document_id": canonical_id}) for parent in parents
            ]
            children = [
                child.model_copy(update={"document_id": canonical_id}) for child in children
            ]
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
            return document, parents, children

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

    async def request_regeneration(self, tenant_id: UUID) -> int:
        """Mark every indexed document pending so a batch re-embeds the whole corpus.

        Called once when the active index generation no longer matches the configured
        embedding (model/revision change). Vectors of the old revision are not
        reusable -- §5 forbids mixing vector spaces -- so previously ``indexed``
        documents must flow through ``is_current`` == False and be re-ingested into
        the new generation before the alias is flipped by ``publish``.
        """
        async with tenant_session(tenant_id) as session:
            result = await session.execute(
                update(KnowledgeDocumentRecord)
                .where(KnowledgeDocumentRecord.index_status == "indexed")
                .values(index_status="pending_index")
            )
        return int(cast(CursorResult[Any], result).rowcount or 0)

    async def count_pending(self, tenant_id: UUID) -> int:
        """Rows still waiting on (re-)indexing -- drives the generation publish gate."""
        async with tenant_session(tenant_id) as session:
            return int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(KnowledgeDocumentRecord)
                        .where(KnowledgeDocumentRecord.index_status == "pending_index")
                    )
                ).scalar_one()
            )

    async def source_record_ids(self, tenant_id: UUID) -> set[str]:
        """Every source record id PostgreSQL knows about for this tenant.

        Reconciliation compares this set against the ids present in the active search
        generation; index rows with no backing row are orphaned and get pruned.
        """
        async with tenant_session(tenant_id) as session:
            rows = (
                await session.execute(select(KnowledgeDocumentRecord.source_record_id))
            ).scalars()
            return set(rows)

    async def delete_missing(self, tenant_id: UUID, retained_source_record_ids: set[str]) -> int:
        """Delete every document this tenant has that a run did NOT supply.

        Used by ``authoritative`` ingestion: the batch is the complete intended
        corpus, so anything absent was retracted at the source and must not linger in
        PostgreSQL (its index rows are pruned by reconciliation afterwards). Without
        this, orphaned ``pending_index`` rows would block the generation publish gate.
        """
        async with tenant_session(tenant_id) as session:
            statement = select(KnowledgeDocumentRecord.id)
            if retained_source_record_ids:
                statement = statement.where(
                    KnowledgeDocumentRecord.source_record_id.not_in(retained_source_record_ids)
                )
            ids = (await session.execute(statement)).scalars().all()
            for chunk in (ids[index : index + 500] for index in range(0, len(ids), 500)):
                await session.execute(
                    delete(KnowledgeChildChunkRecord).where(
                        KnowledgeChildChunkRecord.document_id.in_(chunk)
                    )
                )
                await session.execute(
                    delete(KnowledgeParentChunkRecord).where(
                        KnowledgeParentChunkRecord.document_id.in_(chunk)
                    )
                )
                await session.execute(
                    delete(KnowledgeDocumentRecord).where(KnowledgeDocumentRecord.id.in_(chunk))
                )
            return len(ids)

    async def delete_documents(self, tenant_id: UUID, source_record_ids: set[str]) -> int:
        """Unpublish: delete the PostgreSQL authority rows (chunks cascade).

        The repository is the source of truth, so a removal is physical here. The
        caller must delete the matching rows from the search generation afterwards;
        ``EnterpriseRAG.unpublish`` sequences both. A later re-ingest of the same
        source record simply recreates the rows (idempotent).
        """
        if not source_record_ids:
            return 0
        async with tenant_session(tenant_id) as session:
            doc_ids = (
                await session.execute(
                    select(KnowledgeDocumentRecord.id).where(
                        KnowledgeDocumentRecord.source_record_id.in_(source_record_ids)
                    )
                )
            ).scalars()
            ids = [value for value in doc_ids]
            if not ids:
                return 0
            await session.execute(
                delete(KnowledgeChildChunkRecord).where(
                    KnowledgeChildChunkRecord.document_id.in_(ids)
                )
            )
            await session.execute(
                delete(KnowledgeParentChunkRecord).where(
                    KnowledgeParentChunkRecord.document_id.in_(ids)
                )
            )
            result = await session.execute(
                delete(KnowledgeDocumentRecord).where(KnowledgeDocumentRecord.id.in_(ids))
            )
        return int(cast(CursorResult[Any], result).rowcount or 0)

    async def set_document_active(
        self, tenant_id: UUID, source_record_id: str, *, is_active: bool
    ) -> None:
        """Flip ``is_active`` inside the document ACL (PostgreSQL authority).

        Search rows carry a redundant copy of the flag for pre-filtering; the caller
        (``EnterpriseRAG.set_document_active``) keeps the OpenSearch projection in
        sync, and every later re-ingest re-derives the flag from this ACL anyway.
        """
        async with tenant_session(tenant_id) as session:
            rows = (
                await session.execute(
                    select(KnowledgeDocumentRecord).where(
                        KnowledgeDocumentRecord.source_record_id == source_record_id
                    )
                )
            ).scalars()
            for row in rows:
                acl = dict(row.acl)
                acl["is_active"] = is_active
                row.acl = acl

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

    # -- Ingestion job register -------------------------------------------------

    async def begin_ingestion_job(self, tenant_id: UUID, source: str) -> int:
        """Open (or reopen) the live job row for a source; returns the attempt number.

        The register is keyed on ``(tenant_id, source)``: a fresh source inserts at
        ``attempts = 1``, a retried one advances its existing row (``attempts + 1``)
        so an operator sees how many times a failing source has been tried. Metrics
        and the previous error are cleared -- this run starts clean. RLS keeps the
        upsert scoped to the tenant's own row.
        """
        running = IngestionJobStatus.RUNNING.value
        statement = (
            pg_insert(KnowledgeIngestionJob)
            .values(
                id=uuid4(),
                tenant_id=tenant_id,
                source=source,
                status=running,
                attempts=1,
                error_code=None,
                metrics={},
            )
            .on_conflict_do_update(
                index_elements=[
                    KnowledgeIngestionJob.tenant_id,
                    KnowledgeIngestionJob.source,
                ],
                set_={
                    "status": running,
                    "attempts": KnowledgeIngestionJob.attempts + 1,
                    "error_code": None,
                    "metrics": {},
                    "updated_at": func.now(),
                },
            )
            .returning(KnowledgeIngestionJob.attempts)
        )
        async with tenant_session(tenant_id) as session:
            return int((await session.execute(statement)).scalar_one())

    async def complete_ingestion_job(
        self,
        tenant_id: UUID,
        source: str,
        *,
        status: str,
        error_code: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        """Close a source's job row as ``succeeded`` or ``failed`` (idempotent upsert).

        ``attempts`` was fixed by ``begin_ingestion_job`` and is left untouched so a
        ``failed`` run followed by a retry shows ``attempts=2`` on the next begin.
        Re-running ``complete`` after a crash (begin opened, process died) inserts a
        self-healed row with ``attempts=1`` rather than erroring, so observability
        never silently loses a source that only *started*.
        """
        values = {
            "id": uuid4(),
            "tenant_id": tenant_id,
            "source": source,
            "status": status,
            "attempts": 1,
            "error_code": error_code,
            "metrics": metrics or {},
        }
        statement = pg_insert(KnowledgeIngestionJob).values(**values)
        upsert = statement.on_conflict_do_update(
            index_elements=[
                KnowledgeIngestionJob.tenant_id,
                KnowledgeIngestionJob.source,
            ],
            set_={
                "status": statement.excluded.status,
                "error_code": statement.excluded.error_code,
                "metrics": statement.excluded.metrics,
                "updated_at": func.now(),
            },
        )
        async with tenant_session(tenant_id) as session:
            await session.execute(upsert)

    async def list_ingestion_jobs(
        self, tenant_id: UUID, *, source: str | None = None
    ) -> list[dict[str, Any]]:
        """Latest per-source job state, newest first (for status dashboards/scripts)."""
        async with tenant_session(tenant_id) as session:
            statement = select(KnowledgeIngestionJob).order_by(
                KnowledgeIngestionJob.updated_at.desc()
            )
            if source is not None:
                statement = statement.where(KnowledgeIngestionJob.source == source)
            rows = (await session.execute(statement)).scalars()
            return [
                {
                    "source": row.source,
                    "status": row.status,
                    "attempts": row.attempts,
                    "error_code": row.error_code,
                    "metrics": row.metrics,
                    "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                }
                for row in rows
            ]


knowledge_repository = KnowledgeRepository()
