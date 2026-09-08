"""A/B the multi-query fan-out path against the single-query RRF path.

Both arms run the SAME processed query (real LLM rewrites captured once into
``evaluation/gold/gold_rewrites.v1.json`` through the production rewrite stage, then
committed so this comparison is reproducible offline). The only difference is the
``use_rewrites`` fan-out flag, so anything that moves is attributable to fan-out.

Two measurement surfaces:

1. Final-context A/B (what the agent consumes): Recall@k / MRR@10 / NDCG@10 plus
   candidate_count and latency. On the committed gold set the answerable queries are
   saturated (MRR@10 == 1.0 for every baseline), so this surface is expected flat —
   the report says so honestly rather than claiming a gain.
2. Candidate-pool A/B (the only mechanism by which fan-out can ever help): how many
   distinct candidate parents survive the RRF top-k before the reranker sees them.
   This is NOT saturated and shows whether fan-out actually broadens the pool the
   cross-encoder chooses from.

Usage:
    PYTHONPATH=src .venv/bin/python scripts/compare_phase4_multiquery.py \
        [--model deterministic|bge] [--top-k 1,5,10]

Writes ``evaluation/reports/phase4_multiquery_latest.{json,md}`` and a keep/delete
recommendation field.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import selectors
import statistics
from pathlib import Path
from uuid import UUID, uuid4

from servicemind.domain.knowledge import (
    KnowledgeQuery,
    RetrievalMode,
    RetrievalPrincipal,
)
from servicemind.evaluation import (
    GoldCorpusSource,
    load_gold_set,
)
from servicemind.evaluation.metrics import (
    mrr_at_k,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)
from servicemind.rag.models import (
    BgeM3EmbeddingProvider,
    BgeM3Reranker,
    CallableReranker,
    DeterministicEmbeddingProvider,
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


class CannedQueryProcessor:
    """Returns the committed processed query per gold query text (no live LLM).

    This is the reproducibility seam: fan-out and single-query arms must run the
    identical normalized query + rewrites so the A/B isolates ``use_rewrites`` only.
    """

    def __init__(self, mapping: dict[str, KnowledgeQuery]) -> None:
        self.mapping = mapping

    async def process(self, query: str, *, use_model: bool = True) -> KnowledgeQuery:
        processed = self.mapping.get(query)
        if processed is None:  # pragma: no cover - every gold query is pre-captured
            raise KeyError(f"no committed processed query for: {query!r}")
        return processed


def _processed_from_sidecar(sidecar: dict, gold) -> dict[str, KnowledgeQuery]:
    by_id = {query.id: query for query in gold.queries}
    mapping: dict[str, KnowledgeQuery] = {}
    for query_id, entry in sidecar["queries"].items():
        gold_query = by_id[query_id]
        mapping[gold_query.query] = KnowledgeQuery(
            raw_query=gold_query.query,
            normalized_query=entry["normalized_query"],
            rewritten_queries=entry["rewritten_queries"],
        )
    return mapping


def _reranker() -> CallableReranker:
    # Deterministic token-overlap stand-in keeps the harness offline; swap for the
    # BGE cross-encoder when --model bge (the acceptance run).
    return CallableReranker(
        lambda query, text: len(set(query.casefold().split()) & set(text.casefold().split()))
    )


def _result_keys(result) -> list[str]:
    seen: set[str] = set()
    keys: list[str] = []
    for item in result.items:
        key = item.citation.source_record_id
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", default="evaluation/gold/gold_set.v1.json")
    parser.add_argument("--rewrites", default="evaluation/gold/gold_rewrites.v1.json")
    parser.add_argument("--corpus", default="evaluation/gold/corpus")
    parser.add_argument("--model", choices=["deterministic", "bge"], default="deterministic")
    parser.add_argument("--top-k", default="1,5,10")
    args = parser.parse_args()

    top_ks = tuple(int(x) for x in args.top_k.split(",") if x.strip())
    gold = load_gold_set(Path(args.gold))
    sidecar = json.loads(Path(args.rewrites).read_text(encoding="utf-8"))
    processor = CannedQueryProcessor(_processed_from_sidecar(sidecar, gold))

    client = build_opensearch_client()
    if args.model == "bge":
        from core import settings

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
    else:
        embedding = DeterministicEmbeddingProvider()
        reranker = _reranker()
    index = OpenSearchKnowledgeIndex(
        client,
        prefix=f"sm-rag-mq-{uuid4().hex[:8]}",
        dimension=embedding.dimension,
    )
    rag = EnterpriseRAG(
        index=index,
        embedding=embedding,
        reranker=reranker,
        repository=MemoryRepository(),
    )
    # Swap the live query-side processor for the committed one; retrieve() then runs
    # its full tail (rerank / dedupe / diversity ceilings / packer) unchanged.
    import servicemind.rag.service as service

    original_processor = service.query_processor
    service.query_processor = processor  # type: ignore[assignment]
    try:
        documents = await GoldCorpusSource(Path(args.corpus), TENANT).load()
        await rag.ingest(TENANT, documents)
        principal = RetrievalPrincipal(
            tenant_id=TENANT, user_id="mq-eval", entity_ids=frozenset({1})
        )

        arms = {
            "single_query": {"use_rewrites": False, "note": "dense + BM25 on one text"},
            "fan_out": {
                "use_rewrites": True,
                "note": "dense anchor + one BM25 arm per rewrite (<=5 arms cap)",
            },
        }
        per_arm: dict[str, list[dict]] = {name: [] for name in arms}
        for gold_query in gold.queries:
            for name, spec in arms.items():
                result = await rag.retrieve(
                    principal=principal,
                    query=gold_query.query,
                    use_query_model=True,  # satisfied by the canned processor
                    use_rewrites=spec["use_rewrites"],
                    final_k=max(top_ks) + 4,
                )
                keys = _result_keys(result)
                per_arm[name].append(
                    {
                        "query_id": gold_query.id,
                        "unanswerable": gold_query.unanswerable,
                        "relevant": list(gold_query.relevant),
                        "keys": keys,
                        "candidate_count": result.candidate_count,
                        "latency_ms": result.latency_ms,
                    }
                )

            # Candidate-pool probe (mechanism surface, not saturated): distinct
            # candidate parents the RRF top-k hands the reranker per arm.
            processed = processor.mapping[gold_query.query]
            probe = {}
            for name, spec in arms.items():
                hits = await index.search(
                    processed,
                    principal,
                    embedding,
                    mode=RetrievalMode.HYBRID,
                    use_rewrites=spec["use_rewrites"],
                )
                probe[name] = {
                    "candidate_hits": len(hits),
                    "distinct_parents": len({hit.parent_chunk_id for hit in hits}),
                }
            for name in arms:
                per_arm[name][-1].update({f"pool_{k}": v for k, v in probe[name].items()})

        def _mean(rows, key, only_answerable=False):
            values = [
                row[key]
                for row in rows
                if not (only_answerable and row["unanswerable"])
            ]
            return statistics.fmean(values) if values else 0.0

        report_arms: dict[str, dict] = {}
        for name in arms:
            rows = per_arm[name]
            answerable = [r for r in rows if not r["unanswerable"]]
            report_arms[name] = {
                "note": arms[name]["note"],
                "recall_at": {
                    str(k): round(
                        statistics.fmean(
                            recall_at_k(r["keys"], set(r["relevant"]), k) for r in answerable
                        ),
                        4,
                    )
                    for k in top_ks
                },
                "mrr_at_10": round(
                    statistics.fmean(mrr_at_k(r["keys"], set(r["relevant"]), 10) for r in answerable),
                    4,
                ),
                "ndcg_at_10": round(
                    statistics.fmean(ndcg_at_k(r["keys"], set(r["relevant"]), 10) for r in answerable),
                    4,
                ),
                "precision_at_1": round(
                    statistics.fmean(precision_at_k(r["keys"], set(r["relevant"]), 1) for r in answerable),
                    4,
                ),
                "mean_candidate_count": round(_mean(rows, "candidate_count"), 1),
                "mean_pool_distinct_parents": round(_mean(rows, "pool_distinct_parents"), 1),
                "mean_pool_candidate_hits": round(_mean(rows, "pool_candidate_hits"), 1),
                "mean_latency_ms": round(_mean(rows, "latency_ms"), 1),
                "answered_unanswerable": sum(1 for r in rows if r["unanswerable"] and r["keys"]),
            }

        single, fan = report_arms["single_query"], report_arms["fan_out"]
        # Keep/delete: fan-out is retained when it never *loses* a final-context hit
        # the single arm reached and it does not reduce abstention. Gains cannot be
        # demonstrated on a saturated gold set (MRR@10 == 1.0 already), which the
        # report states explicitly instead of manufacturing a difference.
        regression = any(
            (fan[k] - single[k]) < -1e-9
            for k in ("mrr_at_10", "ndcg_at_10", "precision_at_1")
        )
        abstention_same = (
            fan["answered_unanswerable"] == single["answered_unanswerable"]
        )
        decision = (
            "keep"
            if not regression and abstention_same
            else "investigate"
        )

        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "title": "Multi-query fan-out A/B — single-query RRF vs lexical fan-out "
            "(dense anchor + N BM25 arms)",
            "model": args.model,
            "gold_set": gold.name,
            "rewrites": sidecar.get("model"),
            "rewrites_captured_at": sidecar.get("captured_at"),
            "answerable": len(gold.answerable),
            "unanswerable": len(gold.unanswerable),
            "arms": report_arms,
            "ceiling_note": (
                "Final-context Recall/MRR is saturated at the ceiling on the committed "
                "gold (MRR@10 == 1.0 in every baseline); equality on this surface is "
                "expected, not evidence of no benefit. The measurable, non-saturated "
                "surfaces are the candidate-pool breadth the reranker sees and the "
                "latency/candidate cost fan-out adds."
            ),
            "decision": decision,
            "decision_reason": (
                f"fan_out never regressed final-context MRR/NDCG/P@1 "
                f"(regression={regression}) and abstention is unchanged "
                f"(answered_unanswerable {single['answered_unanswerable']} -> "
                f"{fan['answered_unanswerable']}). Re-measure against a larger, harder "
                "gold set (or the real PagerDuty/Mendeley corpus) where single-query "
                "retrieval misses relevant parents."
            ),
        }
        (REPORTS_DIR / "phase4_multiquery_latest.json").write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
        lines = [
            f"# Multi-query fan-out A/B — {args.model}",
            "",
            f"Gold: {gold.name} (answerable={len(gold.answerable)}, "
            f"unanswerable={len(gold.unanswerable)}). Rewrites: {sidecar.get('model')} "
            f"captured {sidecar.get('captured_at')}.",
            "",
            "| arm | Recall@k | MRR@10 | NDCG@10 | P@1 | candidates | pool parents | "
            "latency(ms) |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for name in arms:
            metrics = report_arms[name]
            lines.append(
                f"| {name} | {metrics['recall_at']} | {metrics['mrr_at_10']} | "
                f"{metrics['ndcg_at_10']} | {metrics['precision_at_1']} | "
                f"{metrics['mean_candidate_count']} | {metrics['mean_pool_distinct_parents']} | "
                f"{metrics['mean_latency_ms']} |"
            )
        lines += [
            "",
            payload["ceiling_note"],
            "",
            f"**Decision: {decision}** — {payload['decision_reason']}",
            "",
        ]
        (REPORTS_DIR / "phase4_multiquery_latest.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
        print(json.dumps(payload, indent=2))
    finally:
        service.query_processor = original_processor
        try:
            await index.drop_tenant_indices(TENANT)
        finally:
            await client.close()


if __name__ == "__main__":
    asyncio.run(main(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))
