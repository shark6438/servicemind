"""Judge the recorded load observations: did concurrency change any answer?

A pure function over the observation files, like the acceptance, security and quality graders
and for the same reason: the judge can be re-run over the same observations without a stack,
so re-running the platform until it agrees with the report is not available as a way to make
an inconvenient measurement go away.

**The criterion, stated exactly.** For every run the plan declares:

1. It must be observed. A declared run with no observation behind it is a tier that was not
   measured, and a tier that was not measured is BLOCKED -- not passed, and not failed.
2. It must rest. A run with no status at all has wedged, and a run that wedges under load is
   the defect this batch exists to find. "Rest" is the acceptance driver's ``SETTLED`` set --
   including the ``waiting_*`` states, because a run parked for a human has stopped moving and
   is an outcome to be compared against, not a hang.
3. It must rest the way the same question rested at the single-run tier. Concretely: if the
   question succeeded when it was the only run on the machine, it must succeed now; if it did
   not (the platform handed it to a human, say), it must land in the same place now. Any
   `succeeded` is accepted in place of a non-succeeding baseline, because a question that
   starts succeeding under load is not a load defect.
4. A run that rested at `succeeded` must cite the document its quality case declared.

**Why clause 3 is phrased as a comparison rather than "everything must succeed".** The first
version of this rule *was* "everything must succeed", and the P7.6.6 batch falsified it
before this batch ever ran: roughly one answerable question in ten rests at `waiting_review`
on an idle machine, because the supervisor decided a human should look. That is the platform
working, not failing, and a load batch that called it a load defect would report a hundred
false failures and hide the real ones among them. Comparing against the unloaded run asks the
question this batch is actually about -- *did the machine being busy change the outcome* --
and it is answerable precisely because the plan is required to contain a single-run tier.

**Why latency is reported rather than judged.** Every absolute latency ceiling is either a
number the deployment's owner has decided it needs, or a number invented in this file. The
first belongs in a plan written by that owner; the second would be chosen by whoever saw the
measurement, which is the shape of a threshold picked to be passed. So the report prints
median, p95, maximum and the ratio against the single-run tier, and says plainly that it is
not asserting anything about them. The measurements are still load-bearing: a tier that
degrades tenfold shows up in the relative column, and a run slow enough to be a defect does
not get to hide in it -- it never settles, which clause 2 fails.

**Why the workload digest travels with every observation.** The runs were made against
specific questions. Change one of those questions and these timings describe a workload that
no longer exists -- the platform may be faster or slower on the new text, and nothing in the
record says which. The digest makes that a drift the gate can see rather than a fact a reader
has to remember.
"""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from servicemind.evaluation.load import (
    LoadPlan,
    LoadTier,
    Workload,
    observation_digest,
    parse_citations,
    plan_digest,
    workload_digest,
)

#: The statuses a run can *rest* at. ``None`` is not one of them: a run still in flight when
#: it was observed was not observed, and folding "never settled" into "settled badly" is how a
#: wedged platform reads as a merely unsuccessful one.
#:
#: This is the acceptance driver's ``SETTLED`` set, and it has to be: that is the set the live
#: poll stops on, so it is the set of outcomes an observation can carry. The quality grader's
#: narrower ``TERMINAL_STATUSES`` was the obvious thing to copy and it was wrong here -- this
#: file had it, and the first contract run failed on it. A run parked in ``waiting_review``
#: *has* rested; it is the platform saying a human should look, and the P7.6.6 batch produced
#: those on an idle machine. A load batch that called them "not rested" would report a wedged
#: platform on a healthy one, and would do it in the very clause whose whole job is to
#: separate the platform's decisions from the machine being busy.
RESTING_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "waiting_approval", "waiting_review"}
)

#: The status a run has to reach for clause 4 (the citation clause) to apply to it.
SUCCEEDED = "succeeded"

#: How many missing or failing rows a report names before it stops. The count is always
#: complete; only the listing is bounded, so that a tier that lost half its runs produces a
#: report somebody can read rather than sixty lines of the same sentence.
MAX_LISTED_ROWS = 20


class Verdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


