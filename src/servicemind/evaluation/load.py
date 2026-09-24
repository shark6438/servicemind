"""The Phase 7.6.7 load plan: declared concurrency tiers, and what one passing run means.

Where the acceptance, security and quality batches ask what the platform *answers*, this one
asks what happens to those answers when several runs arrive at once. The failure it exists to
catch is not a wrong answer -- a wrong answer is the quality batch's to find -- but a right
answer that stops arriving: a run that never settles, a run whose citations come back empty,
a run that dies under contention in a way it does not die when it runs alone.

**What is asserted, and what is only reported.** Every declared run must happen, must rest,
must rest the way the same question rested at the single-run tier, and -- when it rests at
``succeeded`` -- must cite the document the quality case named. Those four clauses are the
whole of the gate; ``load_grader`` states each of them and why clause three is a comparison
rather than "everything must succeed". Latency is measured and reported -- median, p95,
maximum, and the p95 at a tier relative to the single-run tier -- and it is deliberately
*not* asserted against a ceiling. A ceiling would have to be a number this file invented, and
a number invented here is a number chosen to be passed or failed rather than a capacity the
deployment's owner has decided it needs. The report prints the measurements; whether they
are good enough is a decision for whoever owns the deployment, and it should be made with
the numbers in front of them rather than by a constant buried in a plan file.

**Why the single-run tier is part of the plan rather than an afterthought.** It carries two
jobs, and the plan validator refuses a plan that omits it for either. The relative numbers
are the interpretable ones -- an absolute p95 of 40 seconds says nothing on its own, whereas
40 seconds against a single-run p95 of 12 says the platform degrades threefold under ten-way
concurrency, which is a fact about the platform rather than about the host it ran on -- and
it is also the baseline the loaded runs are compared against, since "the outcome changed"
needs an outcome to have changed from. See ``LoadPlan._the_tiers_are_a_ladder``.

**The workload is not a second corpus.** It is a declared slice of the quality case list: the
same questions, the same declared citations, the same subject identities, because a load
batch run over its own private questions would measure a platform under a workload no other
evidence in this repository is about. The slice is pinned by digest. Edit one of the
questions it names and the recorded observations stop describing the current workload, which
is what makes the load gate exit 3 rather than re-report an old measurement against a new
question.

**What this does not measure.** Capacity in the sense an operator means it -- the run rate
the deployment sustains for an hour -- is not what three short tiers produce. Nor is the
latency here a cold-start latency: the workload repeats the same questions across a tier's
repeats, so a provider-side prompt cache that helps the third repeat more than the first is
inside these numbers. Both are reported rather than corrected for, and the report breaks the
latency down by repeat index so that a caching effect is visible as what it is instead of
being averaged into a single figure that reads as the platform getting faster.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from servicemind.evaluation.quality import load_quality_cases

LOAD_SCHEMA_VERSION = "phase7-load-v1"


class LoadTier(BaseModel):
    """One declared concurrency, and how many times the workload is run at it.

    ``repeats`` is not decoration. A single pass at a tier gives one sample per question and
    cannot distinguish "this platform is slow at ten concurrent runs" from "the tenth call
    happened to be slow"; repeats give each tier a distribution and make the per-repeat
    breakdown in the report possible.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=32)
    concurrency: int = Field(ge=1, le=64)
    repeats: int = Field(ge=1, le=20)

    def run_count(self, workload_size: int) -> int:
        """How many runs this tier is declared to produce, given the workload's size."""
        return workload_size * self.repeats


class WorkloadCase(BaseModel):
    """One question the load batch asks, with what its answer has to cite."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    subject: str
    ticket_ref: str
    question: str
    expected_citations: tuple[str, ...]


class Workload(BaseModel):
    """The frozen slice of the quality case list the load batch runs.

    ``source_digest`` is the quality case set's own digest at the moment this was resolved.
    It is what the observation files carry, so a reader can tell which version of the corpus
    the timing came from without re-deriving it from the case ids.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    kind: str
    source_digest: str
    cases: tuple[WorkloadCase, ...]


class LoadPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    description: str
    tenant_id: UUID
    #: Where the questions come from, relative to the repository root, and which class of
    #: case is the workload. A path-and-kind rather than a copy: a second corpus is a second
    #: thing to drift, and this one would drift silently, since nobody re-reads a load plan.
    workload_source: str = Field(min_length=1)
    workload_kind: str = Field(min_length=1)
    workload_limit: int = Field(ge=1, le=500)
    #: How long a single run is given to settle before it is recorded as never having
    #: settled. This is a deadline on the poll, not a latency assertion: the number is
    #: inherited from the acceptance driver's own per-case budget
    #: (``DEFAULT_CASE_BUDGET_SECONDS``), which was chosen there for a single run on an
    #: unloaded stack and is not re-chosen here to suit a loaded one. A run that cannot
    #: finish inside a single run's budget has already failed the criterion that matters --
    #: it did not answer -- and the report says so in those words rather than reporting a
    #: latency percentile as a pass.
    settle_budget_seconds: float = Field(gt=0)
    tiers: tuple[LoadTier, ...]

    @model_validator(mode="after")
    def _the_tiers_are_a_ladder(self) -> LoadPlan:
        if self.schema_version != LOAD_SCHEMA_VERSION:
            raise ValueError(
                f"schema_version must be {LOAD_SCHEMA_VERSION!r}, got {self.schema_version!r}"
            )
        names = [tier.name for tier in self.tiers]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate tier names: {duplicates}")
        concurrencies = [tier.concurrency for tier in self.tiers]
        if len(set(concurrencies)) != len(concurrencies):
            raise ValueError(f"two tiers share a concurrency: {concurrencies}")
        if sorted(concurrencies) != concurrencies:
            raise ValueError(
                f"the tiers are not in ascending concurrency ({concurrencies}); the report "
                "reads as a ladder and a ladder stated out of order invites a reader to "
                "compare the wrong pair"
            )
        if not self.tiers:
            raise ValueError("a plan with no tiers measures nothing")
        if 1 not in concurrencies:
            raise ValueError(
                "a tier at concurrency 1 is required, and it carries two jobs: it is the "
                "denominator for every relative latency in the report, and it is the baseline "
                "the loaded runs are compared against -- 'the outcome changed' needs an "
                "outcome to have changed from. Without it the batch measures the host as "
                "much as the platform"
            )
        baseline = next(tier for tier in self.tiers if tier.concurrency == 1)
        if baseline.repeats != 1:
            raise ValueError(
                f"the concurrency-1 tier is declared with repeats={baseline.repeats}; the "
                "baseline must be one outcome per question, because a tier that ran a "
                "question twice and got two different answers has no single outcome for the "
                "loaded runs to be compared against, and the grader would have to invent a "
                "tie-break rather than report the disagreement"
            )
        return self


def load_plan(path: Path) -> LoadPlan:
    return LoadPlan.model_validate(json.loads(path.read_text(encoding="utf-8")))


def resolve_workload(plan: LoadPlan, source_path: Path) -> Workload:
    """The plan's declared slice of the quality case list, in the order the file holds it.

    Order is the file's, not sorted: "the first 20 answerable cases" is a statement anybody
    can re-derive from the plan, whereas a sorted or sampled slice is a rule that has to be
    reimplemented to be checked. The kind filter comes first so that widening the limit
    extends the slice rather than changing which cases are in it.
    """
    case_set = load_quality_cases(source_path)
    matching = [case for case in case_set.cases if case.kind.value == plan.workload_kind]
    chosen = matching[: plan.workload_limit]
    if len(chosen) != plan.workload_limit:
        raise ValueError(
            f"the plan asks for {plan.workload_limit} {plan.workload_kind!r} cases and "
            f"{source_path.name} holds {len(matching)}; a workload short of its declared "
            "size would be graded against run counts it cannot produce"
        )
    return Workload(
        source=plan.workload_source,
        kind=plan.workload_kind,
        source_digest=_digest(case_set.model_dump(mode="python")),
        cases=tuple(
            WorkloadCase(
                id=case.id,
                subject=case.subject,
                ticket_ref=case.ticket_ref,
                question=case.question,
                expected_citations=tuple(case.expected_citations),
            )
            for case in chosen
        ),
    )


def _canonical(value: object) -> object:
    """Collapse unordered containers to a hash-order-free rendering; see ``acceptance``."""
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(
            (_canonical(item) for item in value),
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"), default=str),
        )
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def _digest(value: object) -> str:
    payload = json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def plan_digest(plan: LoadPlan) -> str:
    return _digest(plan.model_dump(mode="python"))


def workload_digest(workload: Workload) -> str:
    return _digest(workload.model_dump(mode="python"))


def observation_digest(payload: object) -> str:
    return _digest(payload)


def parse_citations(value: Any) -> tuple[str, ...]:
    """A recorded citation list, as a tuple, whatever a hand-edited replay holds.

    The observation files are JSON on disk and a person can edit one; a reader that trusted
    the shape would raise out of the middle of a grade with a message about strings. This
    keeps the failure inside the observation -- an unreadable citations field becomes an
    empty one, and an observation whose citations are empty fails the criterion rather than
    ending the run.
    """
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return ()
