from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import Counter
from pathlib import Path
from uuid import UUID

from opensearchpy import AsyncOpenSearch

from core import settings
from servicemind.domain.evidence import EVIDENCE_CONTENT_MAX, Evidence, EvidenceSourceType
from servicemind.domain.knowledge import (
    Citation,
    ContextItem,
    KnowledgeRAGResult,
    RetrievalMode,
    RetrievalPrincipal,
)
from servicemind.graphrag.retrieval import GraphRetriever, to_graph_evidence
from servicemind.graphrag.store import GraphStore
from servicemind.rag.chunking import semantic_chunker
from servicemind.rag.models import (
    BgeM3EmbeddingProvider,
    BgeM3Reranker,
    EmbeddingProvider,
    Reranker,
    TeiEmbeddingProvider,
    TeiReranker,
)
from servicemind.rag.opensearch import OpenSearchKnowledgeIndex
from servicemind.rag.parsing import structure_parser
from servicemind.rag.query import query_processor
from servicemind.rag.repository import KnowledgeRepository, knowledge_repository

logger = logging.getLogger("servicemind.rag.service")


def _error_code(error: BaseException) -> str:
    """Coarse, stable classification for the ingestion register (String(100)).

    Uses the exception the pipeline actually raised (not a chain hop) so operators
    can group failures by code; the full traceback stays in the application logs.
    """
    return type(error).__name__


def build_opensearch_client() -> AsyncOpenSearch:
    password = settings.SERVICEMIND_OPENSEARCH_PASSWORD or settings.POSTGRES_PASSWORD
    auth = None
    if password:
        auth = (settings.SERVICEMIND_OPENSEARCH_USERNAME, password.get_secret_value())
    return AsyncOpenSearch(
        hosts=[settings.SERVICEMIND_OPENSEARCH_URL],
        http_auth=auth,
        verify_certs=settings.SERVICEMIND_OPENSEARCH_VERIFY_CERTS,
        ssl_show_warn=False,
        timeout=30,
        max_retries=2,
        retry_on_timeout=True,
    )


def _retrieval_mode_label(mode: RetrievalMode, run_rerank: bool, use_rewrites: bool = False) -> str:
    """Machine-readable label describing exactly which pipeline produced the items.

    Keeps the legacy default value (``dense_bm25_rrf_cross_encoder_parent``)
    byte-for-byte so previously persisted Evidence metadata stays stable, while
    evaluation baselines get an honest, distinct label for each mode/rerank combo.
    Multi-query fan-out (``use_rewrites``, hybrid only) is surfaced as an ``_mq``
    marker in the base so a consumer can tell it apart from single-query RRF.
    """
    if mode is RetrievalMode.HYBRID:
        base = "dense_bm25_mq_rrf" if use_rewrites else "dense_bm25_rrf"
    else:
        base = mode.value
    middle = "cross_encoder" if run_rerank else "no_rerank"
    return f"{base}_{middle}_parent"


_EVIDENCE_TRUNCATION_SUFFIX = "\n…[parent truncated at evidence content ceiling]"


def _bounded_evidence_content(content: str) -> str:
    """Serializable Evidence content: never over ``EVIDENCE_CONTENT_MAX`` chars.

    The chunker keeps freshly indexed parents under the ceiling; this is the defensive
    backstop for any oversized row that predates that guarantee. Bounding is explicit
    (an honest marker is appended) and the citation still anchors the matching child,
    which is indexed separately and unaffected by the parent preview bound.
    """
    if len(content) <= EVIDENCE_CONTENT_MAX:
        return content
    head = content[: EVIDENCE_CONTENT_MAX - len(_EVIDENCE_TRUNCATION_SUFFIX)]
    return head + _EVIDENCE_TRUNCATION_SUFFIX