class RunObservation(BaseModel):
    """One recorded run: which tier, which question, how long, and what came back.

    ``latency_seconds`` defaults to -1.0, not 0.0, and the distinction is the same one
    ``quality.ReviewSignals`` draws: a run whose timing was never recorded is not a run that
    took no time. Zero would enter the percentile arithmetic as the fastest possible run and
    drag every reported latency down, which is exactly backwards -- the missing ones are
    usually the ones that went wrong.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tier: str
    concurrency: int = Field(ge=1)
    repeat_index: int = Field(ge=0)
    case_id: str
    subject: str = ""
    run_id: str | None = None
    terminal_status: str | None = None
    reviewer_decision: str | None = None
    citations: tuple[str, ...] = ()
    expected_citations: tuple[str, ...] = ()
    unreadable_citations: int = 0
    latency_seconds: float = -1.0
    errors: tuple[str, ...] = ()
    observed_at: datetime | None = None
    deployed_revision: str | None = None
    plan_digest: str
    workload_digest: str

    @field_validator("citations", "expected_citations", "errors", mode="before")
    @classmethod
    def _tolerate_a_hand_edited_replay(cls, value: object) -> object:
        """Coerce a recorded list, whatever a person left in the file. See ``parse_citations``."""
        return parse_citations(value)

    def slot(self) -> str:
        """The run's identity within the batch: which question, on which pass."""
        return f"{self.case_id}#{self.repeat_index}"


class TierTiming(BaseModel):
    """When a tier started and stopped, so the report can state an achieved run rate.

    The rate is ``declared runs / wall seconds``. It is the one capacity-shaped number the
    batch produces honestly, because it is arithmetic over two recorded instants rather than
    a summary of per-run latencies -- summing those would report a throughput the host never
    had, since runs overlap.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    started_at: datetime
    finished_at: datetime

    def wall_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()


class BatchHeader(BaseModel):
    """``_batch.json``: what the recorded runs were taken under, as distinct from what they saw."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: str = "live"
    base_url: str = ""
    tenant_id: str = ""
    recorded_at: datetime | None = None
    deployed_revision: str | None = None
    plan_digest: str = ""
    workload_digest: str = ""
    tiers: tuple[TierTiming, ...] = ()

    def timing(self, name: str) -> TierTiming | None:
        for tier in self.tiers:
            if tier.name == name:
                return tier
        return None


class LatencyReading(BaseModel):
    """The distribution of one tier's per-run latencies, over the runs that recorded one."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    count: int
    median_seconds: float
    p95_seconds: float
    max_seconds: float


class RepeatReading(BaseModel):
    """One pass of the workload at one tier. The per-repeat view is where caching shows."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repeat_index: int
    runs: int
    p95_seconds: float


