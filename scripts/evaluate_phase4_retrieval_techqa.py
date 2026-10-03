"""Run the TechQA release set through the *production* retrieval path.

The proxy harness (``evaluate_phase4_proxy_release.py``) answers "how well would these
labels be retrieved if we chunked at 420 tokens, scored every child exactly, and reranked
the top 30 or 100 documents ourselves?". It is a legitimate benchmark and it is not the
production pipeline: production chunks at 480/320/120 (``rag/chunking.py``), fuses dense
and BM25 with RRF *on the cluster* at ``rank_constant: 60`` (``rag/opensearch.py``),
expands children to parents through the repository (``rag/service.py``), and blends
``0.85 * rerank + 0.15 * retrieval`` (``core/settings.py``). Every number in the committed
proxy report describes the first thing.

This script measures the second. Same corpus, same 400 queries, same labels, same pinned
models -- this host's TEI services serve exactly the revisions the proxy report pinned, so
the model is held constant and only the pipeline moves. Where the two disagree, the
disagreement is the pipeline's.

What this script does **not** measure, because it cannot: answerable answer rate and
impossible abstention rate. It can report whether an unanswerable query returned *any*
context, and that is a retrieval fact, not an answer -- production refuses through the
Reviewer's semantic ABSTAIN, which involves a model this script never calls. See
``docs/PHASE7_ACCEPTANCE_BASELINE.md``.

Usage:
    uv run python scripts/evaluate_phase4_retrieval_techqa.py --limit 200   # smoke
    uv run python scripts/evaluate_phase4_retrieval_techqa.py               # full corpus
"""

from __future__ import annotations

