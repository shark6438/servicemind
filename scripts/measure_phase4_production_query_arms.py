"""Measure the production *query arm*, and the funnel depth, on the TechQA release set.

Every committed TechQA number was measured with ``use_query_model=False``
(``scripts/evaluate_phase4_retrieval_techqa.py``'s ``HarnessProvider``), and none of them
enabled ``use_rewrites``. Production does neither of those things: the graph node calls
``rag.retrieve(..., use_query_model=True, use_rewrites=settings.SERVICEMIND_RAG_MULTI_QUERY)``
with that setting pinned ``True`` (``core/settings.py``: "the rewrite stage always runs").
So the measurement harness and the deployment disagree about what the production query arm
*is*, and no committed number says which way that disagreement moves retrieval.

This script closes that gap without inventing anything. It:

1. runs the real ``QueryProcessor`` (the same object ``rag.service`` imports) over the 400
   release queries and freezes the result in ``evaluation/gold/techqa_rewrites.v1.json`` --
   the production rewrite stage's actual output, captured once and committed, in the same
   way ``gold_rewrites.v1.json`` was captured for the committed gold set;
2. installs a canned processor that replays that capture, so every arm below runs the
   *identical* processed query and the only thing that moves is the fan-out flag or the
   funnel depth;
3. ingests the 28,481-document corpus once into a named, kept index and runs every arm
   against it, so no arm is measured on a different index than another.

The arms:

* ``c0_off``  -- deterministic query processing, no fan-out. This is the arm every
  committed TechQA number was measured on; it is re-run here as the *fidelity anchor*.
  If it does not reproduce the published ``hybrid_rerank`` recall, this script is
  measuring something other than the published pipeline and no other row here is
  readable.
* ``c0_sq``   -- production query normalisation (LLM), fan-out off.
* ``c0_mq``   -- production query normalisation **and** fan-out on: the deployment's arm.
* ``c0_sq_ck{200,400}`` / ``c0_mq_ck200`` -- the same query arms with the RRF funnel
  widened, which is the only lever that can lift the in-pool ceiling.

The index is deliberately *kept* (``--drop`` to remove it). Re-ingesting 28k documents
costs ~54 minutes and buys nothing on a second run; the arms fan out at query time.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import selectors
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from servicemind.domain.knowledge import (
    KnowledgeQuery,
    QueryProvenance,
    RetrievalMode,
    RetrievalPrincipal,
)
from servicemind.evaluation.harness import Baseline, evaluate
from servicemind.evaluation.metrics import mrr_at_k, recall_at_k
from servicemind.rag.models import TeiEmbeddingProvider, TeiReranker
from servicemind.rag.opensearch import OpenSearchKnowledgeIndex
from servicemind.rag.query import QueryProcessor
from servicemind.rag.service import EnterpriseRAG, build_opensearch_client

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/phase4/raw/eval/techqa-rag-eval"
RELEASE_SCRIPT = ROOT / "scripts/evaluate_phase4_retrieval_techqa.py"
#: This file, as an input to its own report. A report that does not bind its producer
#: outlives it: edit the measurement and the committed numbers still read as current.
PRODUCER = Path(__file__).resolve()
PRODUCTION_REPORT = ROOT / "evaluation/reports/phase4_techqa_production_latest.json"
SELECTION = ROOT / "evaluation/gold/phase4_proxy_release_v1.2.json"
REWRITE_CACHE = ROOT / "evaluation/gold/techqa_rewrites.v1.json"
OUT_JSON = ROOT / "evaluation/reports/phase4_query_arm_funnel_latest.json"
OUT_MD = ROOT / "evaluation/reports/phase4_query_arm_funnel_latest.md"
#: Per-arm progress, rewritten as each arm finishes. Not a deliverable: it exists so an
#: interrupted long run is inspectable without re-deriving anything from the log.
PARTIAL = ROOT / "evaluation/reports/.phase4_query_arm_funnel_partial.json"

TENANT = UUID("11111111-1111-4111-8111-111111111111")
#: Named, not random: the index outlives the process so a second measurement run reuses it.
INDEX_PREFIX = "sm-techqa-arms-v1"
TOP_KS = (5, 10, 20)
#: The production arm shape: hybrid, reranked. ``mode``/``run_rerank`` are the harness's
#: isolation axes; this run holds them fixed and moves the query arm instead.
PRODUCTION = Baseline(name="production", mode=RetrievalMode.HYBRID, run_rerank=True)

ARMS: tuple[dict[str, Any], ...] = (
    {
        "name": "c0_off",
        "use_query_model": False,
        "use_rewrites": False,
        "candidate_k": None,
        "reading": (
            "the arm every committed TechQA number was measured on (deterministic query "
            "processing, no fan-out); present as the fidelity anchor"
        ),
    },
    {
        "name": "c0_sq",
        "use_query_model": True,
        "use_rewrites": False,
        "candidate_k": None,
        "reading": "production query normalisation, fan-out off",
    },
    {
        "name": "c0_mq",
        "use_query_model": True,
        "use_rewrites": True,
        "candidate_k": None,
        "reading": "the deployment's arm: production normalisation with multi-query fan-out",
    },
    {
        "name": "c0_sq_ck200",
        "use_query_model": True,
        "use_rewrites": False,
        "candidate_k": 200,
        "reading": "as c0_sq with the RRF funnel widened to 200",
    },
    {
        "name": "c0_sq_ck400",
        "use_query_model": True,
        "use_rewrites": False,
        "candidate_k": 400,
        "reading": "as c0_sq with the RRF funnel widened to 400",
    },
    {
        "name": "c0_mq_ck200",
        "use_query_model": True,
        "use_rewrites": True,
        "candidate_k": 200,
        "reading": "as c0_mq with the RRF funnel widened to 200",
    },
    {
        "name": "c0_anchor",
        "use_query_model": True,
        "use_rewrites": True,
        "candidate_k": None,
        "anchor_the_question": True,
        "reading": (
            "the deployment's arm with one change: the text the *question* was written in "
            "is kept as the searched text and the model's normalisation joins the fan-out "
            "as one more paraphrase. `QueryProcessor` replaces the question with the "
            "model's rewording, so today the only text any channel sees is the model's -- "
            "which is why enabling the model loses recall instead of adding it. This arm "
            "measures what making the model's contribution additive would be worth."
        ),
    },
)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_release_module():
    """Import the frozen release harness for its corpus/gold loaders.

    Re-implementing ``_corpus``/``_gold_set`` here would let the two drift, and the
    drift would be invisible: the report would name the release set while loading
    something else. Importing keeps one definition. The module is never mutated, and
    its digest is recorded in the report so a change to it invalidates this one
    (``--check`` re-derives the digest).
    """
    spec = importlib.util.spec_from_file_location("phase4_release_loader", RELEASE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CannedQueryProcessor:
    """Replay the captured production rewrite stage, keyed by the raw query text.

    Raises rather than falling back when a query is missing: a silent fallback would
    turn a capture gap into a retrieval result, and the arm would then be a mixture of
    the production query arm and the deterministic one without saying so.
    """

    def __init__(self, mapping: dict[str, KnowledgeQuery]) -> None:
        self.mapping = mapping

    async def process(
        self, query: str, *, use_model: bool = True, model_query: str | None = None
    ) -> KnowledgeQuery:
        try:
            return self.mapping[query]
        except KeyError:  # pragma: no cover - the capture covers every gold query
            raise KeyError(f"no captured processed query for: {query!r}") from None


class KeptIndexRepository:
    """Parent expansion read from the index itself, for a run that does not ingest.

    The production parent read is ``repository.authorized_parents`` under PostgreSQL
    RLS. The thin ``MemoryRepository`` stands in for that storage, and it is filled *by
    ingest* -- so on a run that reuses an index and skips ingest it is empty, every hit
    is dropped by the expansion, and the arms report recall 0 and packed 0. That is not
    a crash; it renders as a measurement, which is why it has to be fixed rather than
    tolerated. Parent ids are ``uuid4`` minted at ingest time and are therefore *not*
    reproducible from the corpus, so the only surviving copy of the content is the one
    the ingestion wrote: the index's parent alias.

    ``replace`` returns its arguments untouched, exactly as ``MemoryRepository`` does:
    the production repository is what canonicalizes document ids, and neither thin
    substitute claims to.
    """

    def __init__(self, index: OpenSearchKnowledgeIndex) -> None:
        self.index = index

    async def is_current(self, tenant_id: UUID, document: object) -> bool:
        return False

    async def replace(self, tenant_id: UUID, document, parents, children):
        return document, parents, children

    async def mark_indexed(self, tenant_id: UUID, document_id: UUID) -> None:
        return None

    async def count_pending(self, tenant_id: UUID) -> int:
        return 0

    async def parents(self, tenant_id: UUID, ids) -> dict[UUID, str]:
        return await self.index.parents(tenant_id, [value for value in ids])

    async def authorized_parents(self, principal, hits) -> dict[UUID, str]:
        wanted = {hit.parent_chunk_id for hit in hits}
        found = await self.index.parents(principal.tenant_id, sorted(wanted))
        return {key: value for key, value in found.items() if key in wanted}


def _knowledge_query(
    entry: dict[str, Any],
    raw: str,
    *,
    keep_rewrites: bool,
    anchor_the_question: bool = False,
) -> KnowledgeQuery:
    """One captured query, carrying who actually produced it.

    ``provenance`` is read out of the capture rather than left to the field's default.
    The default is "deterministic", so a replay built from an unflagged capture would
    stamp deterministic provenance onto the model's own text -- quietly turning the
    arm that exists to measure the model into a claim about the fallback.
    """
    deterministic = " ".join(raw.split())
    model_normalization = entry.get("model_normalized_query")
    rewrites = list(entry["rewritten_queries"]) if keep_rewrites else []
    if anchor_the_question:
        # The shipped configuration: the question the user typed is what every channel
        # searches, and the model's normalization is offered to the fan-out beside it --
        # ``lexical_variants`` puts it first, so the platform cap cannot drop it in favour
        # of a paraphrase of the paraphrase.
        return KnowledgeQuery(
            raw_query=raw,
            normalized_query=deterministic,
            model_normalized_query=model_normalization,
            rewritten_queries=rewrites,
            identifiers=list(entry.get("identifiers", [])),
            entities=list(entry.get("entities", [])),
            intent=entry["intent"],
            language=entry["language"],
            provenance=QueryProvenance(entry["provenance"]),
        )
    # The pre-fix arms searched the model's normalization *in place of* the question,
    # which is the behaviour the anchor arm exists to be compared against. A capture
    # entry with no model normalization (the fallback) searched the question, which is
    # what the deterministic arm measured.
    return KnowledgeQuery(
        raw_query=raw,
        normalized_query=model_normalization or deterministic,
        rewritten_queries=rewrites,
        identifiers=list(entry.get("identifiers", [])),
        entities=list(entry.get("entities", [])),
        intent=entry["intent"],
        language=entry["language"],
        provenance=QueryProvenance(entry["provenance"]),
    )


def _replay_mapping(
    capture: dict[str, Any], *, keep_rewrites: bool, anchor_the_question: bool = False
) -> dict[str, KnowledgeQuery]:
    """The capture, re-keyed by the question the harness actually asks with.

    The capture is keyed by query id; the harness hands the processor ``gold_query.query``
    and the processor looks the mapping up by that string. Keying the mapping by id is a
    KeyError on the first arm that uses the model -- and it stayed invisible because the
    arms are ordered with the deterministic one first.
    """
    return {
        entry["query"]: _knowledge_query(
            entry,
            entry["query"],
            keep_rewrites=keep_rewrites,
            anchor_the_question=anchor_the_question,
        )
        for entry in capture["queries"].values()
    }


def _cache_reuse(cached: dict[str, Any], expected: dict[str, str]) -> str | None:
    """Why the committed capture may be reused, or ``None`` if it may not.

    A capture that does not carry provenance cannot support the claim every arm below
    makes about it: ``QueryProcessor`` reports a swallowed model failure as an ordinary
    deterministic query, so an unflagged capture may be the fallback wearing the model's
    name. Re-using one would publish that as the production stage's own output -- which
    is exactly what the flag was added to stop. The query texts are checked as well, so
    a capture of a different release set is not silently a capture of this one.
    """
    queries = cached.get("queries", {})
    if not all(
        isinstance(entry, dict) and "provenance" in entry and "model_normalized_query" in entry
        for entry in queries.values()
    ):
        return None
    if set(queries) != set(expected) or any(
        queries[key]["query"] != value for key, value in expected.items()
    ):
        return None
    return f"rewrite cache reused: {REWRITE_CACHE} ({len(queries)} queries)"


async def capture_rewrites(gold, *, concurrency: int, force: bool) -> dict[str, Any]:
    """Run the production rewrite stage over the release queries and freeze the output."""
    expected = {query.id: query.query for query in gold.queries}
    if REWRITE_CACHE.exists() and not force:
        cached = json.loads(REWRITE_CACHE.read_text(encoding="utf-8"))
        reason = _cache_reuse(cached, expected)
        if reason is not None:
            print(reason)
            return cached
        print("rewrite cache is unusable for this run (unflagged or stale); recapturing")

    processor = QueryProcessor()
    semaphore = asyncio.Semaphore(concurrency)
    failures: list[str] = []

    async def one(query_id: str, text: str) -> tuple[str, dict[str, Any]]:
        async with semaphore:
            processed = await processor.process(text, use_model=True)
            return query_id, {
                "query": text,
                # The anchor the processor chose, and the model's own output beside it.
                # Recording the model's words under their own field is what lets this
                # capture be replayed under every arm: the combined text the search path
                # builds is a decision, and a capture that stored the decision could only
                # ever replay the arm it was recorded for.
                "normalized_query": processed.normalized_query,
                "model_normalized_query": processed.model_normalized_query,
                "rewritten_queries": list(processed.rewritten_queries),
                "identifiers": list(processed.identifiers),
                "entities": list(processed.entities),
                "intent": processed.intent.value,
                "language": processed.language,
                "provenance": processed.provenance.value,
            }

    started = time.perf_counter()
    results = await asyncio.gather(
        *(one(query_id, text) for query_id, text in expected.items()), return_exceptions=True
    )
    captured: dict[str, dict[str, Any]] = {}
    for query_id, value in zip(expected, results, strict=True):
        if isinstance(value, BaseException):
            failures.append(f"{query_id}: {type(value).__name__}: {value}")
            continue
        captured[value[0]] = value[1]
    if failures:
        # Partial capture is not a smaller measurement, it is a different one: the
        # missing queries would silently run the deterministic arm inside the
        # production arm. Nothing is written and the caller fails.
        raise RuntimeError(f"{len(failures)} queries failed to capture: {failures[:5]}")

    from core import settings

    payload = {
        "schema_version": "phase4-techqa-rewrites-v1",
        "captured_at": datetime.now(UTC).isoformat(),
        "model": settings.DEFAULT_MODEL,
        "processor": "servicemind.rag.query.QueryProcessor",
        "release_set": "techqa-rag-eval-release-v1.2",
        "note": (
            "the production query-rewrite stage's real output over the TechQA release "
            "queries, captured once and committed so the production query arm is "
            "reproducible offline. No secrets: only the model's rewrites of public "
            "dataset questions are stored."
        ),
        "capture_seconds": round(time.perf_counter() - started, 1),
        "concurrency": concurrency,
        "queries": captured,
    }
    REWRITE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    REWRITE_CACHE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    empty = [key for key, value in captured.items() if not value["rewritten_queries"]]
    print(
        f"captured {len(captured)} rewrites in {payload['capture_seconds']}s; "
        f"{len(empty)} carried no rewrite ({len(empty) / len(captured):.1%})"
    )
    return payload


class _ArmProvider:
    """One arm's retrieval call, holding the harness signature and adding the arm's knobs."""

    def __init__(self, rag: EnterpriseRAG, arm: dict[str, Any]) -> None:
        self.rag = rag
        self.arm = arm
        self.calls = 0

    async def retrieve(self, **kwargs: object):
        self.calls += 1
        candidate_k = self.arm["candidate_k"]
        return await self.rag.retrieve(
            use_query_model=self.arm["use_query_model"],
            use_rewrites=self.arm["use_rewrites"],
            **({"candidate_k": candidate_k} if candidate_k else {}),
            **kwargs,
        )


def _arm_metrics(metrics, elapsed: float) -> dict[str, Any]:
    answerable = metrics.answerable
    packed = [len(outcome.ranked_keys) for outcome in answerable]
    candidates = [outcome.candidate_count for outcome in answerable]
    latencies = sorted(outcome.latency_ms for outcome in answerable)
    distinct_recalls = {cutoff: round(metrics.recall_at(cutoff), 4) for cutoff in TOP_KS}
    identical: list[tuple[int, int]] = []
    for i, left in enumerate(TOP_KS):
        for right in TOP_KS[i + 1 :]:
            if distinct_recalls[left] == distinct_recalls[right]:
                identical.append((left, right))
    return {
        "recall_at_5": distinct_recalls[5],
        "recall_at_10": distinct_recalls[10],
        "recall_at_20": distinct_recalls[20],
        "mrr_at_10": round(metrics.mrr_at(10), 4),
        "ndcg_at_10": round(metrics.ndcg_at(10), 4),
        "answerable": len(answerable),
        "unanswerable": len(metrics.unanswerable),
        "answered_unanswerable": metrics.answered_unanswerable(),
        "packed_documents": {
            "min": min(packed) if packed else 0,
            "p50": statistics.median(packed) if packed else 0,
            "max": max(packed) if packed else 0,
            "mean": round(statistics.fmean(packed), 2) if packed else 0.0,
        },
        "candidates": {
            "min": min(candidates) if candidates else 0,
            "p50": statistics.median(candidates) if candidates else 0,
            "max": max(candidates) if candidates else 0,
        },
        "latency_ms": {
            "p50": round(statistics.median(latencies), 1) if latencies else 0.0,
            "p95": round(latencies[int(0.95 * (len(latencies) - 1))], 1) if latencies else 0.0,
        },
        "cutoffs_the_pack_cannot_tell_apart": [f"{left}=={right}" for left, right in identical],
        "arm_seconds": round(elapsed, 1),
    }


def _control_repeatability(first, second) -> dict[str, Any]:
    """What a second pass of the control arm reproduces, query by query.

    The control arm is this table's fidelity anchor: the published number is compared
    through it and every other row is read as a delta against it, so what it repeats is
    what the table can resolve. Comparing the two passes' averages would not measure
    that -- a query that gains a rank and one that loses a rank cancel out -- so the
    comparison is per query, and the cutoffs are counted apart from the rank order: a
    query whose relevant document stays inside the top ten but moves within it changes
    ``MRR`` and not ``R@10``, while only a crossing of the tenth rank can move a
    headline this table reports.
    """
    left = {outcome.query_id: outcome for outcome in first.answerable}
    right = {outcome.query_id: outcome for outcome in second.answerable}
    shared = sorted(set(left) & set(right))
    moved_at_5 = moved_at_10 = moved_rank = 0
    for query_id in shared:
        before, after = left[query_id], right[query_id]
        relevant = set(before.relevant)
        moved_at_5 += recall_at_k(before.ranked_keys, relevant, 5) != recall_at_k(
            after.ranked_keys, relevant, 5
        )
        moved_at_10 += recall_at_k(before.ranked_keys, relevant, 10) != recall_at_k(
            after.ranked_keys, relevant, 10
        )
        moved_rank += round(mrr_at_k(before.ranked_keys, relevant, 10), 6) != round(
            mrr_at_k(after.ranked_keys, relevant, 10), 6
        )
    return {
        "queries_compared": len(shared),
        "queries_in_one_pass_only": sorted(set(left) ^ set(right)),
        "recall_at_5_moved": moved_at_5,
        "recall_at_10_moved": moved_at_10,
        "reciprocal_rank_moved": moved_rank,
        "reading": (
            "The same arm, the same index, the same capture and the same principal; the "
            "only difference between the two columns is the pass. A delta at or below "
            "what moved here is not a difference this table can resolve, and where the "
            "cutoff columns moved while the rank column did not, what this stack does "
            "not repeat is the order of near-tied documents rather than which documents "
            "it retrieved."
        ),
    }


def _select_arms(names: list[str] | None) -> tuple[dict[str, Any], ...]:
    """The arms this run measures, refusing a selection no reading could be drawn from.

    A subset is for iterating on one lever without paying for the other six, so it is
    legitimate -- but every row in this table is read relative to ``c0_off`` (it is the
    fidelity anchor the published numbers are compared through), and the payload is
    compared with a profile by name. Both are refused here rather than discovered when
    the anchor block raises halfway through a build.
    """
    if not names:
        return ARMS
    known = {arm["name"]: arm for arm in ARMS}
    unknown = [name for name in names if name not in known]
    if unknown:
        raise SystemExit(f"unknown arms {unknown}; known: {sorted(known)}")
    if "c0_off" not in names:
        raise SystemExit("a subset must include c0_off: every row is read relative to it")
    return tuple(known[name] for name in names)


async def measure(
    *,
    force_capture: bool,
    concurrency: int,
    drop: bool,
    reuse_index: bool = False,
    repeat_control: bool = False,
    arms: tuple[dict[str, Any], ...] = ARMS,
) -> dict[str, Any]:
    release = _load_release_module()
    selection = json.loads(SELECTION.read_text(encoding="utf-8"))
    train = json.loads((DATA / "train.json").read_text(encoding="utf-8"))
    gold = release._gold_set(selection, train)
    documents = release._corpus(None)
    print(f"queries={len(gold.queries)} documents={len(documents)}")

    capture = await capture_rewrites(gold, concurrency=concurrency, force=force_capture)

    from core import settings

    client = build_opensearch_client()
    embedding = TeiEmbeddingProvider(
        settings.SERVICEMIND_EMBEDDING_URL,
        model_revision=settings.SERVICEMIND_EMBEDDING_REVISION,
    )
    reranker = TeiReranker(
        settings.SERVICEMIND_RERANKER_URL,
        model_revision=settings.SERVICEMIND_RERANKER_REVISION,
    )
    index = OpenSearchKnowledgeIndex(client, prefix=INDEX_PREFIX, dimension=embedding.dimension)
    rag = EnterpriseRAG(
        index=index,
        embedding=embedding,
        reranker=reranker,
        repository=KeptIndexRepository(index),  # type: ignore[arg-type]
    )
    principal = RetrievalPrincipal(
        tenant_id=TENANT, user_id="phase4-techqa-query-arms", entity_ids=frozenset({1})
    )
    coordinates = release._coordinates(documents)

    # The corpus is 28,481 documents and indexing it costs ~53 minutes. The script keeps
    # the index for exactly that reason, so a re-run after a fault must be able to use it
    # -- re-ingesting buys nothing when the corpus digest has not moved. What it costs is
    # the guarantee that the index on the cluster is the one this corpus builds, so the
    # count is read back either way and the fidelity anchor is what validates the reuse.
    if reuse_index:
        totals: dict[str, int] = {}
        ingest_seconds: float | None = None
    else:
        started = time.perf_counter()
        totals = await rag.ingest(TENANT, documents)
        ingest_seconds = round(time.perf_counter() - started, 1)
        print(f"ingested in {ingest_seconds}s: {totals}")

    # Read back the generation the readers actually use, not every index under the
    # prefix. A kept index is only a substitute for ingesting if its alias still
    # resolves -- and the expansion in ``KeptIndexRepository`` reads *parent* documents,
    # so an empty parent generation is the one state that makes every arm report zero
    # for a reason the report cannot see. Counting the alias rather than a glob is also
    # what makes the two numbers meaningful: they are this tenant's active generation,
    # parents and children separately.
    active = await index.active_generation(TENANT)
    if active is None:
        raise RuntimeError(
            f"no active generation for {INDEX_PREFIX}-tenant-{TENANT}: an index no reader "
            "can reach is not a reused one, and every arm below would report zero"
        )
    children_on_cluster = int((await client.count(index=index.child_alias(TENANT))).get("count", 0))
    parents_on_cluster = int((await client.count(index=index.parent_alias(TENANT))).get("count", 0))
    print(
        f"active generation {active}: {children_on_cluster} children, {parents_on_cluster} parents",
        flush=True,
    )
    if parents_on_cluster == 0:
        raise RuntimeError(
            f"the active parent generation for {TENANT} is empty: the arms would search "
            "fine and then drop every hit at parent expansion, which reports as recall 0"
        )

    # Install the canned processor for the duration of the arms and restore the real
    # one after: `service.query_processor` is a module-level singleton, and leaving a
    # replay object on it would make every later caller in this process lie.
    import servicemind.rag.service as service

    original = service.query_processor

    def _replay(keep_rewrites: bool, *, anchor: bool = False) -> CannedQueryProcessor:
        return CannedQueryProcessor(
            _replay_mapping(capture, keep_rewrites=keep_rewrites, anchor_the_question=anchor)
        )

    # Separate replay objects, not one: the fan-out arms need the rewrites present on
    # the processed query and the single-query arms must see none, or `use_rewrites=False`
    # would be the only thing separating them and the capture would go untested. The
    # anchored one is a third object because it differs in the *query text* it hands
    # over, not in a retrieval flag -- sharing an object here would silently make the
    # anchor arm a re-run of c0_mq.
    authoritative = _replay(True)
    stripped = _replay(False)
    anchored = _replay(True, anchor=True)
    arm_results: dict[str, Any] = {}
    control_metrics = None
    try:
        for arm in arms:
            if not arm["use_query_model"]:
                service.query_processor = original
            elif arm.get("anchor_the_question"):
                service.query_processor = anchored
            else:
                service.query_processor = authoritative if arm["use_rewrites"] else stripped
            provider = _ArmProvider(rag, arm)
            arm_started = time.perf_counter()
            report = await evaluate(
                provider,
                gold,
                principal,
                top_ks=TOP_KS,
                baselines=(PRODUCTION,),
                coordinates=coordinates,
            )
            elapsed = time.perf_counter() - arm_started
            metrics = report.metrics(PRODUCTION.name)
            if arm["name"] == "c0_off":
                # Held as the metrics object rather than the rendered row: the repeat
                # pass below compares outcomes query by query.
                control_metrics = metrics
            arm_results[arm["name"]] = {
                **_arm_metrics(metrics, elapsed),
                "use_query_model": arm["use_query_model"],
                "use_rewrites": arm["use_rewrites"],
                "candidate_k": arm["candidate_k"],
                "reading": arm["reading"],
                "visible_evidence_violations": len(metrics.visible_evidence_violations()),
            }
            print(
                f"{arm['name']:>13}  R@5={arm_results[arm['name']]['recall_at_5']:.4f} "
                f"R@10={arm_results[arm['name']]['recall_at_10']:.4f} "
                f"R@20={arm_results[arm['name']]['recall_at_20']:.4f} "
                f"MRR@10={arm_results[arm['name']]['mrr_at_10']:.4f} "
                f"packed_max={arm_results[arm['name']]['packed_documents']['max']} "
                f"({elapsed:.0f}s)",
                flush=True,
            )
            # Written after every arm: a two-hour run that is stopped at the timeout
            # would otherwise leave nothing, and the arms already finished are the
            # expensive part. The committed report is only written by `main`.
            PARTIAL.write_text(
                json.dumps({"arms": arm_results}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    finally:
        service.query_processor = original

    control_repeat: dict[str, Any] | None = None
    if repeat_control and control_metrics is not None:
        # Same provider, same processor, same principal, same capture as the control row
        # above; the only thing that differs is that it is a second pass. What this buys
        # is the scale below which two arms in the table above stop being separable.
        print("repeating the control arm to measure its own reproducibility", flush=True)
        control_arm = next(arm for arm in arms if arm["name"] == "c0_off")
        try:
            service.query_processor = original
            started = time.perf_counter()
            repeated = await evaluate(
                _ArmProvider(rag, control_arm),
                gold,
                principal,
                top_ks=TOP_KS,
                baselines=(PRODUCTION,),
                coordinates=coordinates,
            )
            repeat_seconds = time.perf_counter() - started
        finally:
            service.query_processor = original
        second_pass = repeated.metrics(PRODUCTION.name)
        control_repeat = {
            "first": arm_results["c0_off"],
            "second": _arm_metrics(second_pass, repeat_seconds),
            **_control_repeatability(control_metrics, second_pass),
        }
        print(
            f"control repeat  R@5={control_repeat['second']['recall_at_5']:.4f} "
            f"R@10={control_repeat['second']['recall_at_10']:.4f} "
            f"moved_r5={control_repeat['recall_at_5_moved']} "
            f"moved_r10={control_repeat['recall_at_10_moved']} "
            f"moved_rank={control_repeat['reciprocal_rank_moved']} ({repeat_seconds:.0f}s)",
            flush=True,
        )

    published = json.loads(PRODUCTION_REPORT.read_text(encoding="utf-8"))
    published_arm = published["results"]["hybrid_rerank"]
    anchor = arm_results["c0_off"]
    anchors = {
        "published_arm": "phase4_techqa_production_latest.json results.hybrid_rerank",
        "published_recall_at_10": published_arm["recall_at_10"],
        "reproduced_recall_at_10": anchor["recall_at_10"],
        "delta_recall_at_10": round(anchor["recall_at_10"] - published_arm["recall_at_10"], 4),
        "published_packed_max": published["packing"]["documents_packed_per_answerable_query"][
            "hybrid_rerank"
        ]["max"],
        "reproduced_packed_max": anchor["packed_documents"]["max"],
        "reading": (
            "c0_off replays the published run's query arm on this run's index. A delta of "
            "0.0000 means the two builds agree and the published recall is this run's "
            "baseline. A non-zero delta is a difference between two *index builds*, not "
            "between two arms: every arm in this table shares one index and one capture. "
            "What the control repeats is measured, not assumed -- see 控制臂自身的可复现度 "
            "below -- and a delta at or below the movement reported there is not a "
            "difference this table can resolve."
        ),
    }

    payload: dict[str, Any] = {
        "schema_version": "phase4-query-arm-funnel-v1",
        "status": "QUERY_ARM_MEASUREMENT",
        "quality_certification": False,
        "generated_at": datetime.now(UTC).isoformat(),
        "status_semantics": (
            "this report measures the *production query arm* and the funnel depth on the "
            "TechQA release set through the production retrieval path. The labels remain "
            "silver, so it certifies nothing about tenant-domain quality; it answers a "
            "narrower question -- which query arm the deployed pipeline actually runs, "
            "and how much the measured numbers depend on the harness's choice of arm."
        ),
        "measurement_gap": {
            "harness_arm": "use_query_model=False, use_rewrites=False",
            "production_arm": (
                "use_query_model=True, use_rewrites=settings.SERVICEMIND_RAG_MULTI_QUERY (True)"
            ),
            "harness_call_site": (
                "scripts/evaluate_phase4_retrieval_techqa.py HarnessProvider.retrieve"
            ),
            "production_call_site": "src/servicemind/agents/knowledge.py retrieve(...)",
        },
        "inputs": {
            "producer": {
                "path": str(PRODUCER.relative_to(ROOT)),
                "sha256": _digest(PRODUCER),
                "tracked": True,
            },
            "release_loader": {
                "path": str(RELEASE_SCRIPT.relative_to(ROOT)),
                "sha256": _digest(RELEASE_SCRIPT),
                "tracked": True,
            },
            "rewrite_cache": {
                "path": str(REWRITE_CACHE.relative_to(ROOT)),
                "sha256": _digest(REWRITE_CACHE),
                "captured_at": capture["captured_at"],
                "model": capture["model"],
                "capture_seconds": capture["capture_seconds"],
                "queries_with_no_rewrite": sum(
                    1 for value in capture["queries"].values() if not value["rewritten_queries"]
                ),
                "queries_normalised_by_the_model": sum(
                    1
                    for value in capture["queries"].values()
                    if value.get("provenance") == QueryProvenance.MODEL.value
                ),
                "queries_served_by_the_deterministic_fallback": sum(
                    1
                    for value in capture["queries"].values()
                    if value.get("provenance") == QueryProvenance.DETERMINISTIC.value
                ),
                "tracked": False,
                "note": (
                    "the capture holds the model's rewrites of public dataset questions "
                    "only; it carries no secret, but it is regenerable and is not tracked"
                ),
            },
            "selection": {
                "path": str(SELECTION.relative_to(ROOT)),
                "sha256": _digest(SELECTION),
                "tracked": True,
            },
            "corpus": {
                "path": str((DATA / "corpus.zip").relative_to(ROOT)),
                "sha256": _digest(DATA / "corpus.zip"),
            },
            "production_report": {
                "path": str(PRODUCTION_REPORT.relative_to(ROOT)),
                "sha256": _digest(PRODUCTION_REPORT),
            },
        },
        "corpus": {
            "documents_indexed": len(documents),
            "ingest_seconds": ingest_seconds,
            "totals": totals,
            "index_prefix": INDEX_PREFIX,
            "index_kept": not drop,
            "index_reused": reuse_index,
            "arms_measured": [arm["name"] for arm in arms],
            "arms_measured_is_the_full_table": tuple(arms) == ARMS,
            "active_generation": active,
            "children_on_cluster": children_on_cluster,
            "parents_on_cluster": parents_on_cluster,
        },
        "models": {
            "embedding_revision": settings.SERVICEMIND_EMBEDDING_REVISION,
            "reranker_revision": settings.SERVICEMIND_RERANKER_REVISION,
            "rewrite_model": capture["model"],
            "default_candidate_k": settings.SERVICEMIND_RAG_CANDIDATE_K,
            "default_final_k_production": 8,
            "harness_final_k": max(TOP_KS) + 4,
        },
        "queries": {
            "total": len(gold.queries),
            "answerable": len(gold.answerable),
            "unanswerable": len(gold.unanswerable),
        },
        "arms": arm_results,
        "fidelity_anchor": anchors,
        "control_repeatability": control_repeat,
        "limitations": [
            "the labels are source-provided silver, not tenant-domain human qrels, so no "
            "row here is a release threshold",
            "each answerable query carries a single relevant filename, so a rank-2 hit "
            "scores the same as a miss under Recall@k",
            "the harness packs to final_k = max(top_ks) + 4 = 24 while production's first "
            "round packs to 8; every packed count here is the harness's, and the rows "
            "whose cutoffs collapse are reporting the pack length rather than the "
            "retriever",
            "parents are read from the index's parent alias rather than under "
            "PostgreSQL row-level security, so this run exercises the expansion call, "
            "its tenant scoping and its ACL filter, but not the RLS policy itself",
            "the rewrite capture is one sample of a non-deterministic stage: a second "
            "capture would differ in wording. The vendored sha256 is what makes this "
            "particular sample reproducible, not the stage",
        ]
        + (
            [
                "the control arm's own reproducibility was measured in this run (see "
                "控制臂自身的可复现度): a delta at or below the movement reported there "
                "is not a difference this table resolves"
            ]
            if control_repeat is not None
            else [
                "the control arm's reproducibility was NOT measured in this run, so this "
                "table makes no claim that its row repeats; --repeat-control measures it"
            ]
        )
        + (
            [
                "the index was not rebuilt in this run. It was reused under "
                f"`{INDEX_PREFIX}-tenant-{TENANT}-*` because re-ingesting 28,481 documents "
                "costs ~53 minutes and the corpus digest has not moved. What the report "
                "records is the active generation and its parent/child counts, read back "
                "from the cluster; that proves the generation is reachable and populated, "
                "not that it holds *this* corpus. The run's own check is the fidelity "
                "anchor -- if `c0_off` did not reproduce the published `hybrid_rerank` "
                "recall, none of the rows beside it may be read"
            ]
            if reuse_index
            else []
        ),
    }
    if drop:
        await index.drop_tenant_indices(TENANT)
    return payload


def _pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def _delta(value: float) -> str:
    return f"{value * 100:+.2f}pp"


def render_markdown(payload: dict[str, Any]) -> str:
    arms = payload["arms"]
    anchor = payload["fidelity_anchor"]
    baseline = arms["c0_off"]["recall_at_10"]
    rows = []
    for name, arm in arms.items():
        rows.append(
            "| `{name}` | {q} | {r} | {r5} | {r10} | {r20} | {mrr} | {packed} | {secs}s |".format(
                name=name,
                q="model" if arm["use_query_model"] else "deterministic",
                r="on" if arm["use_rewrites"] else "off",
                r5=_pct(arm["recall_at_5"]),
                r10=_pct(arm["recall_at_10"]),
                r20=_pct(arm["recall_at_20"]),
                mrr=f"{arm['mrr_at_10']:.4f}",
                packed=arm["packed_documents"]["max"],
                secs=int(arm["arm_seconds"]),
            )
        )
    moves = []
    for name, arm in arms.items():
        if name == "c0_off":
            continue
        moves.append(
            f"| `{name}` | {_pct(arm['recall_at_10'])} | "
            f"{_delta(arm['recall_at_10'] - baseline)} | "
            f"{_delta(arm['recall_at_10'] - arms['c0_mq']['recall_at_10'])} |"
        )
    collapsed = [
        f"- `{name}`：{'、'.join(arm['cutoffs_the_pack_cannot_tell_apart']) or '无'}"
        for name, arm in arms.items()
        if arm["cutoffs_the_pack_cannot_tell_apart"]
    ]
    # ``get``, not ``[]``: a report written before this block existed is still a
    # payload this renderer must be able to read, and a KeyError would make the
    # gate crash rather than report the mismatch it exists to report.
    repeat = payload.get("control_repeatability")
    if repeat is None:
        repeat_block = (
            "## 控制臂自身的可复现度\n\n"
            "本轮**未测量**（`--repeat-control` 未开启）。因此本表不声明控制臂可复现，"
            "也不声明任何小于一行之差是可分辨的。\n"
        )
    else:
        first, second = repeat["first"], repeat["second"]
        repeat_block = f"""## 控制臂自身的可复现度