class TierReading(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    concurrency: int
    repeats: int
    declared_runs: int
    observed_runs: int
    passed: int
    failed: int
    #: Declared runs with no observation behind them, as ``case#repeat``. Non-empty blocks the
    #: tier rather than failing it: nothing was learned about those runs.
    missing: tuple[str, ...] = ()
    latency: LatencyReading | None = None
    by_repeat: tuple[RepeatReading, ...] = ()
    wall_seconds: float | None = None
    runs_per_minute: float | None = None


class LoadOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    generated_at: datetime
    verdict: Verdict
    plan_digest: str
    workload_digest: str
    workload_source: str
    workload_size: int
    settle_budget_seconds: float
    baseline_tier: str
    deployed_revisions: tuple[str, ...] = ()
    observed_from: datetime | None = None
    observed_to: datetime | None = None
    tiers: tuple[TierReading, ...]
    findings: tuple[str, ...]
    blockers: tuple[str, ...]

    def tier(self, name: str) -> TierReading:
        for reading in self.tiers:
            if reading.name == name:
                return reading
        raise KeyError(name)

    def degradation(self, name: str) -> float | None:
        """p95 at this tier over p95 at the single-run tier, or None when either is missing.

        The denominator is the tier with concurrency 1 -- found by its concurrency rather
        than by its name, since the name is the plan's to choose. A plan is required to have
        one, so a None here means the single-run tier was not observed, not that the plan
        omitted it.
        """
        single = next((item for item in self.tiers if item.concurrency == 1), None)
        reading = self.tier(name)
        if single is None or single.latency is None or reading.latency is None:
            return None
        if single.latency.p95_seconds <= 0:
            return None
        return reading.latency.p95_seconds / single.latency.p95_seconds


def percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile. No interpolation, so every number quoted is a real run.

    Interpolating between the two runs either side of the 95th percentile produces a duration
    no run ever took, and it does it precisely in the tail a reader is looking at. With
    twenty runs to a tier the difference is small; the principle is not, and nearest-rank is
    the definition a reader can check against the raw list without a formula.
    """
    if not values:
        raise ValueError("a percentile needs at least one observation")
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def _latency(observations: Sequence[RunObservation]) -> LatencyReading | None:
    recorded = [item.latency_seconds for item in observations if item.latency_seconds >= 0]
    if not recorded:
        return None
    return LatencyReading(
        count=len(recorded),
        median_seconds=round(statistics.median(recorded), 3),
        p95_seconds=round(percentile(recorded, 0.95), 3),
        max_seconds=round(max(recorded), 3),
    )


def _judge_run(
    tier: LoadTier, observation: RunObservation, baseline_status: str | None
) -> tuple[str, ...]:
    """What was wrong with this run, if anything. Empty means it passed.

    ``baseline_status`` is what the same question did at the single-run tier, or None when
    that tier did not record it -- in which case clause 3 cannot be applied and the run is
    judged on what can be judged: it must still rest, and if it succeeded it must still cite.
    """
    findings: list[str] = []
    label = f"{tier.name} {observation.slot()}"
    if observation.errors:
        findings.append(f"{label}: the recorder recorded {len(observation.errors)} error(s)")
    if observation.terminal_status is None:
        findings.append(
            f"{label}: the run never settled; {observation.latency_seconds:.1f}s after it was "
            "submitted it still had no terminal status, so it did not answer"
        )
        return tuple(findings)
    if observation.terminal_status not in RESTING_STATUSES:
        findings.append(
            f"{label}: the run rested at a status this batch does not know how to read "
            f"(status={observation.terminal_status!r})"
        )
        return tuple(findings)

    if (
        baseline_status is not None
        and observation.terminal_status != baseline_status
        and observation.terminal_status != SUCCEEDED
    ):
        findings.append(
            f"{label}: the same question rested at {baseline_status!r} on an idle machine and "
            f"at {observation.terminal_status!r} under {tier.concurrency}-way concurrency, so "
            "the load changed the outcome"
        )

    if observation.unreadable_citations:
        findings.append(
            f"{label}: {observation.unreadable_citations} evidence row(s) carried a citation "
            "that would not parse, so what this run cited is not fully known"
        )
    expected = set(observation.expected_citations)
    if observation.terminal_status == SUCCEEDED and expected:
        if not expected.issubset(set(observation.citations)):
            missing = sorted(expected - set(observation.citations))
            findings.append(
                f"{label}: the run succeeded without citing {missing}; it cited "
                f"{sorted(set(observation.citations))}"
            )
    return tuple(findings)


def _baseline_statuses(
    recorded: Sequence[RunObservation],
    baseline: LoadTier,
) -> dict[str, str]:
    """What each question did at the single-run tier.

    A question whose unloaded run did not rest is left *out* of the map rather than being
    entered with a placeholder. It is not a baseline, so clause 3 cannot be applied to it --
    and ``_judge_run`` reads a missing baseline as exactly that, judging the run on what can
    still be judged. The run is reported all the same: the baseline tier's own findings are
    routed to blockers by ``grade``, so a plan whose unloaded runs did not behave produces
    BLOCKED with the wedged runs named, rather than a FAIL that puts a load-shaped cause on
    an observation the batch cannot attribute to load.
    """
    by_case: dict[str, str] = {}
    for observation in recorded:
        if observation.tier != baseline.name:
            continue
        status = observation.terminal_status
        if status is None or status not in RESTING_STATUSES:
            continue
        by_case[observation.case_id] = status
    return by_case


def grade(
    plan: LoadPlan,
    workload: Workload,
    observations: Iterable[RunObservation],
    *,
    generated_at: datetime,
    batch: BatchHeader | None = None,
) -> LoadOutcome:
    recorded = list(observations)
    declared_names = {tier.name for tier in plan.tiers}
    findings: list[str] = []
    blockers: list[str] = []

    # An observation for a tier the plan does not declare is not a harmless extra. Either the
    # observations were recorded under a different plan -- which the digest check catches
    # first and with a better message -- or a row has been manufactured or left behind, and a
    # batch that quietly ignores rows it did not expect is a batch whose coverage claim is
    # about the rows it happened to look at.
    for observation in recorded:
        if observation.tier not in declared_names:
            findings.append(
                f"{observation.tier} {observation.slot()}: an observation for a tier this "
                "plan does not declare"
            )

    baseline_tier = next(tier for tier in plan.tiers if tier.concurrency == 1)
    baseline = _baseline_statuses(recorded, baseline_tier)

    tiers: list[TierReading] = []
    for tier in plan.tiers:
        rows = [item for item in recorded if item.tier == tier.name]
        observed_slots = {item.slot() for item in rows}
        declared_slots = [
            f"{case.id}#{repeat}" for repeat in range(tier.repeats) for case in workload.cases
        ]
        missing = sorted(slot for slot in declared_slots if slot not in observed_slots)
        if missing:
            blockers.append(
                f"{tier.name}: {len(missing)} of {len(declared_slots)} declared run(s) were "
                f"not observed, so this tier was not measured (first: {missing[0]})"
            )

        # Judged once. Asking ``_judge_run`` twice -- once to count the passes and once to
        # write the findings -- would let the two answers drift apart the first time anybody
        # added a branch to it, and the report would then disagree with its own table.
        judged = [
            (item, _judge_run(tier, item, baseline.get(item.case_id)))
            for item in sorted(rows, key=lambda row: row.slot())
        ]
        # A run that went wrong at the single-run tier is not evidence about load, so it is
        # not a finding: it is the batch telling you its own reference point is broken, and
        # the verdict for that is BLOCKED. Routing on the tier rather than on the message
        # keeps the two apart without every clause having to know which tier it is judging.
        is_baseline = tier.concurrency == 1
        baseline_rows: list[str] = []
        for _, row_findings in judged:
            if is_baseline:
                baseline_rows.extend(f"{baseline_tier.name}: {row}" for row in row_findings)
            else:
                findings.extend(row_findings)
        if baseline_rows:
            blockers.extend(baseline_rows)
            blockers.append(
                f"{baseline_tier.name}: the unloaded tier is not a usable baseline "
                f"({len(baseline_rows)} run(s) did not behave), so nothing observed under load "
                "can be attributed to load"
            )
        passed = sum(
            1
            for item, row_findings in judged
            if not row_findings and item.terminal_status == SUCCEEDED
        )

        by_repeat: list[RepeatReading] = []
        for repeat in range(tier.repeats):
            pass_rows = [item for item in rows if item.repeat_index == repeat]
            reading = _latency(pass_rows)
            by_repeat.append(
                RepeatReading(
                    repeat_index=repeat,
                    runs=len(pass_rows),
                    p95_seconds=reading.p95_seconds if reading else -1.0,
                )
            )

        timing = batch.timing(tier.name) if batch else None
        wall = timing.wall_seconds() if timing else None
        declared_runs = tier.run_count(len(workload.cases))
        tiers.append(
            TierReading(
                name=tier.name,
                concurrency=tier.concurrency,
                repeats=tier.repeats,
                declared_runs=declared_runs,
                observed_runs=len(rows),
                passed=passed,
                failed=len(rows) - passed,
                # The blocker above counts them all; only this listing is bounded.
                missing=tuple(missing[:MAX_LISTED_ROWS]),
                latency=_latency(rows),
                by_repeat=tuple(by_repeat),
                # The rate divides the runs that were declared, not the ones that were
                # observed: dividing by what happened to arrive would report a higher rate
                # for a tier that lost runs.
                wall_seconds=round(wall, 3) if wall else None,
                runs_per_minute=round(declared_runs / (wall / 60.0), 3) if wall else None,
            )
        )

    revisions = sorted(
        {item.deployed_revision for item in recorded if item.deployed_revision is not None}
    )
    if len(revisions) > 1:
        findings.append(
            "these observations were recorded against more than one revision "
            f"({', '.join(revisions)}); no single build is described by this batch"
        )
    observed = [item.observed_at for item in recorded if item.observed_at is not None]

    if findings:
        overall = Verdict.FAIL
    elif blockers:
        overall = Verdict.BLOCKED
    else:
        overall = Verdict.PASS

    return LoadOutcome(
        schema_version=plan.schema_version,
        generated_at=generated_at,
        verdict=overall,
        plan_digest=plan_digest(plan),
        workload_digest=workload_digest(workload),
        workload_source=workload.source,
        workload_size=len(workload.cases),
        settle_budget_seconds=plan.settle_budget_seconds,
        baseline_tier=baseline_tier.name,
        deployed_revisions=tuple(revisions),
        observed_from=min(observed) if observed else None,
        observed_to=max(observed) if observed else None,
        tiers=tuple(tiers),
        findings=tuple(findings),
        blockers=tuple(blockers),
    )


def dump_outcome(outcome: LoadOutcome) -> str:
    return json.dumps(outcome.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)


def outcome_summary(outcome: LoadOutcome) -> dict[str, Any]:
    return {
        "verdict": outcome.verdict.value,
        "workload_source": outcome.workload_source,
        "workload_size": outcome.workload_size,
        "settle_budget_seconds": outcome.settle_budget_seconds,
        "baseline_tier": outcome.baseline_tier,
        "deployed_revisions": list(outcome.deployed_revisions),
        "observed_from": outcome.observed_from.isoformat() if outcome.observed_from else None,
        "observed_to": outcome.observed_to.isoformat() if outcome.observed_to else None,
        "tiers": [
            {
                "name": tier.name,
                "concurrency": tier.concurrency,
                "repeats": tier.repeats,
                "declared_runs": tier.declared_runs,
                "observed_runs": tier.observed_runs,
                "passed": tier.passed,
                "failed": tier.failed,
                "missing": len(tier.missing),
                "latency": tier.latency.model_dump(mode="json") if tier.latency else None,
                "wall_seconds": tier.wall_seconds,
                "runs_per_minute": tier.runs_per_minute,
                # None when the single-run tier was not observed, which is a different
                # statement from 1.0 and must not be rendered as one.
                "p95_over_baseline_tier": outcome.degradation(tier.name),
            }
            for tier in outcome.tiers
        ],
        "findings": len(outcome.findings),
        "blockers": list(outcome.blockers),
        "plan_digest": outcome.plan_digest,
        "workload_digest": outcome.workload_digest,
    }


def _table(rows: Sequence[Sequence[str]], header: Sequence[str]) -> list[str]:
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---" for _ in header) + "|",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def _seconds(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}"


def render_report(outcome: LoadOutcome) -> str:
    """The human-readable report. Machine-generated, like the other three batches'.

    Written from the outcome rather than from the run, so the two can never disagree: the
    numbers here are the numbers that were graded, not a second reading of the same files.
    """
    lines = [
        "# P7.6.7 负载批 —— 并发档位观测报告",
        "",
        "本报告由 `scripts/gate_phase7_load.py` 从已落盘的观测文件机器生成，不手写。",
        "",
        "## 结论",
        "",
        f"- 判定：**{outcome.verdict.value}**",
        f"- 工作负载：{outcome.workload_source}（前 {outcome.workload_size} 条，按文件顺序）",
        f"- 基线档位（并发 1）：`{outcome.baseline_tier}`",
        f"- 被测版本：{'、'.join(outcome.deployed_revisions) or '**未记录**'}",
        f"- 观测时间窗："
        f"{outcome.observed_from.isoformat() if outcome.observed_from else '**未记录**'}"
        f" ~ {outcome.observed_to.isoformat() if outcome.observed_to else '**未记录**'}",
        f"- 生成时间：{outcome.generated_at.isoformat()}（gate 运行时刻，非观测时刻）",
        f"- 单次运行结算预算：{outcome.settle_budget_seconds:.0f} 秒",
        f"- 未通过运行：{len(outcome.findings)} 条；观测不足：{len(outcome.blockers)} 条",
        f"- plan_digest：`{outcome.plan_digest}`",
        f"- workload_digest：`{outcome.workload_digest}`",
        "",
        "## 判定准则（四条，逐条可查）",
        "",
        "对计划声明的每一次运行：",
        "",
        "1. **必须被观测到。** 声明了却没有观测的运行 = 该档位没有被测量，记 **BLOCKED**——既不是通过，也不是失败。",
        "2. **必须停下来。** 没有终态的运行就是卡死，而卡死正是本批次存在的理由。",
        "3. **必须和在单并发档位下的结果一致。** 具体说：某问题在「机器上只有它一个运行」时 `succeeded`，在并发下也必须 `succeeded`；",
        "   若它单独跑时没有 `succeeded`（例如平台把它交给了人工），并发下也必须落在同一个地方。任何 `succeeded` 都接受，",
        "   因为「负载下反而成功了」不是负载缺陷。",
        "4. **停在 `succeeded` 的运行必须引用其质量案例预先声明的那篇文档。**",
        "",
        "### 为什么第 3 条是比较，而不是「全部必须成功」",
        "",
        "这条规则最初写的就是「全部必须成功」，而 **P7.6.6 批次在本批次开跑之前就把它证伪了**：",
        "约十分之一的可答问题在空载机器上停在 `waiting_review`——那是主管决定该由人看一眼，是平台在工作，不是失效。",
        "若按「全部必须成功」判定，本批次会报出大量假失败，并把真正的失败混在里面一起淹没。",
        "与空载运行比较，问的才是本批次真正要问的问题——**机器忙起来之后，结果变了没有**——",
        "而这个问题之所以可判定，正是因为计划必须包含一个单并发档位。",
        "",
        "### 时延只报告，不断言",
        "",
        "任何绝对的时延上限，要么是部署方已经决定的容量需求，要么是某个文件里凭空发明的数字；",
        "后者会被看过结果的人挑选，那正是「为了让门槛被通过而设的门槛」。因此下表给出中位数、p95、最大值与本档位相对单并发档位的倍数，",
        "并明确声明：**本批次不对这些数字作任何断言**。它们仍然承载信息——退化十倍的档位会在相对列里现形；",
        "而慢到成为缺陷的运行不藏在时延里：它根本停不下来，踩的是第 2 条。",
        "",
        "## 档位表",
        "",
    ]
    lines.extend(
        _table(
            [
                [
                    tier.name,
                    str(tier.concurrency),
                    str(tier.repeats),
                    str(tier.declared_runs),
                    str(tier.observed_runs),
                    str(tier.passed),
                    str(tier.failed),
                    str(len(tier.missing)),
                    _seconds(tier.latency.median_seconds if tier.latency else None),
                    _seconds(tier.latency.p95_seconds if tier.latency else None),
                    _seconds(tier.latency.max_seconds if tier.latency else None),
                    _seconds(tier.wall_seconds),
                    f"{tier.runs_per_minute:.1f}" if tier.runs_per_minute is not None else "—",
                    (
                        f"{outcome.degradation(tier.name):.2f}×"
                        if outcome.degradation(tier.name) is not None
                        else "—"
                    ),
                ]
                for tier in outcome.tiers
            ],
            (
                "档位",
                "并发",
                "重复",
                "声明运行数",
                "实际观测",
                "通过",
                "未通过",
                "未观测",
                "p50(秒)",
                "p95(秒)",
                "max(秒)",
                "墙钟(秒)",
                "运行/分钟",
                "p95/基线",
            ),
        )
    )
    lines.extend(
        [
            "",
            "「运行/分钟」按**声明的运行数**除以档位墙钟计算，不按实际到达数——按到达数除会让一个丢了运行的档位报出更高的速率。",
            "「p95/基线」是以并发 1 档位 p95 为分母的倍数；该列是相对量，不受本次所跑主机的影响。",
            "",
            "### 逐重复 p95（供读者自行判断是否存在缓存效应）",
            "",
        ]
    )
    lines.extend(
        _table(
            [
                [
                    tier.name,
                    str(reading.repeat_index),
                    str(reading.runs),
                    _seconds(reading.p95_seconds if reading.p95_seconds >= 0 else None),
                ]
                for tier in outcome.tiers
                for reading in tier.by_repeat
            ],
            ("档位", "第几次", "运行数", "p95(秒)"),
        )
    )
    lines.extend(
        [
            "",
            "工作负载在一档内会重复同一批问题，因此服务商侧的提示缓存若对后几遍帮助更大，就会落在这些数字里。",
            "本批次**不作修正**，把它逐遍列出，让读者看见它是什么，而不是被平均成一个读起来像「平台变快了」的单一数字。",
            "",
        ]
    )

    if outcome.findings:
        lines.extend(
            [
                "## 未通过的运行",
                "",
                f"共 {len(outcome.findings)} 条，列出前 {MAX_LISTED_ROWS} 条：",
                "",
            ]
        )
        lines.extend(f"- {finding}" for finding in outcome.findings[:MAX_LISTED_ROWS])
        lines.append("")

    if outcome.blockers:
        lines.extend(["## 观测不足", ""])
        lines.extend(f"- {blocker}" for blocker in outcome.blockers[:MAX_LISTED_ROWS])
        lines.append("")

    lines.extend(
        [
            "## 本批次不证明的内容",
            "",
            "- **不判定答案质量。** 本批次只用质量案例当工作负载，问的是「并发有没有把结果改掉」；答案本身对不对仍以",
            "  `phase7_quality_latest` 为准（P7.6.6）。一个所有档位答案都错的平台可以通过本批次，这条限制是结构性的，不是遗漏。",
            "- **不证明容量。** 三个短档位不产生「部署能持续支撑多少运行/小时」这个意义上的容量；报告里的速率是本次观测时长内的算术，",
            "  声明为测试档位，不是已认证容量。",
            "- **不是冷启动时延。** 同一档位内重复同一批问题，服务商侧提示缓存的影响已被包含在内（见上表逐重复 p95），本批次不作修正。",
            "- **只覆盖只读运行。** 工作负载全部是 `request_write=false` 的运行；写入路径（审批、`execute_node`、GLPI 回写）在并发下的表现不在本批次内。",
            "- **单主机结论。** 以上数字来自本部署所运行的那台主机与其上的 Docker/OpenSearch/Postgres/Neo4j，换一个部署不继承。",
            "- **依赖夹具存活。** 工作负载引用的文档与工单必须仍在索引与 GLPI 中；`workload_digest` 只保证问题文本未变，不保证语料未变。",
            "",
            "## 复现",
            "",
            "```bash",
            "uv run python scripts/verify_phase7_load_live.py --tenant-id <tenant>",
            "uv run python scripts/gate_phase7_load.py --replay-only --check",
            "```",
            "",
            "本批次**只**由观测文件判定，且不会在线运行平台；退出码与其余三个 gate 相同：",
            "0 PASS、1 FAIL、2 观测不足、3 输入之间已漂移。",
            "",
        ]
    )
    return "\n".join(lines)


def unplanned_tiers(plan: LoadPlan, observations: Iterable[RunObservation]) -> Mapping[str, int]:
    """Observation counts for tier names the plan does not declare. Used by the gate's drift note."""
    counts: dict[str, int] = {}
    declared = {tier.name for tier in plan.tiers}
    for observation in observations:
        if observation.tier not in declared:
            counts[observation.tier] = counts.get(observation.tier, 0) + 1
    return counts


def observations_digest(observations: Iterable[RunObservation]) -> str:
    """Order-independent by construction: sorted by tier, then slot.

    The runner finishes runs in whatever order the concurrency produced; the gate reads the
    files back in filename order. Digesting either order would make the digest a statement
    about the caller -- the mistake the security batch's ``observation_digest`` was fixed for.
    Sorting first means the digest is about the batch.
    """
    return observation_digest(
        [
            observation.model_dump(mode="python")
            for observation in sorted(observations, key=lambda item: (item.tier, item.slot()))
        ]
    )
