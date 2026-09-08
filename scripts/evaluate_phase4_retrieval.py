"""Run the Phase 4 retrieval evaluation against the committed gold set.

Builds an isolated ``sm-rag-eval-*`` index (never the production alias), ingests the
gold corpus through the real Enterprise RAG pipeline, then measures the four
baselines (dense / bm25 / hybrid / hybrid+rerank). Writes
``evaluation/reports/phase4_retrieval_latest.{json,md}``.

Usage:
    PYTHONPATH=src .venv/bin/python scripts/evaluate_phase4_retrieval.py \
        [--model deterministic|bge|tei] [--top-k 5,10,20]

``deterministic`` embeddings are fast and offline (harness wiring checks); ``bge``
uses the pinned BGE-M3 snapshot and is the number that counts for acceptance.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import selectors
from pathlib import Path
from uuid import UUID, uuid4

from servicemind.domain.knowledge import RetrievalPrincipal
from servicemind.evaluation import (
    GoldCorpusSource,
    load_gold_set,
)
from servicemind.evaluation.harness import evaluate, markdown_report
from servicemind.rag.models import (
    BgeM3EmbeddingProvider,
    BgeM3Reranker,
    CallableReranker,
    DeterministicEmbeddingProvider,
    TeiEmbeddingProvider,
    TeiReranker,
)
from servicemind.rag.opensearch import OpenSearchKnowledgeIndex
from servicemind.rag.service import EnterpriseRAG, build_opensearch_client

TENANT = UUID("11111111-1111-4111-8111-111111111111")
REPORTS_DIR = Path("evaluation/reports")


class MemoryRepository:
    """RLS-authority substitute so the harness runs without PostgreSQL."""

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


def _reranker() -> CallableReranker:
    # Deterministic token-overlap stand-in keeps the harness offline; swap for the
    # BGE cross-encoder when --model bge (the acceptance run).
    return CallableReranker(
        lambda query, text: len(set(query.casefold().split()) & set(text.casefold().split()))
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", default="evaluation/gold/gold_set.v1.json")
    parser.add_argument("--corpus", default="evaluation/gold/corpus")
    parser.add_argument(
        "--model",
        choices=["deterministic", "bge", "tei"],
        default="deterministic",
    )
    parser.add_argument("--top-k", default="5,10,20")
    args = parser.parse_args()

    top_ks = tuple(int(x) for x in args.top_k.split(",") if x.strip())
    gold = load_gold_set(Path(args.gold))
    client = build_opensearch_client()
    if args.model in {"bge", "tei"}:
        from core import settings

    if args.model == "bge":
        embedding = BgeM3EmbeddingProvider(
            model_revision=settings.SERVICEMIND_EMBEDDING_REVISION,
            device=settings.SERVICEMIND_MODEL_DEVICE or "cpu",
            cache_folder=settings.SERVICEMIND_EMBEDDING_CACHE_DIR,
            local_files_only=bool(settings.SERVICEMIND_EMBEDDING_CACHE_DIR),
        )
        reranker = BgeM3Reranker(
            model_revision=settings.SERVICEMIND_RERANKER_REVISION,
            device=settings.SERVICEMIND_MODEL_DEVICE or "cpu",
            cache_folder=settings.SERVICEMIND_RERANKER_CACHE_DIR,
            local_files_only=bool(settings.SERVICEMIND_RERANKER_CACHE_DIR),
        )
    elif args.model == "tei":
        if not settings.SERVICEMIND_EMBEDDING_URL:
            raise RuntimeError("SERVICEMIND_EMBEDDING_URL is required for --model tei")
        if not settings.SERVICEMIND_RERANKER_URL:
            raise RuntimeError("SERVICEMIND_RERANKER_URL is required for --model tei")
        embedding = TeiEmbeddingProvider(
            settings.SERVICEMIND_EMBEDDING_URL,
            model_revision=settings.SERVICEMIND_EMBEDDING_REVISION,
        )
        reranker = TeiReranker(
            settings.SERVICEMIND_RERANKER_URL,
            model_revision=settings.SERVICEMIND_RERANKER_REVISION,
        )
    else:
        embedding = DeterministicEmbeddingProvider()
        reranker = _reranker()
    index = OpenSearchKnowledgeIndex(
        client,
        prefix=f"sm-rag-eval-{uuid4().hex[:8]}",
        dimension=embedding.dimension,
    )
    rag = EnterpriseRAG(
        index=index,
        embedding=embedding,
        reranker=reranker,
        repository=MemoryRepository(),
    )
    try:
        documents = await GoldCorpusSource(Path(args.corpus), TENANT).load()
        totals = await rag.ingest(TENANT, documents)
        # Build the retrieval principal *after* ingestion so query_time is a real
        # "as-of now" for the eval and post-dates every document's effective_from.
        # (Constructing it earlier froze query_time a few microseconds before the
        # corpus was loaded, which let the ACL effective_from window hide the whole
        # index depending on process timing.)
        principal = RetrievalPrincipal(
            tenant_id=TENANT, user_id="eval-harness", entity_ids=frozenset({1})
        )

        class HarnessProvider:
            """Bind EnterpriseRAG.retrieve (use_query_model=False: deterministic,
            never the production query-side model) to the eval protocol."""

            async def retrieve(self, **kwargs: object):
                return await rag.retrieve(use_query_model=False, **kwargs)

        report = await evaluate(HarnessProvider(), gold, principal, top_ks=top_ks)
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "gold_set": report.gold_set,
            "model": args.model,
            "index_prefix": index.prefix,
            "ingested": totals,
            "queries": {
                "total": len(gold.queries),
                "answerable": len(gold.answerable),
                "unanswerable": len(gold.unanswerable),
            },
            "top_ks": report.top_ks,
            "results": {
                metrics.baseline: {
                    "recall_at_5": round(metrics.recall_at(5), 4),
                    "recall_at_10": round(metrics.recall_at(10), 4),
                    "recall_at_20": round(metrics.recall_at(20), 4),
                    "mrr_at_10": round(metrics.mrr_at(10), 4),
                    "ndcg_at_10": round(metrics.ndcg_at(10), 4),
                    "precision_at_5": round(metrics.precision_at(5), 4),
                    "mean_dedupe_rate": round(metrics.mean_dedupe(), 4),
                    "answered_unanswerable": metrics.answered_unanswerable(),
                    "abstention_rate": round(metrics.abstention_rate(), 4),
                }
                for metrics in report.baselines
            },
        }
        (REPORTS_DIR / "phase4_retrieval_latest.json").write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
        (REPORTS_DIR / "phase4_retrieval_latest.md").write_text(
            markdown_report(report), encoding="utf-8"
        )
        print(json.dumps(payload, indent=2))
    finally:
        # Eval runs never share the production alias, and leaving the isolated
        # concrete indices behind would let repeated runs accumulate shards on the
        # cluster (observed: hundreds of single-run indexes destabilise refresh).
        try:
            await index.drop_tenant_indices(TENANT)
        finally:
            await client.close()


if __name__ == "__main__":
    asyncio.run(main(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))