同一条控制臂（`c0_off`）在同一索引、同一捕获、同一主体上跑第二遍；两列之间唯一的
差别就是「又跑了一遍」。这张表的每一行都是**相对它**读的，所以它能移动多少，就是
这张表能分辨的最小差。

| 项 | 第一遍 | 第二遍 |
|---|---|---|
| R@5 | {_pct(first["recall_at_5"])} | {_pct(second["recall_at_5"])} |
| R@10 | {_pct(first["recall_at_10"])} | {_pct(second["recall_at_10"])} |
| R@20 | {_pct(first["recall_at_20"])} | {_pct(second["recall_at_20"])} |
| MRR@10 | {first["mrr_at_10"]:.4f} | {second["mrr_at_10"]:.4f} |
| packed max | {first["packed_documents"]["max"]} | {second["packed_documents"]["max"]} |

| 逐 query 对比 | 条数 |
|---|---|
| 参与比较 | {repeat["queries_compared"]} |
| R@5 变化 | {repeat["recall_at_5_moved"]} |
| R@10 变化 | {repeat["recall_at_10_moved"]} |
| 排名（MRR@10）变化 | {repeat["reciprocal_rank_moved"]} |
| 只出现在其中一遍 | {len(repeat["queries_in_one_pass_only"])} |

