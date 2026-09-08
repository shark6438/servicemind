"""Infrastructure smoke test for ACL-filtered Hybrid RAG against real OpenSearch."""

import asyncio
import json
import selectors
from uuid import UUID

from servicemind.agents.knowledge import RUNBOOKS
from servicemind.domain.knowledge import RetrievalPrincipal
from servicemind.rag.models import CallableReranker, DeterministicEmbeddingProvider
from servicemind.rag.opensearch import OpenSearchKnowledgeIndex
from servicemind.rag.service import EnterpriseRAG, build_opensearch_client
from servicemind.rag.sources import InternalRunbookSource

TENANT = UUID("11111111-1111-4111-8111-111111111111")
OTHER_TENANT = UUID("22222222-2222-4222-8222-222222222222")


class MemoryRepository:
    """RLS-authority substitute so this OpenSearch-only smoke can run without PG.

    Keeps the production invariant intact: parent expansion is served by a
    repository (here an in-memory map filled at ingest), never by the search index.
    """

    def __init__(self) -> None:
        self.parent_content: dict[UUID, str] = {}

    async def is_current(self, tenant_id, document) -> bool:
        return False

    async def replace(self, tenant_id, document, parents, children):
        for parent in parents:
            self.parent_content[parent.parent_chunk_id] = parent.content
        return document, parents, children

    async def mark_indexed(self, tenant_id, document_id) -> None:
        return None

    async def count_pending(self, tenant_id) -> int:
        return 0

    async def parents(self, tenant_id, ids):
        return {item: self.parent_content[item] for item in ids if item in self.parent_content}


async def main() -> None:
    client = build_opensearch_client()
    embedding = DeterministicEmbeddingProvider()
    index = OpenSearchKnowledgeIndex(
        client, prefix="sm-rag-infrastructure-smoke", dimension=embedding.dimension
    )
    rag = EnterpriseRAG(
        index=index,
        embedding=embedding,
        reranker=CallableReranker(
            lambda query, text: len(set(query.casefold().split()) & set(text.casefold().split()))
        ),
        repository=MemoryRepository(),
    )
    try:
        documents = await InternalRunbookSource(TENANT, RUNBOOKS).load()
        ingested = await rag.ingest(TENANT, documents)
        result = await rag.retrieve(
            principal=RetrievalPrincipal(
                tenant_id=TENANT, user_id="rag-smoke", entity_ids=frozenset({1})
            ),
            query="VPN MFA identity provider troubleshooting",
            use_query_model=False,
        )
        evidence = rag.to_evidence(TENANT, result)
        if not evidence or not evidence[0].metadata.get("citation"):
            raise RuntimeError("Hybrid RAG returned no citable evidence")
        # Parents are sharded per tenant index: another tenant's index must not
        # resolve the tenant's parent chunk. Build the sibling generation and
        # publish its (empty) active alias explicitly so the mget path is exercised
        # rather than short-circuiting on a missing alias.
        await index.ensure(OTHER_TENANT, embedding)
        await index.publish(OTHER_TENANT, embedding)
        leaked = await index.parents(OTHER_TENANT, [result.items[0].hit.parent_chunk_id])
        if leaked:
            raise RuntimeError("Cross-tenant parent retrieval was not blocked by RLS")
        print(
            json.dumps(
                {
                    "status": "passed",
                    "ingested": ingested,
                    "candidates": result.candidate_count,
                    "evidence": len(evidence),
                    "mode": result.retrieval_mode,
                    "top_source": evidence[0].source_ref,
                    "cross_tenant_parent_leaks": 0,
                },
                indent=2,
            )
        )
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))
