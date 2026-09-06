from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from servicemind.domain.knowledge import (
    AuthorityLevel,
    CorpusScope,
    KnowledgeACL,
    RetrievalHit,
    RetrievalPrincipal,
)
from servicemind.rag.chunking import StructureAwareSemanticChunker
from servicemind.rag.models import CallableReranker, DeterministicEmbeddingProvider
from servicemind.rag.opensearch import OpenSearchKnowledgeIndex
from servicemind.rag.parsing import StructureParser
from servicemind.rag.service import EnterpriseRAG
from servicemind.rag.sources import make_document

TENANT = UUID("11111111-1111-4111-8111-111111111111")


def document(content: str):
    return make_document(
        title="VPN runbook",
        content=content,
        document_type="runbook",
        source="test",
        source_version="v1",
        source_uri="runbook://vpn",
        source_record_id="vpn",
        license_name="project-owned",
        authority=AuthorityLevel.INTERNAL_KNOWLEDGE,
        acl=KnowledgeACL(
            corpus_scope=CorpusScope.TENANT,
            tenant_id=TENANT,
            entity_ids=frozenset({1}),
        ),
    )


def test_structure_recovery_precedes_chunking_and_preserves_table() -> None:
    value = document(
        "# VPN\n\nIntro paragraph.\n\n## Checks\n\n- Verify IdP\n- Verify clock\n\n| Code | Meaning |\n|---|---|\n| E42 | Clock skew |"
    )
    blocks = StructureParser().parse_markdown(value)
    assert [item.block_type.value for item in blocks] == [
        "heading",
        "paragraph",
        "heading",
        "list",
        "table",
    ]
    assert "| E42 | Clock skew |" in blocks[-1].content


@pytest.mark.asyncio
async def test_parent_child_chunker_indexes_children_only() -> None:
    value = document("# VPN\n\n" + "Check identity provider health. " * 100)
    blocks = StructureParser().parse_markdown(value)
    chunker = StructureAwareSemanticChunker(
        child_min_tokens=10, child_target_tokens=30, child_max_tokens=50
    )
    parents = chunker.build_parents(blocks)
    children = await chunker.build_children(value, parents, DeterministicEmbeddingProvider())
    assert parents
    assert len(children) > len(parents)
    assert all(
        child.parent_chunk_id in {parent.parent_chunk_id for parent in parents}
        for child in children
    )
    assert all(child.token_count <= 50 for child in children)


def test_acl_filter_is_compiled_before_retrieval() -> None:
    index = OpenSearchKnowledgeIndex(object(), dimension=16)  # type: ignore[arg-type]
    filters = index._acl_filter(
        RetrievalPrincipal(tenant_id=TENANT, user_id="u1", entity_ids=frozenset({1}))
    )
    serialized = str(filters)
    assert str(TENANT) in serialized
    assert "effective_from" in serialized
    assert "entity_ids" in serialized
    assert "user_ids" in serialized


class FakeIndex:
    def __init__(self, hits):
        self.hits = hits
        self.search_calls = 0

    async def search(self, query, principal, embedding):
        self.search_calls += 1
        assert principal.tenant_id == TENANT
        return self.hits

    async def parents(self, tenant_id, ids):
        assert tenant_id == TENANT
        return {item: "Full parent context with complete troubleshooting steps." for item in ids}


@pytest.mark.asyncio
async def test_hybrid_result_reranks_deduplicates_expands_and_cites() -> None:
    value = document("# VPN\n\nVerify the identity provider.")
    parent_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    hits = [
        RetrievalHit(
            child_chunk_id=UUID(f"00000000-0000-4000-8000-00000000000{i}"),
            parent_chunk_id=parent_id,
            document_id=value.document_id,
            child_content=text,
            score=score,
            title=value.title,
            source=value.provenance.source,
            source_uri=value.provenance.source_uri,
            source_record_id=value.provenance.source_record_id,
            source_version=value.provenance.source_version,
            license=value.provenance.license,
            authority_level=value.provenance.authority_level,
            synthetic=False,
            content_hash=value.provenance.content_hash,
            acl=value.acl,
        )
        for i, (text, score) in enumerate((("irrelevant", 0.9), ("vpn identity", 0.5)), start=1)
    ]
    rag = EnterpriseRAG(
        index=FakeIndex(hits),  # type: ignore[arg-type]
        embedding=DeterministicEmbeddingProvider(),
        reranker=CallableReranker(lambda query, text: 1 if "vpn" in text else 0),
    )
    result = await rag.retrieve(
        principal=RetrievalPrincipal(tenant_id=TENANT, user_id="u1", entity_ids=frozenset({1})),
        query="How to troubleshoot VPN?",
        use_query_model=False,
    )
    assert len(result.items) == 1
    assert result.items[0].parent_content.startswith("Full parent")
    assert result.items[0].citation.citation_id.startswith("cite-")
    evidence = rag.to_evidence(TENANT, result)
    assert evidence[0].metadata["citation"]["source_uri"] == "runbook://vpn"


def test_acl_rejects_invalid_effective_window() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="later"):
        KnowledgeACL(
            corpus_scope=CorpusScope.TENANT,
            tenant_id=TENANT,
            effective_from=now,
            effective_to=now - timedelta(seconds=1),
        )