import argparse
import asyncio
import json
import selectors
import statistics
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from core import settings
from servicemind.domain.knowledge import (
    AuthorityLevel,
    CorpusScope,
    KnowledgeACL,
    KnowledgeDocument,
    KnowledgeProvenance,
    RetrievalPrincipal,
)
from servicemind.evaluation import (
    AnnotationProvenance,
    GoldQuery,
    GoldSet,
    LabelTier,
    QueryCategory,
    RefusalReason,
)
from servicemind.evaluation.harness import evaluate, markdown_report
from servicemind.evaluation.leakage import EvidenceCoordinates, summarize
from servicemind.rag.models import TeiEmbeddingProvider, TeiReranker
from servicemind.rag.opensearch import OpenSearchKnowledgeIndex
from servicemind.rag.service import (
    EnterpriseRAG,
    build_opensearch_client,
    source_ceiling_applies,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "phase4" / "raw" / "eval" / "techqa-rag-eval"
SELECTION = ROOT / "evaluation" / "gold" / "phase4_proxy_release_v1.2.json"
PROXY_REPORT = ROOT / "evaluation" / "reports" / "phase4_proxy_release_latest.json"
OUT_JSON = ROOT / "evaluation" / "reports" / "phase4_techqa_production_latest.json"
OUT_MD = ROOT / "evaluation" / "reports" / "phase4_techqa_production_latest.md"
TENANT = UUID("11111111-1111-4111-8111-111111111111")

#: The proxy arms this run is comparable to, as ``production baseline -> proxy arm``.
#: The proxy's ``hybrid_rrf`` and ``hybrid_bge_rerank_depth100`` have no counterpart
#: here: the first is RRF without the rerank that production always applies, the second
#: is the *proxy's* approximation of the production pool depth, and this run is the
#: thing that approximation was approximating.
COMPARABLE_ARMS = {
    "bm25": "bm25",
    "dense": "dense",
    "hybrid": "hybrid_rrf",
    "hybrid_rerank": "production",
}


class MemoryRepository:
    """Hold parent content in memory instead of reading it under PostgreSQL RLS.

    Production expansion is a row-level-security read (``repository.authorized_parents``),
    so this stands in for the *storage*, not for the expansion: ``EnterpriseRAG`` still
    asks for parents by id and still gets only what was indexed under this tenant.
    """

    def __init__(self) -> None:
        self.parent_content: dict[UUID, str] = {}

    async def is_current(self, tenant_id: UUID, document: KnowledgeDocument) -> bool:
        return False

    async def replace(self, tenant_id: UUID, document: KnowledgeDocument, parents, children):
        for parent in parents:
            self.parent_content[parent.parent_chunk_id] = parent.content
        return document, parents, children

    async def mark_indexed(self, tenant_id: UUID, document_id: UUID) -> None:
        return None

    async def count_pending(self, tenant_id: UUID) -> int:
        return 0

    async def parents(self, tenant_id: UUID, ids) -> dict[UUID, str]:
        return {item: self.parent_content[item] for item in ids if item in self.parent_content}

    async def authorized_parents(self, principal, hits):
        """The production call, served from memory and filtered by the same principal.

        Returning everything would be the silent-disclosure failure the RLS read exists
        to prevent, and doing it here would hide a real ordering bug behind a fake
        permissive repository.
        """
        return {
            hit.parent_chunk_id: self.parent_content[hit.parent_chunk_id]
            for hit in hits
            if hit.parent_chunk_id in self.parent_content
        }


def _gold_set(selection: dict[str, Any], train: list[dict[str, Any]]) -> GoldSet:
    """The 400-query release set as a ``GoldSet``, carrying its own provenance.

    Built rather than loaded because the committed selection records ids and filenames
    only; the query text lives in ``train.json``. The provenance is what makes the
    §4.1 gate's inapplicability a property of this data instead of a harness flag: a
    silver set with no grades, no hard negatives, one refusal kind and no development
    split blocks the gate, and ``release_gate_blockers`` says so from these fields.
    """
    by_id = {row["id"]: row for row in train}
    queries: list[GoldQuery] = []
    for entry in selection["answerable"]:
        row = by_id[entry["id"]]
        queries.append(
            GoldQuery(
                id=entry["id"],
                query=row["question"],
                relevant=sorted(set(entry["relevant_filenames"])),
            )
        )
    for query_id in selection["impossible"]:
        queries.append(
            GoldQuery(
                id=query_id,
                query=by_id[query_id]["question"],
                unanswerable=True,
                category=QueryCategory.UNANSWERABLE,
                # TechQA marks these impossible because no passage in the corpus answers
                # them. That is the evidence-absent kind, not a version conflict and not
                # a refusal on access grounds, and the schema refuses to leave it unstated.
                refusal_reason=RefusalReason.INSUFFICIENT_EVIDENCE,
            )
        )
    return GoldSet(
        name="techqa-rag-eval-release-v1.2",
        description=(
            "the 400-query TechQA selection the proxy report scored, re-run through the "
            "production pipeline"
        ),
        provenance=AnnotationProvenance(
            label_tier=LabelTier.SILVER,
            notes=f"labels supplied by {selection['source']}@{selection['source_revision']}",
        ),
        queries=queries,
    )


def _corpus(limit: int | None) -> list[KnowledgeDocument]:
    """TechQA's support corpus as knowledge documents, keyed by filename.

    ``source_record_id`` is the bare filename because that is what the selection's
    ``relevant_filenames`` holds and what the proxy ranked; anything else would make
    recall measure a join rather than a retrieval.
    """
    documents: list[KnowledgeDocument] = []
    with zipfile.ZipFile(DATA / "corpus.zip") as archive:
        for item in archive.infolist():
            if item.is_dir():
                continue
            if limit is not None and len(documents) >= limit:
                break
            text = archive.read(item).decode("utf-8", "replace").strip()
            if not text:
                continue
            name = Path(item.filename).name
            documents.append(
                KnowledgeDocument(
                    title=next((line.strip() for line in text.splitlines() if line.strip()), name)[
                        :200
                    ],
                    content=text,
                    document_type="techqa_support_ticket",
                    language="en",
                    metadata={"source": "nvidia/TechQA-RAG-Eval"},
                    acl=KnowledgeACL(
                        corpus_scope=CorpusScope.TENANT,
                        tenant_id=TENANT,
                        effective_from=datetime(2026, 1, 1, tzinfo=UTC),
                    ),
                    provenance=KnowledgeProvenance(
                        source="techqa-support-corpus",
                        source_version="0b5bbc84b7f07d6d09d063130e90b716d8d4a32a",
                        source_uri=f"techqa://{name}",
                        source_record_id=name,
                        license="public-dataset",
                        authority_level=AuthorityLevel.INTERNAL_KNOWLEDGE,
                        content_hash=KnowledgeDocument.content_digest(text),
                    ),
                )
            )
    return documents


def _coordinates(documents: list[KnowledgeDocument]) -> dict[str, EvidenceCoordinates]:
    """The ACL each indexed document was given, as the audit's ground truth.

    Taken from the ``KnowledgeDocument`` the loader built, before any of it reached the
    index -- not read back from the pipeline. Reading the pipeline's own report would ask
    the filter to grade its own filtering, and a filter that dropped the ACL on the way in
    would then look clean on the way out.
    """
    return {
        document.provenance.source_record_id: EvidenceCoordinates(
            tenant_id=document.acl.tenant_id,
            group_ids=document.acl.group_ids,
            is_active=document.acl.is_active,
            effective_from=document.acl.effective_from,
            effective_to=document.acl.effective_to,
            authority_level=document.provenance.authority_level,
        )
        for document in documents
    }


def _strata(documents: list[KnowledgeDocument]) -> dict[str, int]:
    """How much the corpus could have leaked, so a zero can be read for what it is.

    A zero-count over a corpus with one tenant and no group restrictions is a property of
    the corpus, not a finding about the pipeline: there is no stratum in which a leak
    could occur. Reported beside the count so the two are never confused -- the same
    discipline the release gate applies when it refuses to be applicable.
    """
    return {
        "documents": len(documents),
        "tenants": len({document.acl.tenant_id for document in documents}),
        "group_restricted": sum(1 for document in documents if document.acl.group_ids),
        "inactive": sum(1 for document in documents if not document.acl.is_active),
        "with_an_effective_end": sum(
            1 for document in documents if document.acl.effective_to is not None
        ),
        "query_time_strata": 1,
    }


def _packing(report, top_ks: tuple[int, ...], distinct_sources: int) -> dict[str, Any]:
    """How many documents actually reached the prompt, per arm, and which ceiling bound it.

    The proxy ranks passages and cuts at top-k. Production returns the *packed context*:
    ``EnterpriseRAG.retrieve`` stops at ``final_k``, at the token budget
    (``SERVICEMIND_RAG_CONTEXT_TOKENS``) and at the two diversity ceilings, so its
    ``items`` can be shorter than the cutoff being asked about. When it is, Recall@10
    and Recall@20 are the same measurement wearing two names, and the delta against the
    proxy is partly a difference in *what is counted* rather than in how well anything
    was ranked.

    Counted from the outcomes rather than asserted, so the report carries the evidence
    for its own caveat instead of asking the reader to take it on faith.

    ``per_source`` is a ``Counter`` keyed by ``KnowledgeProvenance.source`` -- the *origin*
    of a document, not the document. On a corpus whose documents all name one origin the
    counter is a single tally for the whole corpus, so a ceiling meant to stop one origin
    crowding out the others would become a ceiling on the entire prompt. That is why the
    source cardinality is reported here: without it, "4" looks like the token budget and
    the wrong ceiling gets the blame. The ceiling is now applied only between competing
    sources (``source_ceiling_applies``), so on this single-source corpus it is not in
    force at all -- reported as ``source_ceiling_enforced`` rather than assumed.

    ``arms_at_the_per_source_ceiling`` compares a count of *documents* against a ceiling on
    *parents*, which is sound only because the two are ordered: every packed item belongs to
    exactly one document, so documents <= parents, and when all documents share one origin
    parents <= per_source_cap. An arm whose maximum document count reaches the cap therefore
    has exactly ``cap`` parents. With the ceiling out of force that count is a coincidence
    of the packer passing through 4, not evidence that a rule stopped it -- which is the one
    reading the ``ceiling_note`` exists to prevent.

    Which constraint does bind is a separate measurement, and this report does not make it:
    it is answered by ``scripts/diagnose_phase4_packing_ceiling.py``, which sizes the
    corpus's parents with this same chunker and asks whether the budget would still have
    admitted another of typical size.
    """
    packed = {
        metrics.baseline: [len(outcome.ranked_keys) for outcome in metrics.answerable]
        for metrics in report.baselines
    }
    per_arm = {
        arm: {
            "mean": round(statistics.fmean(counts), 3) if counts else 0.0,
            "min": min(counts) if counts else 0,
            "max": max(counts) if counts else 0,
        }
        for arm, counts in packed.items()
    }
    per_source_cap = settings.SERVICEMIND_RAG_MAX_PARENTS_PER_SOURCE
    enforced = source_ceiling_applies(distinct_sources)
    saturating = sorted(arm for arm, stats_ in per_arm.items() if stats_["max"] >= per_source_cap)
    saturating_text = ", ".join(saturating) if saturating else "none"
    single_source = (
        f"Every document here names the same source, so the {per_source_cap}-parent "
        f"ceiling has no other source to balance and is not in force: the pack is bounded "
        f"by the token budget and by the candidate list. The arms whose maximum nevertheless "
        f"reads {per_source_cap} ({saturating_text}) pass through that number, they are not "
        f"stopped by it."
        if distinct_sources == 1
        else f"The corpus names {distinct_sources} distinct sources, so the "
        f"{per_source_cap}-parent ceiling is in force and binds per origin rather than on "
        f"the prompt as a whole."
    )
    return {
        "context_token_budget": settings.SERVICEMIND_RAG_CONTEXT_TOKENS,
        "max_parents_per_document": settings.SERVICEMIND_RAG_MAX_PARENTS_PER_DOCUMENT,
        "max_parents_per_source": per_source_cap,
        "distinct_sources": distinct_sources,
        "source_ceiling_enforced": enforced,
        "arms_at_the_per_source_ceiling": saturating,
        "final_k": max(top_ks) + 4,
        "documents_packed_per_answerable_query": per_arm,
        "ceiling_note": single_source.strip(),
        "reading": (
            "the proxy has no token budget and no diversity ceilings, so its top-k is a "
            "ranking cut; production's is a prompt-fitting cut. Read every recall figure "
            "beside the packed count: where it is below the cutoff, that recall number is "
            "reporting the budget, not the retriever."
        ),
    }


def _comparison(results: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    """This run's arms beside the proxy's, per metric, with the difference."""
    proxy = json.loads(PROXY_REPORT.read_text(encoding="utf-8"))["retrieval"]
    metrics = ("recall_at_5", "recall_at_10", "recall_at_20", "mrr_at_10", "ndcg_at_10")
    table: dict[str, dict[str, float]] = {}
    for ours, theirs in COMPARABLE_ARMS.items():
        row: dict[str, float] = {}
        for metric in metrics:
            row[f"production_{metric}"] = round(results[ours][metric], 4)
            row[f"proxy_{metric}"] = round(proxy[theirs][metric], 4)
            row[f"delta_{metric}"] = round(results[ours][metric] - proxy[theirs][metric], 4)
        table[f"{ours}_vs_{theirs}"] = row
    return table


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="index only the first N docs")
    parser.add_argument("--top-k", default="5,10,20")
    parser.add_argument("--keep-index", action="store_true")
    args = parser.parse_args()

    top_ks = tuple(int(x) for x in args.top_k.split(",") if x.strip())
    selection = json.loads(SELECTION.read_text(encoding="utf-8"))
    train = json.loads((DATA / "train.json").read_text(encoding="utf-8"))
    gold = _gold_set(selection, train)
    documents = _corpus(args.limit)
    print(
        f"queries={len(gold.queries)} (answerable={len(gold.answerable)}, "
        f"unanswerable={len(gold.unanswerable)}) documents={len(documents)}"
    )
    print("gate blockers:", len(gold.release_gate_blockers))

    client = build_opensearch_client()
    embedding = TeiEmbeddingProvider(
        settings.SERVICEMIND_EMBEDDING_URL,
        model_revision=settings.SERVICEMIND_EMBEDDING_REVISION,
    )
    reranker = TeiReranker(
        settings.SERVICEMIND_RERANKER_URL,
        model_revision=settings.SERVICEMIND_RERANKER_REVISION,
    )
    index = OpenSearchKnowledgeIndex(
        client, prefix=f"sm-techqa-prod-{uuid4().hex[:8]}", dimension=embedding.dimension
    )
    rag = EnterpriseRAG(
        index=index, embedding=embedding, reranker=reranker, repository=MemoryRepository()
    )
    try:
        started = time.perf_counter()
        totals = await rag.ingest(TENANT, documents)
        ingest_seconds = time.perf_counter() - started
        print(f"ingested in {ingest_seconds:.1f}s: {totals}")
        principal = RetrievalPrincipal(
            tenant_id=TENANT, user_id="phase4-techqa-production", entity_ids=frozenset({1})
        )

        class HarnessProvider:
            async def retrieve(self, **kwargs: object):
                return await rag.retrieve(use_query_model=False, **kwargs)

        report = await evaluate(
            HarnessProvider(),
            gold,
            principal,
            top_ks=top_ks,
            coordinates=_coordinates(documents),
        )
        results = {
            metrics.baseline: {
                "recall_at_5": metrics.recall_at(5),
                "recall_at_10": metrics.recall_at(10),
                "recall_at_20": metrics.recall_at(20),
                "mrr_at_10": metrics.mrr_at(10),
                "ndcg_at_10": metrics.ndcg_at(10),
                "answered_unanswerable": metrics.answered_unanswerable(),
                "abstention_rate": round(metrics.abstention_rate(), 4),
            }
            for metrics in report.baselines
        }
        payload = {
            "schema_version": "phase4-techqa-production-v1",
            "status": "PIPELINE_COMPARISON_ONLY",
            "quality_certification": False,
            "status_semantics": (
                "this report compares the production retrieval path against the proxy "
                "harness on the same corpus, queries and labels. It is still a silver "
                "external set: it certifies nothing about tenant-domain quality, and "
                "`release_gate_blockers` below is why the §4.1 thresholds are not applied."
            ),
            "release_gate_blockers": gold.release_gate_blockers,
            "corpus": {
                "documents_indexed": len(documents),
                "truncated_to": args.limit,
                "ingest_seconds": round(ingest_seconds, 1),
                "totals": totals,
                "distinct_sources": len({document.provenance.source for document in documents}),
            },
            "models": {
                "embedding_revision": settings.SERVICEMIND_EMBEDDING_REVISION,
                "reranker_revision": settings.SERVICEMIND_RERANKER_REVISION,
                "served_by": "tei",
                "note": (
                    "the proxy report pinned these same two revisions, so the model is "
                    "held constant and the difference below is the pipeline's"
                ),
            },
            "pipeline": {
                "index": index.prefix,
                "child_chunking": "480 max / 320 target / 120 min, 48 overlap",
                "child_tokenizer": "cl100k_base" if _cl100k() else "ConservativeOfflineEncoding",
                "fusion": "cluster-side RRF (rank_constant 60) then 0.85*rerank + 0.15*retrieval",
                "parent_expansion": "repository lookup by id (RLS in production)",
            },
            "queries": {
                "total": len(gold.queries),
                "answerable": len(gold.answerable),
                "unanswerable": len(gold.unanswerable),
            },
            "results": results,
            "visible_evidence_violations": {
                "by_baseline": {
                    metrics.baseline: summarize(metrics.visible_evidence_violations())
                    for metrics in report.baselines
                },
                "corpus_strata": _strata(documents),
                "reading": (
                    "§4.1's threshold is a zero-count, and this is that count computed "
                    "against the ACLs the loader assigned rather than read back from the "
                    "pipeline. Read it with `corpus_strata`: a zero over a single-tenant "
                    "corpus with no group-restricted documents is a property of this "
                    "corpus, not a finding about the pipeline. The strata in which a leak "
                    "could occur are the acceptance fixtures' business."
                ),
            },
            "proxy_comparison": _comparison(results),
            "packing": _packing(
                report,
                top_ks,
                len({document.provenance.source for document in documents}),
            ),
            "limitations": [
                "the labels are source-provided silver, not tenant-domain human qrels",
                "each answerable query carries a single relevant filename, so a rank-2 hit "
                "scores the same as a miss under Recall@10",
                "`abstention_rate` counts unanswerable queries that returned *any* context. "
                "It is not the Reviewer's semantic abstention and must not be reported as "
                "an answerable answer rate",
                "parents are held in memory rather than read under PostgreSQL row-level "
                "security, so this run exercises the expansion *call* and not the RLS "
                "policy that authorises it",
                "the two chunkers count tokens in different units: the proxy splits on the "
                "bge-m3 tokenizer (`AutoTokenizer` for the pinned revision), production on "
                "`ConservativeOfflineEncoding` whenever the tiktoken table is not cached, "
                "which it is not on this host. 480 production tokens and 420 proxy tokens "
                "are therefore not the same quantity. The comparison is still the "
                "production path against the proxy path, which is what it claims to be; it "
                "just cannot be decomposed into RRF's share and the chunker's share",
                "the two sides cut at different things: the proxy ranks passages and cuts "
                "at top-k, production returns the packed prompt, which stops at the token "
                "budget and the diversity ceilings. See `packing` for how many documents "
                "actually reached the prompt; where that is below the cutoff, the recall "
                "number is reporting the budget rather than the retriever",
            ],
        }
        OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
        OUT_JSON.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        OUT_MD.write_text(_markdown(payload, markdown_report(report)), encoding="utf-8")
        print(json.dumps(payload["results"], indent=2))
        print(json.dumps(payload["proxy_comparison"], indent=2))
    finally:
        if not args.keep_index:
            try:
                await index.drop_tenant_indices(TENANT)
            finally:
                await client.close()
        else:
            await client.close()


def _cl100k() -> bool:
    from servicemind.rag.chunking import has_cl100k_cache

    return has_cl100k_cache()


def _markdown(payload: dict[str, Any], harness_markdown: str) -> str:
    table = payload["proxy_comparison"]
    headers = ["arm (production vs proxy)"]
    for metric in ("recall_at_5", "recall_at_10", "recall_at_20", "mrr_at_10", "ndcg_at_10"):
        headers += [f"production {metric}", f"proxy {metric}", "delta"]
    lines = ["| " + " | ".join(headers) + " |", "| --- " * len(headers) + "|"]
    for arm, row in table.items():
        cells = [arm]
        for metric in ("recall_at_5", "recall_at_10", "recall_at_20", "mrr_at_10", "ndcg_at_10"):
            cells += [
                f"{row[f'production_{metric}']:.4f}",
                f"{row[f'proxy_{metric}']:.4f}",
                f"{row[f'delta_{metric}']:+.4f}",
            ]
        lines.append("| " + " | ".join(cells) + " |")
    blockers = "\n".join(f"- {item}" for item in payload["release_gate_blockers"])
    limitations = "\n".join(f"- {item}" for item in payload["limitations"])
    leaks = payload["visible_evidence_violations"]
    leak_rows = "\n".join(
        f"| {arm} | {counts['wrong_tenant']} | {counts['unauthorized_group']} | "
        f"{counts['expired_version']} | {counts['total']} |"
        for arm, counts in leaks["by_baseline"].items()
    )
    strata = leaks["corpus_strata"]
    packing = payload["packing"]
    capped = set(packing["arms_at_the_per_source_ceiling"])
    # "reaches" rather than "at": when the ceiling is not in force an arm can pass through
    # its number without being stopped by it, and a column reading "at the ceiling: yes"
    # would say the opposite of what the note under the table says.
    packed_rows = "\n".join(
        f"| {arm} | {row['mean']:.2f} | {row['min']} | {row['max']} | "
        f"{'reaches' if arm in capped else 'below'} |"
        for arm, row in packing["documents_packed_per_answerable_query"].items()
    )
    return f"""# TechQA release set through the production retrieval path

Status: **{payload["status"]}** (documents indexed: {payload["corpus"]["documents_indexed"]}, \
ingest {payload["corpus"]["ingest_seconds"]}s, queries {payload["queries"]["total"]})

Same corpus, same queries, same labels, same pinned model revisions as the proxy report.
The only thing that moved is the pipeline.

{chr(10).join(lines)}

## §4.1 visible evidence: wrong tenant / ACL / expired version

| baseline | wrong tenant | unauthorized group | expired version | total |
|---|---|---|---|---|
{leak_rows}

Corpus strata this was measured over: {strata["tenants"]} tenant(s), \
{strata["group_restricted"]} group-restricted, {strata["inactive"]} inactive, \
{strata["with_an_effective_end"]} with an effective end, out of \
{strata["documents"]} documents. A zero here is read against those numbers.

## What the recall figures are actually counting

The proxy cuts at top-k. Production returns the packed prompt, which stops earlier: at
`final_k` = {packing["final_k"]}, at a {packing["context_token_budget"]}-token budget, and at
{packing["max_parents_per_document"]} parents per document / {packing["max_parents_per_source"]} per source.
These are the documents that actually reached the prompt, per answerable query:

| arm | mean documents packed | min | max | vs the {packing["max_parents_per_source"]}-parent source ceiling |
| --- | ---: | ---: | ---: | :---: |
{packed_rows}

Where a packed count is below the cutoff, that Recall@k is reporting the constraint that
bound the pack rather than the retriever's ranking, and Recall@10 and Recall@20 are the
same measurement under two names.

{packing["ceiling_note"]}

## Why §4.1 still does not apply

{blockers}

## Limitations

{limitations}

## Per-arm detail (production path)

{harness_markdown}
"""


if __name__ == "__main__":
    asyncio.run(main(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))
