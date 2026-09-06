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
        # resolve the tenant's parent chunk. Build the sibling index explicitly so
        # the mget path is exercised rather than short-circuiting on a 404.
        await index.ensure(OTHER_TENANT)
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