class EnterpriseRAG:
    def __init__(
        self,
        *,
        index: OpenSearchKnowledgeIndex,
        embedding: EmbeddingProvider,
        reranker: Reranker,
        repository: KnowledgeRepository | None = None,
        graph_store: GraphStore | None = None,
    ) -> None:
        self.index, self.embedding, self.reranker = index, embedding, reranker
        self.repository = repository
        self.graph_store = graph_store
        #: Tenants whose corpus was reset for a generation migration this process.
        self._regeneration_requested: set[UUID] = set()

    async def reconcile(self, tenant_id: UUID) -> dict[str, int]:
        """Two-way drift repair between PostgreSQL and the active search generation.

        ``marked``: pending documents that ARE present in the active generation are
        confirmed indexed (they were fully written). ``pruned``: index rows whose
        source record no longer exists in PostgreSQL are dropped, so corpus
        retractions (source delete / unpublish) propagate to search. PostgreSQL is
        the authority in both directions.
        """
        if not self.repository:
            return {"marked": 0, "pruned": 0}
        desired = self.index.generation(self.embedding)
        active = await self.index.active_generation(tenant_id)
        indexed = await self.index.indexed_document_ids(tenant_id)
        marked = (
            await self.repository.reconcile_indexed(tenant_id, indexed) if active == desired else 0
        )
        present = await self.repository.source_record_ids(tenant_id)
        pruned = await self.index.prune_to(tenant_id, present)
        return {"marked": marked, "pruned": pruned}

    async def ingest(
        self,
        tenant_id: UUID,
        documents,
        *,
        concurrency: int = 2,
        authoritative: bool = False,
    ) -> dict[str, int]:
        """Index a batch of documents into the embedding's concrete generation.

        Model upgrades: ``replace_document`` always writes vectors into the generation
        that matches the *current* embedding identity, never an older one (vector
        spaces must not mix). The first batch seen after an identity change resets the
        whole corpus to ``pending_index`` (``request_regeneration``) so unchanged
        documents are re-embedded too; the active alias only flips to the new
        generation once no row is left pending (``publish``), which keeps readers on
        the previous generation until the migration is complete. Run model upgrades
        as a maintenance/offline window, and pass the full corpus when
        ``authoritative=True`` (docs no longer supplied by any source are deleted
        from PostgreSQL + the search generation instead of blocking the flip).
        """
        semaphore = asyncio.Semaphore(concurrency)
        if (
            self.repository
            and tenant_id not in self._regeneration_requested
            and getattr(self.index, "active_generation", None)
            and getattr(self.index, "generation", None)
        ):
            desired = self.index.generation(self.embedding)
            active = await self.index.active_generation(tenant_id)
            if active is not None and active != desired:
                await self.repository.request_regeneration(tenant_id)
                self._regeneration_requested.add(tenant_id)

        batch_ids: set[str] | None = None
        if authoritative and self.repository:
            batch_ids = {document.provenance.source_record_id for document in documents}

        # The ingestion register keeps one live job row per source (status, attempt
        # count, coarse error, metrics). Thin repository substitutes in smoke/eval
        # harnesses implement only the authority read/write surface and opt out; the
        # production KnowledgeRepository always records.
        job_repository = (
            self.repository
            if self.repository and hasattr(self.repository, "begin_ingestion_job")
            else None
        )
        by_source: dict[str, list] = {}
        for document in documents:
            by_source.setdefault(document.provenance.source, []).append(document)

        async def ingest_one(document):
            async with semaphore:
                if self.repository and await self.repository.is_current(tenant_id, document):
                    return 0, 0, True
                # A raw file attachment (PDF/DOCX/...) is routed by suffix to the
                # docling-aware parser; everything else keeps the legacy content-prefix
                # sniff. Without the hint, real PDFs would be misparsed as markdown.
                source_file = document.metadata.get("source_file")
                blocks = (
                    structure_parser.parse_file(Path(source_file), document)
                    if source_file
                    else (
                        structure_parser.parse_html(document)
                        if document.content.lstrip().startswith("<")
                        else structure_parser.parse_markdown(document)
                    )
                )
                parents = semantic_chunker.build_parents(blocks)
                children = await semantic_chunker.build_children(document, parents, self.embedding)
                if self.repository:
                    # Repository returns normalized copies whose document_id is the stable
                    # canonical PK persisted by UPSERT; the search index must project the
                    # same ids or parent expansion / reconcile will disagree with PG.
                    document, parents, children = await self.repository.replace(
                        tenant_id, document, parents, children
                    )
                await self.index.replace_document(
                    tenant_id, document, parents, children, self.embedding
                )
                if self.repository:
                    await self.repository.mark_indexed(tenant_id, document.document_id)
                return len(parents), len(children), False

        async def ingest_source(source: str, docs: list) -> dict[str, int]:
            """Ingest one source's documents and close its job row -- never swallow.

            ``return_exceptions=True`` lets every document of the source be attempted
            (a single bad doc does not starve its siblings), then the job is closed
            as ``succeeded`` or ``failed`` with the partial metrics, and the first
            error is re-raised so the caller and the publish gate never observe a
            partial success as a full one.
            """
            if job_repository is not None:
                await job_repository.begin_ingestion_job(tenant_id, source)
            outcomes = await asyncio.gather(
                *(ingest_one(document) for document in docs), return_exceptions=True
            )
            errors = [value for value in outcomes if isinstance(value, BaseException)]
            done = [value for value in outcomes if not isinstance(value, BaseException)]
            source_totals = {
                "documents": len(done),
                "parents": sum(parent_count for parent_count, _, _ in done),
                "children": sum(child_count for _, child_count, _ in done),
                "skipped": sum(1 for *_, skipped in done if skipped),
            }
            if job_repository is not None:
                if errors:
                    await job_repository.complete_ingestion_job(
                        tenant_id,
                        source,
                        status="failed",
                        error_code=_error_code(errors[0]),
                        metrics=source_totals,
                    )
                else:
                    await job_repository.complete_ingestion_job(
                        tenant_id,
                        source,
                        status="succeeded",
                        metrics=source_totals,
                    )
            if errors:
                raise errors[0]
            return source_totals

        totals = {"documents": 0, "parents": 0, "children": 0, "skipped": 0}
        for source, docs in by_source.items():
            source_totals = await ingest_source(source, docs)
            for key in totals:
                totals[key] += source_totals[key]

        if authoritative and self.repository and batch_ids is not None:
            # Documents this source set no longer supplies were retracted. They are
            # pending (never re-indexed), so the publish gate below would deadlock;
            # delete their authority rows now -- unpublish.
            await self.repository.delete_missing(tenant_id, batch_ids)

        # Flip the alias only when no row is left pending: PostgreSQL is the source
        # of truth for "is the desired generation fully written?". Without a
        # repository (smoke/infra harnesses) flip eagerly once the batch landed.
        if getattr(self.index, "publish", None):
            pending = (
                0 if self.repository is None else await self.repository.count_pending(tenant_id)
            )
            if pending == 0:
                await self.index.publish(tenant_id, self.embedding)
        await self.index.refresh(tenant_id)
        return totals

    async def set_document_active(
        self, tenant_id: UUID, source_record_id: str, *, is_active: bool
    ) -> None:
        """Suspend/activate a document. PostgreSQL authority first, then the search
        projection, so the pre-filter stops (or resumes) serving immediately while
        future re-ingests keep re-deriving the flag from the ACL."""
        if self.repository:
            await self.repository.set_document_active(
                tenant_id, source_record_id, is_active=is_active
            )
        if getattr(self.index, "set_document_active", None):
            await self.index.set_document_active(tenant_id, source_record_id, is_active=is_active)

    async def unpublish(self, tenant_id: UUID, source_record_ids: list[str]) -> dict[str, int]:
        """Remove documents entirely (delete propagation). The repository rows go
        first; the active search generation is pruned to match."""
        ids = set(source_record_ids)
        postgres = await self.repository.delete_documents(tenant_id, ids) if self.repository else 0
        removed = (
            await self.index.delete_documents(tenant_id, ids)
            if getattr(self.index, "delete_documents", None)
            else 0
        )
        return {"postgres": postgres, "index": removed}

    async def retrieve(
        self,
        *,
        principal: RetrievalPrincipal,
        query: str,
        model_query: str | None = None,
        use_query_model: bool = True,
        final_k: int = 8,
        mode: RetrievalMode = RetrievalMode.HYBRID,
        run_rerank: bool = True,
        use_rewrites: bool = False,
        dense_k: int | None = None,
        bm25_k: int | None = None,
        candidate_k: int | None = None,
    ) -> KnowledgeRAGResult:
        """End-to-end retrieval: query process -> candidate generation (``mode``) ->
        optional cross-encoder rerank -> parent expansion + context packing.

        ``mode`` and ``run_rerank`` let the evaluation harness isolate each stage's
        contribution (dense-only / bm25-only / hybrid / hybrid + rerank). They never
        alter ACL enforcement, which stays a pre-filter inside the search index.
        ``use_rewrites`` (hybrid only) enables multi-query fan-out over the processed
        query's ``rewritten_queries`` (each rewrite runs as its own lexical arm under
        the single dense anchor -- see ``OpenSearchKnowledgeIndex.search``); the
        rerank step still scores against the normalized query (the cross-encoder is
        the faithfulness anchor, independent of how many candidate arms ran).
        ``dense_k`` / ``bm25_k`` / ``candidate_k`` size the RRF funnel that feeds the
        reranker and default to ``SERVICEMIND_RAG_{DENSE_K,BM25_K,CANDIDATE_K}``;
        raising ``candidate_k`` raises the recall ceiling at rerank cost.
        """
        started = time.perf_counter()
        if not 1 <= final_k <= 50:
            raise ValueError("final_k must be between 1 and 50")
        processed = await query_processor.process(
            query,
            use_model=use_query_model,
            model_query=model_query,
        )
        hits = await self.index.search(
            processed,
            principal,
            self.embedding,
            mode=mode,
            use_rewrites=use_rewrites,
            dense_k=(
                dense_k
                if dense_k is not None
                else settings.SERVICEMIND_RAG_DENSE_K
            ),
            bm25_k=bm25_k if bm25_k is not None else settings.SERVICEMIND_RAG_BM25_K,
            candidate_k=(
                candidate_k
                if candidate_k is not None
                else settings.SERVICEMIND_RAG_CANDIDATE_K
            ),
        )
        candidate_count = len(hits)
        if hits and self.repository is None:
            raise RuntimeError("KnowledgeRepository missing; refusing OpenSearch-only parent expansion")
        hits = [hit for hit in hits if principal.allows(hit.acl)]
        if hits:
            if self.repository is None:
                raise RuntimeError(
                    "KnowledgeRepository is required for RLS-protected parent expansion"
                )
            if hasattr(self.repository, "authorized_parents"):
                parents = await self.repository.authorized_parents(principal, hits)
            else:
                parents = await self.repository.parents(
                    principal.tenant_id, [hit.parent_chunk_id for hit in hits]
                )
            hits = [hit for hit in hits if hit.parent_chunk_id in parents]
        else:
            parents = {}
        if run_rerank and hits:
            scores = await self.reranker.score(
                processed.normalized_query, [hit.child_content for hit in hits]
            )
            if len(scores) != len(hits) or any(
                not math.isfinite(score) or not 0 <= score <= 1 for score in scores
            ):
                raise ValueError("reranker must return one normalized finite score per hit")
            hits = [
                hit.model_copy(update={"rerank_score": score})
                for hit, score in zip(hits, scores, strict=True)
            ]
        ranked = sorted(
            hits,
            key=lambda hit: hit.rerank_score if hit.rerank_score is not None else hit.score,
            reverse=True,
        )

        # Deduplicate to one parent per ... , then apply diversity ceilings so a single
        # document or source cannot crowd the context (Phase 4 baseline §7). Selection
        # stays relevance-ordered; caps only skip, never pad with irrelevant content.
        per_document_cap = settings.SERVICEMIND_RAG_MAX_PARENTS_PER_DOCUMENT
        per_source_cap = settings.SERVICEMIND_RAG_MAX_PARENTS_PER_SOURCE
        seen_parents: set[UUID] = set()
        per_document: Counter[UUID] = Counter()
        per_source: Counter[str] = Counter()
        unique: list = []
        budget = settings.SERVICEMIND_RAG_CONTEXT_TOKENS
        used = 0
        for hit in ranked:
            content = parents.get(hit.parent_chunk_id)
            if not content:
                continue
            count = semantic_chunker.tokens(content)
            if used + count > budget:
                continue
            if hit.parent_chunk_id in seen_parents:
                continue
            if per_document[hit.document_id] >= per_document_cap:
                continue
            if per_source[hit.source] >= per_source_cap:
                continue
            seen_parents.add(hit.parent_chunk_id)
            per_document[hit.document_id] += 1
            per_source[hit.source] += 1
            unique.append(hit)
            used += count
            if len(unique) >= final_k:
                break

        items: list[ContextItem] = []
        budget = settings.SERVICEMIND_RAG_CONTEXT_TOKENS
        used = 0
        for hit in unique:
            content = parents.get(hit.parent_chunk_id)
            if not content:
                continue
            count = semantic_chunker.tokens(content)
            if used + count > budget:
                continue
            if hit.acl.tenant_id not in {None, principal.tenant_id}:
                raise PermissionError("RAG defense-in-depth tenant check failed")
            items.append(
                ContextItem(
                    parent_content=content,
                    hit=hit,
                    citation=Citation.from_hit(hit),
                    token_count=count,
                )
            )
            used += count
        return KnowledgeRAGResult(
            query=processed,
            items=items,
            retrieval_mode=_retrieval_mode_label(mode, run_rerank, use_rewrites),
            candidate_count=candidate_count,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    _EVIDENCE_TRUNCATION_SUFFIX = "\n…[parent truncated at evidence content ceiling]"

    def to_evidence(self, tenant_id: UUID, result: KnowledgeRAGResult) -> list[Evidence]:
        return [
            Evidence.create(
                tenant_id=tenant_id,
                source_type=EvidenceSourceType.KNOWLEDGE,
                source_ref=item.citation.source_uri,
                resource_type="knowledge_parent_chunk",
                resource_id=str(item.citation.parent_chunk_id),
                content=_bounded_evidence_content(item.parent_content),
                provider=item.hit.source,
                retrieval_method=result.retrieval_mode,
                confidence=(
                    max(0.0, min(1.0, item.hit.rerank_score))
                    if item.hit.rerank_score is not None
                    else None
                ),
                metadata={
                    "citation": item.citation.model_dump(mode="json"),
                    "authority_level": int(item.hit.authority_level),
                    "license": item.hit.license,
                    "synthetic": item.hit.synthetic,
                    "query": result.query.model_dump(mode="json"),
                },
            )
            for item in result.items
        ]

    async def graph_evidence(
        self, principal: RetrievalPrincipal, result: KnowledgeRAGResult
    ) -> list[Evidence]:
        """Structural side channel over the same processed query (Phase 4 baseline 4.2).

        Runs only when a GraphStore was wired in; text retrieval is never blocked on it.
        Any store/query failure is logged and degrades to no graph findings, because the
        graph is advisory context, not the knowledge authority.
        """
        if self.graph_store is None:
            return []
        try:
            findings = await GraphRetriever().retrieve(result.query, principal, self.graph_store)
        except Exception:
            logger.exception("Graph-RAG side channel failed; text retrieval stands alone.")
            return []
        return to_graph_evidence(principal.tenant_id, findings, result.query)


def build_enterprise_rag() -> EnterpriseRAG:
    embedding = (
        TeiEmbeddingProvider(
            settings.SERVICEMIND_EMBEDDING_URL,
            model_revision=settings.SERVICEMIND_EMBEDDING_REVISION,
        )
        if settings.SERVICEMIND_EMBEDDING_URL
        else BgeM3EmbeddingProvider(
            model_revision=settings.SERVICEMIND_EMBEDDING_REVISION,
            device=settings.SERVICEMIND_MODEL_DEVICE,
            cache_folder=settings.SERVICEMIND_EMBEDDING_CACHE_DIR,
            local_files_only=bool(settings.SERVICEMIND_EMBEDDING_CACHE_DIR),
        )
    )
    reranker = (
        TeiReranker(
            settings.SERVICEMIND_RERANKER_URL, model_revision=settings.SERVICEMIND_RERANKER_REVISION
        )
        if settings.SERVICEMIND_RERANKER_URL
        else BgeM3Reranker(
            model_revision=settings.SERVICEMIND_RERANKER_REVISION,
            device=settings.SERVICEMIND_MODEL_DEVICE,
            cache_folder=settings.SERVICEMIND_RERANKER_CACHE_DIR,
            local_files_only=bool(settings.SERVICEMIND_RERANKER_CACHE_DIR),
        )
    )
    from servicemind.graphrag.build import build_graph_store

    return EnterpriseRAG(
        index=OpenSearchKnowledgeIndex(build_opensearch_client(), dimension=embedding.dimension),
        embedding=embedding,
        reranker=reranker,
        repository=knowledge_repository,
        graph_store=build_graph_store(),
    )
