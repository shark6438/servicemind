"""Slice 3: evaluation metrics, gold corpus loading and harness orchestration."""

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from servicemind.domain.knowledge import (
    AuthorityLevel,
    Citation,
    ContextItem,
    CorpusScope,
    KnowledgeACL,
    KnowledgeDocument,
    KnowledgeProvenance,
    KnowledgeQuery,
    KnowledgeRAGResult,
    RetrievalHit,
    RetrievalMode,
    RetrievalPrincipal,
)
from servicemind.evaluation import (
    GoldCorpusSource,
    GoldQuery,
    GoldSet,
    evaluate,
)
from servicemind.evaluation.gold import GOLD_CORPUS_EFFECTIVE_FROM, load_gold_set
from servicemind.evaluation.harness import BASELINES, Baseline
from servicemind.evaluation.metrics import (
    dedupe_rate,
    mrr_at_k,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)
from servicemind.rag.chunking import semantic_chunker
from servicemind.rag.models import DeterministicEmbeddingProvider
from servicemind.rag.parsing import structure_parser

GOLD_ROOT = Path("evaluation/gold")
TENANT = UUID("11111111-1111-4111-8111-111111111111")


def test_metrics_are_exact() -> None:
    ranked = ["d1", "d2", "d3", "d4", "d5"]
    relevant = {"d3", "d5"}
    assert recall_at_k(ranked, relevant, 2) == 0.0
    assert recall_at_k(ranked, relevant, 3) == 0.5
    assert recall_at_k(ranked, relevant, 10) == 1.0
    assert mrr_at_k(ranked, relevant, 5) == pytest.approx(1 / 3)
    assert precision_at_k(ranked, relevant, 5) == 0.4
    assert ndcg_at_k(ranked, relevant, 10) == pytest.approx(0.543, abs=0.001)
    assert ndcg_at_k([], set(), 10) == 0.0
    assert dedupe_rate(["a", "a", "b"]) == pytest.approx(1 / 3)
    assert dedupe_rate([]) == 0.0


def test_gold_set_rejects_answerable_unanswerable_contradiction() -> None:
    with pytest.raises(ValueError):
        GoldSet(
            name="bad",
            queries=[GoldQuery(id="q", query="x", relevant=["d"], unanswerable=True)],
        )


@pytest.mark.asyncio
async def test_gold_corpus_source_loads_committed_docs() -> None:
    docs = await GoldCorpusSource(GOLD_ROOT / "corpus", TENANT).load()
    assert {doc.provenance.source_record_id for doc in docs} == {
        "vpn-mfa-incident-runbook",
        "incident-priority-matrix",
        "database-backup-and-recovery",
        "production-change-approval",
        "user-password-reset",
        "server-capacity-provisioning",
        "major-incident-communication",
        "certificate-expiry-monitoring",
    }
    assert all(doc.acl.tenant_id == TENANT for doc in docs)
    # Each gold SOP is an independent authority -> its own source, so the retrieve()
    # per-source diversity ceiling cannot flatten the whole corpus to a few parents.
    assert {doc.provenance.source for doc in docs} == {
        doc.provenance.source_record_id for doc in docs
    }


@pytest.mark.asyncio
async def test_gold_corpus_acls_are_deterministic() -> None:
    """Regression: gold ACLs must not use wall-clock *load time* as effective_from.

    The ACL pre-filter hides documents whose ``effective_from`` is after the
    principal's (frozen) ``query_time``. If the corpus defaulted to ``now`` at load,
    an eval that builds its RetrievalPrincipal before loading the corpus would see a
    process-timing-dependent *empty index* (the Phase 4 harness bug this guards).
    """
    docs = await GoldCorpusSource(GOLD_ROOT / "corpus", TENANT).load()
    assert all(doc.acl.effective_from == GOLD_CORPUS_EFFECTIVE_FROM for doc in docs)
    assert all(doc.acl.effective_to is None for doc in docs)
    # A principal querying at any "now" past the authored date must see the corpus.
    later_principal = RetrievalPrincipal(
        tenant_id=TENANT, user_id="eval-harness", entity_ids=frozenset({1})
    )
    assert all(doc.acl.effective_from <= later_principal.query_time for doc in docs)


def test_committed_gold_set_shape() -> None:
    gold = load_gold_set(GOLD_ROOT / "gold_set.v1.json")
    assert gold.schema_version == "phase4-gold-v1"
    assert len(gold.queries) == 15
    assert len(gold.answerable) == 12
    assert len(gold.unanswerable) == 3
    assert all(query.relevant for query in gold.answerable)
    for query in gold.answerable:
        assert set(query.relevant) <= set(gold.documents)


