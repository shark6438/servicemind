from __future__ import annotations

import statistics
from collections.abc import Mapping
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from servicemind.domain.knowledge import (
    KnowledgeRAGResult,
    RetrievalMode,
    RetrievalPrincipal,
)
from servicemind.evaluation.gold import GoldQuery, GoldSet
from servicemind.evaluation.leakage import (
    EvidenceCoordinates,
    Violation,
    visible_evidence_violations,
)
from servicemind.evaluation.metrics import (
    dedupe_rate,
    mrr_at_k,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)


class RetrievalProvider(Protocol):
    """Anything that runs one retrieval configuration end-to-end."""

    async def retrieve(
        self,
        *,
        principal: RetrievalPrincipal,
        query: str,
        mode: RetrievalMode,
        run_rerank: bool,
        final_k: int,
    ) -> KnowledgeRAGResult: ...


class Baseline(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    mode: RetrievalMode
    run_rerank: bool


#: The four isolation axes the acceptance report compares (Phase 4 baseline §4.3).
BASELINES = (
    Baseline(name="dense", mode=RetrievalMode.DENSE, run_rerank=False),
    Baseline(name="bm25", mode=RetrievalMode.BM25, run_rerank=False),
    Baseline(name="hybrid", mode=RetrievalMode.HYBRID, run_rerank=False),
    Baseline(name="hybrid_rerank", mode=RetrievalMode.HYBRID, run_rerank=True),
)


def result_keys(result: KnowledgeRAGResult) -> list[str]:
    """The ordered, deduped document keys a packed result stands for."""
    seen: set[str] = set()
    keys: list[str] = []
    for item in result.items:
        key = item.citation.source_record_id
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


class EvalQueryOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query_id: str
    query: str
    unanswerable: bool
    relevant: list[str] = Field(default_factory=list)
    ranked_keys: list[str] = Field(default_factory=list)
    candidate_count: int = 0
    dedupe_rate: float = 0.0
    latency_ms: float = 0.0
    #: Retrieved items the asker had no right to see. Empty for every run that is not
    #: broken -- the §4.1 threshold is a zero-count, so a non-empty list here is a
    #: release blocker rather than a metric that can be averaged or compared.
    violations: list[Violation] = Field(default_factory=list)


class BaselineMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    baseline: str
    outcomes: list[EvalQueryOutcome] = Field(default_factory=list)

    @property
    def answerable(self) -> list[EvalQueryOutcome]:
        return [outcome for outcome in self.outcomes if not outcome.unanswerable]

    @property
    def unanswerable(self) -> list[EvalQueryOutcome]:
        return [outcome for outcome in self.outcomes if outcome.unanswerable]

    def visible_evidence_violations(self) -> list[Violation]:
        """Every unauthorized item this baseline surfaced, over all queries.

        Reported per baseline rather than per report because the four baselines run the
        same principal through different filters; a violation appearing in one and not
        another is a fact about that filter, which is exactly what §4.1 wants to know
        and what a report-level total would hide.
        """
        return [item for outcome in self.outcomes for item in outcome.violations]

    def _mean(self, fn) -> float:
        values = [fn(outcome) for outcome in self.answerable]
        return statistics.fmean(values) if values else 0.0

    def recall_at(self, k: int) -> float:
        return self._mean(lambda o: recall_at_k(o.ranked_keys, set(o.relevant), k))

    def mrr_at(self, k: int) -> float:
        return self._mean(lambda o: mrr_at_k(o.ranked_keys, set(o.relevant), k))

    def ndcg_at(self, k: int) -> float:
        return self._mean(lambda o: ndcg_at_k(o.ranked_keys, set(o.relevant), k))

    def precision_at(self, k: int) -> float:
        return self._mean(lambda o: precision_at_k(o.ranked_keys, set(o.relevant), k))

    def mean_dedupe(self) -> float:
        values = [o.dedupe_rate for o in self.outcomes if o.ranked_keys]
        return statistics.fmean(values) if values else 0.0

    def answered_unanswerable(self) -> int:
        """Unanswerable gold queries that nonetheless got context fed upward."""
        return sum(1 for o in self.unanswerable if o.ranked_keys)

    def abstention_rate(self) -> float:
        total = len(self.unanswerable)
        return 1.0 - (self.answered_unanswerable() / total) if total else 1.0


class EvalReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gold_set: str
    top_ks: list[int] = Field(default_factory=lambda: [5, 10, 20])
    baselines: list[BaselineMetrics] = Field(default_factory=list)

    def metrics(self, name: str) -> BaselineMetrics:
        for metrics in self.baselines:
            if metrics.baseline == name:
                return metrics
        raise KeyError(name)


def principal_for(gold_query: GoldQuery, default: RetrievalPrincipal) -> RetrievalPrincipal:
    """The principal this query is asked as, falling back to the run's default.

    §3.2 asks for a "wrong tenant" stratum and §4.1 for a zero-count on wrong-ACL
    evidence, and both are statements about *who asked*: one query text is answerable for
    one caller and a disclosure for another. A single principal for the whole run cannot
    express that, so a query that names its asker overrides the default and one that does
    not inherits it.
    """
    if gold_query.asker_tenant_id is None and not gold_query.asker_group_ids:
        return default
    return default.model_copy(
        update={
            "tenant_id": gold_query.asker_tenant_id or default.tenant_id,
            "group_ids": gold_query.asker_group_ids,
            "entity_ids": gold_query.asker_entity_ids or default.entity_ids,
        }
    )


async def evaluate(
    provider: RetrievalProvider,
    gold: GoldSet,
    principal: RetrievalPrincipal,
    *,
    top_ks: tuple[int, ...] = (5, 10, 20),
    baselines: tuple[Baseline, ...] = BASELINES,
    coordinates: Mapping[str, EvidenceCoordinates] | None = None,
) -> EvalReport:
    """Run every gold query through each retrieval baseline.

    Top-k isolation is bounded by how many packed contexts the provider can return
    for one query; the provider is asked for ``max(top_ks) + 4`` so Recall@top_ks is
    measurable rather than truncated by the context packer.

    ``coordinates`` is the ACL ground truth for the corpus that was loaded. When it is
    supplied, every returned item is checked against the asker that asked for it and any
    unauthorized one is recorded on the outcome; when it is not, ``violations`` stays
    empty because nothing is known -- which is a different statement from "none were
    found", and the two must not render the same way. Callers that index a corpus should
    pass it; the §4.1 zero-count is not computable without it.
    """
    final_k = max(top_ks) + 4
    per_baseline: list[BaselineMetrics] = []
    for baseline in baselines:
        outcomes: list[EvalQueryOutcome] = []
        for gold_query in gold.queries:
            asker = principal_for(gold_query, principal)
            result = await provider.retrieve(
                principal=asker,
                query=gold_query.query,
                mode=baseline.mode,
                run_rerank=baseline.run_rerank,
                final_k=final_k,
            )
            keys = result_keys(result)
            violations: list[Violation] = []
            if coordinates is not None:
                violations = visible_evidence_violations(
                    query_id=gold_query.id,
                    retrieved=keys,
                    coordinates=coordinates,
                    asker_tenant_id=asker.tenant_id,
                    asker_group_ids=asker.group_ids,
                    query_time=asker.query_time,
                )
            outcomes.append(
                EvalQueryOutcome(
                    query_id=gold_query.id,
                    query=gold_query.query,
                    unanswerable=gold_query.unanswerable,
                    relevant=list(gold_query.relevant),
                    ranked_keys=keys,
                    candidate_count=result.candidate_count,
                    dedupe_rate=dedupe_rate(
                        [item.citation.source_record_id for item in result.items]
                    ),
                    latency_ms=result.latency_ms,
                    violations=violations,
                )
            )
        per_baseline.append(BaselineMetrics(baseline=baseline.name, outcomes=outcomes))
    return EvalReport(
        gold_set=gold.name,
        top_ks=list(top_ks),
        baselines=per_baseline,
    )


def to_report_tables(report: EvalReport) -> dict[str, list[list[object]]]:
    """Flatten per-baseline metrics into a small table for markdown/JSON reports."""
    headers: list[object] = [
        "baseline",
        "recall@5",
        "recall@10",
        "recall@20",
        "mrr@10",
        "ndcg@10",
        "precision@5",
        "answered_unanswerable",
        "abstention_rate",
        "mean_latency_ms",
    ]
    rows: list[list[object]] = [headers]
    for metrics in report.baselines:
        latencies = [o.latency_ms for o in metrics.outcomes]
        rows.append(
            [
                metrics.baseline,
                round(metrics.recall_at(5), 4),
                round(metrics.recall_at(10), 4),
                round(metrics.recall_at(20), 4),
                round(metrics.mrr_at(10), 4),
                round(metrics.ndcg_at(10), 4),
                round(metrics.precision_at(5), 4),
                metrics.answered_unanswerable(),
                round(metrics.abstention_rate(), 4),
                round(statistics.fmean(latencies), 1) if latencies else 0.0,
            ]
        )
    return {"table": rows}


def markdown_report(report: EvalReport) -> str:
    lines = [
        f"# Phase 4 retrieval evaluation — {report.gold_set}",
        "",
        f"Top-k cutoffs: {', '.join(str(k) for k in report.top_ks)}.",
        "",
        "| baseline | Recall@5 | Recall@10 | Recall@20 | MRR@10 | NDCG@10 | "
        "Precision@5 | answered-unanswerable | abstention | latency(ms) |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for metrics in report.baselines:
        latencies = [o.latency_ms for o in metrics.outcomes]
        lines.append(
            f"| {metrics.baseline} | {metrics.recall_at(5):.3f} | "
            f"{metrics.recall_at(10):.3f} | {metrics.recall_at(20):.3f} | "
            f"{metrics.mrr_at(10):.3f} | {metrics.ndcg_at(10):.3f} | "
            f"{metrics.precision_at(5):.3f} | {metrics.answered_unanswerable()} | "
            f"{metrics.abstention_rate():.3f} | "
            f"{(statistics.fmean(latencies) if latencies else 0):.0f} |"
        )
    return "\n".join(lines) + "\n"


def gold_query_lookup(gold: GoldSet) -> dict[str, GoldQuery]:
    return {query.id: query for query in gold.queries}