{repeat["reading"]}
"""
    return f"""# 生产 query arm 与漏斗深度实测（TechQA release set）

**状态**：`{payload["status"]}`（不构成质量认证）
**生成时间**：{payload["generated_at"]}
**索引**：`{payload["corpus"]["index_prefix"]}`，{payload["corpus"]["documents_indexed"]} 篇文档，ingest {payload["corpus"]["ingest_seconds"]}s
**改写模型**：`{payload["models"]["rewrite_model"]}`
**改写缓存**：`{payload["inputs"]["rewrite_cache"]["path"]}` sha256 `{payload["inputs"]["rewrite_cache"]["sha256"][:16]}…`（{payload["inputs"]["rewrite_cache"]["queries_with_no_rewrite"]} 条未产出改写）

## 为什么需要这份报告

已提交的每一个 TechQA 数字都是在 `use_query_model=False` 下测出来的，且全部关闭
`use_rewrites`。生产不是这样跑的：`agents/knowledge.py` 用
`use_query_model=True`、`use_rewrites=SERVICEMIND_RAG_MULTI_QUERY`（该配置固定为
`True`，注释写明“改写阶段始终运行”）。测量口径与部署口径不一致，而此前没有任何
数字说明这个差异把检索推向哪一边。

## 忠实度锚点

