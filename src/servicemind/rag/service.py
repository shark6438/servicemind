from __future__ import annotations

import asyncio
import time
from uuid import UUID

from opensearchpy import AsyncOpenSearch

from core import settings
from servicemind.domain.evidence import Evidence, EvidenceSourceType
from servicemind.domain.knowledge import (
    Citation,
    ContextItem,
    KnowledgeRAGResult,
    RetrievalPrincipal,
)
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


class EnterpriseRAG:
    def __init__(
        self,
        *,
        index: OpenSearchKnowledgeIndex,
        embedding: EmbeddingProvider,
        reranker: Reranker,
        repository: KnowledgeRepository | None = None,
    ) -> None:
        self.index, self.embedding, self.reranker = index, embedding, reranker
        self.repository = repository

    async def reconcile(self, tenant_id: UUID) -> int:
        if not self.repository:
            return 0
        return await self.repository.reconcile_indexed(
            tenant_id, await self.index.indexed_document_ids(tenant_id)
        )

    async def ingest(self, tenant_id: UUID, documents, *, concurrency: int = 2) -> dict[str, int]:
        semaphore = asyncio.Semaphore(concurrency)

        async def ingest_one(document):
            async with semaphore:
                if self.repository and await self.repository.is_current(tenant_id, document):
                    return 0, 0, True
                blocks = (
                    structure_parser.parse_html(document)
                    if document.content.lstrip().startswith("<")
                    else structure_parser.parse_markdown(document)
                )
                parents = semantic_chunker.build_parents(blocks)
                children = await semantic_chunker.build_children(document, parents, self.embedding)
                if self.repository:
                    await self.repository.replace(tenant_id, document, parents, children)
                await self.index.replace_document(
                    tenant_id, document, parents, children, self.embedding
                )
                if self.repository:
                    await self.repository.mark_indexed(tenant_id, document.document_id)
                return len(parents), len(children), False

        results = await asyncio.gather(*(ingest_one(document) for document in documents))
        await self.index.refresh(tenant_id)
        return {
            "documents": len(results),
            "parents": sum(parent_count for parent_count, _, _ in results),
            "children": sum(child_count for _, child_count, _ in results),
            "skipped": sum(1 for _, _, skipped in results if skipped),
        }

    async def retrieve(
        self,
        *,
        principal: RetrievalPrincipal,
        query: str,
        use_query_model: bool = True,
        final_k: int = 8,
    ) -> KnowledgeRAGResult:
        started = time.perf_counter()
        processed = await query_processor.process(query, use_model=use_query_model)
        hits = await self.index.search(processed, principal, self.embedding)
        scores = await self.reranker.score(
            processed.normalized_query, [x.child_content for x in hits]
        )
        ranked = sorted(
            [
                x.model_copy(update={"rerank_score": score})
                for x, score in zip(hits, scores, strict=True)
            ],
            key=lambda x: x.rerank_score or -1,
            reverse=True,
        )
        unique = []
        seen = set()
        for hit in ranked:
            if hit.parent_chunk_id not in seen:
                unique.append(hit)
                seen.add(hit.parent_chunk_id)
            if len(unique) >= final_k:
                break
        parent_ids = [x.parent_chunk_id for x in unique]
        parents = (
            await self.repository.parents(principal.tenant_id, parent_ids)
            if self.repository
            else await self.index.parents(principal.tenant_id, parent_ids)
        )
        items = []
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
            retrieval_mode="dense_bm25_rrf_cross_encoder_parent",
            candidate_count=len(hits),
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    def to_evidence(self, tenant_id: UUID, result: KnowledgeRAGResult) -> list[Evidence]:
        return [
            Evidence.create(
                tenant_id=tenant_id,
                source_type=EvidenceSourceType.KNOWLEDGE,
                source_ref=item.citation.source_uri,
                resource_type="knowledge_parent_chunk",
                resource_id=str(item.citation.parent_chunk_id),
                content=item.parent_content,
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
        )
    )
    return EnterpriseRAG(
        index=OpenSearchKnowledgeIndex(build_opensearch_client(), dimension=embedding.dimension),
        embedding=embedding,
        reranker=reranker,
        repository=knowledge_repository,
    )