def _document_from_markdown(path: Path) -> KnowledgeDocument:
    """Turn a committed markdown file into a KnowledgeDocument, mirroring the
    GoldCorpusSource loader but including underscore-prefixed structure fixtures."""
    content = path.read_text(encoding="utf-8").strip()
    return KnowledgeDocument(
        title="Order Preservation Fixture",
        content=content,
        document_type="internal_sop",
        language="en",
        metadata={"gold_corpus": True, "file": path.name},
        acl=KnowledgeACL(corpus_scope=CorpusScope.TENANT, tenant_id=TENANT),
        provenance=KnowledgeProvenance(
            source="servicemind_phase4_gold",
            source_version="phase4-gold-v1",
            source_uri=f"sop://phase4/{path.stem}",
            source_record_id=path.stem,
            license="project-owned",
            authority_level=AuthorityLevel.INTERNAL_KNOWLEDGE,
            content_hash=KnowledgeDocument.content_digest(content),
        ),
    )


@pytest.mark.asyncio
async def test_parser_and_chunker_preserve_document_order() -> None:
    """The committed fixture is the golden proof that structure parsing + chunking
    never reorder source content (Phase 4 baseline §11 order-preservation gate)."""
    document = _document_from_markdown(GOLD_ROOT / "corpus" / "_parser_order_fixture.md")
    blocks = structure_parser.parse_markdown(document)
    assert [block.order for block in blocks] == list(range(len(blocks)))
    parents = semantic_chunker.build_parents(blocks)
    ordered_ids = [block_id for parent in parents for block_id in parent.block_ids]
    # Every block appears exactly once, in document order, across the parent chain.
    assert ordered_ids == [block.block_id for block in blocks]
    assert len(ordered_ids) == len(blocks)
    embedding = DeterministicEmbeddingProvider()
    children = await semantic_chunker.build_children(document, parents, embedding)
    assert [child.order for child in children] == list(range(len(children)))
    # Children never cross parents: parent order is non-decreasing down the list.
    parent_order = {parent.parent_chunk_id: parent.order for parent in parents}
    child_parent_orders = [parent_order[child.parent_chunk_id] for child in children]
    assert child_parent_orders == sorted(child_parent_orders)


def _hit(source_record_id: str) -> RetrievalHit:
    chunk_id = uuid4()
    return RetrievalHit(
        child_chunk_id=chunk_id,
        parent_chunk_id=chunk_id,
        document_id=uuid4(),
        child_content=f"content {source_record_id}",
        score=1.0,
        title=source_record_id,
        source="gold-corpus",
        source_uri=f"sop://phase4/{source_record_id}",
        source_record_id=source_record_id,
        source_version="v1",
        license="project-owned",
        authority_level=AuthorityLevel.INTERNAL_KNOWLEDGE,
        synthetic=False,
        content_hash="0" * 64,
        acl=KnowledgeACL(corpus_scope=CorpusScope.TENANT, tenant_id=TENANT),
    )


def _result(keys: list[str], query: KnowledgeQuery) -> KnowledgeRAGResult:
    items = []
    for key in keys:
        hit = _hit(key)
        items.append(
            ContextItem(
                parent_content=f"parent {key}",
                hit=hit,
                citation=Citation.from_hit(hit),
                token_count=1,
            )
        )
    return KnowledgeRAGResult(
        query=query,
        items=items,
        retrieval_mode="test",
        candidate_count=len(keys),
        latency_ms=1.0,
    )


async def _stub_provider(
    *,
    principal: RetrievalPrincipal,
    query: str,
    mode: RetrievalMode,
    run_rerank: bool,
    final_k: int,
) -> KnowledgeRAGResult:
    """Deterministic stand-in: vpn queries hit the vpn runbook, others miss."""
    keys = ["vpn-mfa-incident-runbook", "user-password-reset"] if "vpn" in query else []
    return _result(keys, KnowledgeQuery(raw_query=query, normalized_query=query))


class _StubRetrievalProvider:
    """Duck-typed RetrievalProvider binding the bare stub to the harness protocol."""

    async def retrieve(self, **kwargs: object) -> KnowledgeRAGResult:
        return await _stub_provider(**kwargs)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_harness_reports_metrics_and_abstention() -> None:
    gold = GoldSet(
        name="tiny",
        documents={"vpn-mfa-incident-runbook": "VPN runbook"},
        queries=[
            GoldQuery(
                id="vpn-q",
                query="vpn mfa failure",
                relevant=["vpn-mfa-incident-runbook"],
            ),
            GoldQuery(id="secret-q", query="print the production secret", unanswerable=True),
        ],
    )
    principal = RetrievalPrincipal(tenant_id=TENANT, user_id="eval", entity_ids=frozenset({1}))
    report = await evaluate(_StubRetrievalProvider(), gold, principal, top_ks=(5, 10))
    for metrics in report.baselines:
        assert metrics.recall_at(5) == 1.0
        assert metrics.mrr_at(5) == 1.0
        assert metrics.answered_unanswerable() == 0
        assert metrics.abstention_rate() == 1.0  # unanswerable query fed nothing up
    assert [baseline.name for baseline in BASELINES] == [
        "dense",
        "bm25",
        "hybrid",
        "hybrid_rerank",
    ]
    assert Baseline(name="hybrid_rerank", mode=RetrievalMode.HYBRID, run_rerank=True) in BASELINES