| 项 | 值 |
|---|---|
| 已提交 `hybrid_rerank` R@10 | {anchor["published_recall_at_10"]} |
| 本次 `c0_off` 复现 R@10 | {anchor["reproduced_recall_at_10"]} |
| 差值 | {anchor["delta_recall_at_10"]} |
| 已提交 packed max | {anchor["published_packed_max"]} |
| 本次 `c0_off` packed max | {anchor["reproduced_packed_max"]} |

{anchor["reading"]}

{repeat_block}

## 各 arm 实测

| arm | query 处理 | 改写 fan-out | R@5 | R@10 | R@20 | MRR@10 | packed max | 耗时 |
|---|---|---|---|---|---|---|---|---|
{chr(10).join(rows)}

## 相对 `c0_off` 与相对生产 arm 的位移（R@10）

| arm | R@10 | 对 `c0_off` | 对 `c0_mq` |
|---|---|---|---|
{chr(10).join(moves)}

## 截止点坍缩

包内文档数不足以区分这些截止点，它们报告的同一个数字是包长而非检索能力：

{chr(10).join(collapsed) if collapsed else "（无）"}

## 局限

{chr(10).join(f"- {item}" for item in payload["limitations"])}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-only", action="store_true")
    parser.add_argument("--force-capture", action="store_true")
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--drop", action="store_true", help="drop the index when done")
    parser.add_argument(
        "--reuse-index",
        action="store_true",
        help="skip the ~53-minute ingest and run the arms against the index the previous "
        "run left under --index-prefix; the fidelity anchor validates the reuse",
    )
    parser.add_argument(
        "--repeat-control",
        action="store_true",
        help="measure the control arm a second time and report query by query what it "
        "reproduces; every other row is read against it",
    )
    parser.add_argument(
        "--arms",
        help="comma-separated subset of the arms to measure; must include c0_off. "
        "The payload records the selection, so a subset is never read as the full table",
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    selected = _select_arms(args.arms.split(",") if args.arms else None)

    if args.check:
        if not (OUT_JSON.exists() and OUT_MD.exists()):
            print("FAIL report missing")
            return 1
        payload = json.loads(OUT_JSON.read_text(encoding="utf-8"))
        stale = []
        for key, path in (
            ("producer", PRODUCER),
            ("release_loader", RELEASE_SCRIPT),
            ("selection", SELECTION),
            ("corpus", DATA / "corpus.zip"),
            ("production_report", PRODUCTION_REPORT),
        ):
            if payload["inputs"][key]["sha256"] != _digest(path):
                stale.append(key)
        if REWRITE_CACHE.exists() and payload["inputs"]["rewrite_cache"]["sha256"] != _digest(
            REWRITE_CACHE
        ):
            stale.append("rewrite_cache")
        markdown_matches = OUT_MD.read_text(encoding="utf-8") == render_markdown(payload)
        ok = not stale and markdown_matches
        print(
            "PASS" if ok else "FAIL",
            payload["status"],
            f"stale_inputs={stale}",
            f"markdown_matches={markdown_matches}",
        )
        return 0 if ok else 1

    if args.capture_only:
        release = _load_release_module()
        selection = json.loads(SELECTION.read_text(encoding="utf-8"))
        train = json.loads((DATA / "train.json").read_text(encoding="utf-8"))
        gold = release._gold_set(selection, train)
        capture = asyncio.run(
            capture_rewrites(gold, concurrency=args.concurrency, force=args.force_capture),
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
        distinct = statistics.fmean(
            len(value["rewritten_queries"]) for value in capture["queries"].values()
        )
        print(f"mean rewrites per query: {distinct:.2f}")
        return 0

    payload = asyncio.run(
        measure(
            force_capture=args.force_capture,
            concurrency=args.concurrency,
            drop=args.drop,
            reuse_index=args.reuse_index,
            repeat_control=args.repeat_control,
            arms=selected,
        ),
        loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
    )
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    OUT_MD.write_text(render_markdown(payload), encoding="utf-8")
    print("wrote", OUT_JSON.name, "and", OUT_MD.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
