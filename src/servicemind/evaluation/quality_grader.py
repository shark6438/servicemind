"""The pure judge over recorded quality-case observations. No I/O, no stack, no clock.

Same split as the other two graders: the runner records what each run did, this module
decides what that means, and a recorded observation plus the case list is a complete
description of the outcome.

Three verdicts, and the split between the classes is the point:

``PASS``     the case came out the way it was pre-registered to.
``FAIL``     it did not. For the three hard classes this is a correctness defect -- a
             fabricated answer, a retired procedure cited, or a restricted document
             reached -- and there is no rate at which those become acceptable.
``BLOCKED``  the run did not produce something to judge: no terminal status, no reviewer
             decision, driver errors. Nothing was learned about the case, which is never
             folded into PASS.

**The four classes are not four columns of one rate.** Three of them are absolute: forty
questions the corpus cannot answer must not be answered, twenty questions must not cite the
superseded revision, twenty must not cite a document the asker cannot reach. The gate fails
on a single violation of any of them. Only the answerable class is a rate, because only
there is a population and a threshold. Reporting all four as one "quality score" would let
a fabrication be averaged against ninety-nine successes.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from servicemind.evaluation.quality import (
    REQUIRED_COMPOSITION,
    CaseKind,
    QualityCase,
    QualityCaseSet,
    case_set_digest,
    observation_digest,
)


class Verdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


#: The terminal statuses a run can rest at. Anything else -- and None -- means the run was
#: still in flight when it was observed, and a case observed mid-flight was not observed.
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})

#: The reviewer's decision that constitutes asserting an answer, and the signal the two
#: classes that *require* an answer are graded on.
#:
#: It is about the claims the analysis made, not about the question. ``passed`` means the
#: claims are supported by the evidence they cite -- so a run that answers an uncovered
#: question with "none of the cited documents states a procedure for this" is also
#: ``passed``. ``insufficient-evidence`` was written to fail on it and that was wrong; see
#: ``quality.ReviewSignals`` for the reading it uses instead.
ASSERTING_DECISION = "passed"


class CaseObservation(BaseModel):
    """What one quality case's run did, as the runner saw it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    observed_at: datetime
    deployed_revision: str
    cases_digest: str
    run_id: str | None = None
    terminal_status: str | None = None
    reviewer_decision: str | None = None
    #: The answer the batch gave when the run stopped in the human review queue, if it did.
    #:
    #: A run at ``waiting_review`` is waiting for a person and will not move again until one
    #: answers, so the batch answers -- as the approver identity a deployment routes the
    #: escalation to -- and settles again. Recorded because it is part of how the run
    #: reached its terminal state, and ``None`` because most runs never raise an escalation:
    #: a reader must be able to tell "no human was needed" from "a human was consulted".
    human_review_decision: str | None = None
    citations: tuple[str, ...] = ()
    #: Who the platform was actually told the caller was, read from the token's own claims.
    #:
    #: Recorded because the must-refuse-access class is only that if the asker really cannot
    #: reach the document. If the token carried a different subject -- or the same name with
    #: different groups -- those twenty cases would still pass, having asserted nothing. The
    #: name is compared here; the groups are checked by the runner against the corpus, which
    #: is the only place that knows which document is restricted to what.
    observed_username: str | None = None
    #: Rows the reader could not turn into a citation. Carried rather than dropped: a run
    #: whose only defect is a malformed citation must not look like a run that cited
    #: exactly what was expected.
    unreadable_citations: int = 0
    #: What the run's own review recorded about claims its evidence does not carry. Read only
    #: by the ``insufficient-evidence`` rule, and only because that class has no external
    #: check available; see ``quality.ReviewSignals`` for what the signal is worth. ``-1``
    #: means the field was not there to read, which is not the same as a zero.
    unsupported_claims: int = -1
    missing_evidence: int = -1
    proposed_actions: int = -1
    review_findings: int = -1
    errors: tuple[str, ...] = ()
    elapsed_seconds: float | None = None


class CaseOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    kind: CaseKind
    subject: str
    question: str
    verdict: Verdict
    findings: tuple[str, ...]


class QualityOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    generated_at: datetime
    verdict: Verdict
    cases_digest: str
    observations_digest: str
    answerable_rate_target: float
    answerable_rate_target_source: str
    cases: tuple[CaseOutcome, ...]
    blockers: tuple[str, ...]

    def counts(self) -> dict[str, int]:
        totals = {name: 0 for name in (item.value for item in Verdict)}
        for case in self.cases:
            totals[case.verdict.value] += 1
        return totals

    def counts_by_kind(self) -> dict[str, dict[str, int]]:
        grouped: dict[str, dict[str, int]] = {
            kind: {name: 0 for name in (item.value for item in Verdict)}
            for kind in REQUIRED_COMPOSITION
        }
        for case in self.cases:
            grouped[case.kind.value][case.verdict.value] += 1
        return grouped

    def answerable_rate(self) -> float | None:
        """The end-to-end answerable rate, over every answerable case, or None.

        The denominator is all 120, always -- a rate over whichever subset happened to be
        observable is not the rate the case list registered a target for, and shrinking the
        denominator to the cases that behaved is how a batch gets a good number by
        discarding the bad ones.

        ``None`` rather than 0.0 when nothing was observed. Zero answers out of 120 cases
        that were never run is not a measurement of zero; it is the absence of a
        measurement, and rendering the two the same way is how a batch that did not run
        reads as a batch that ran badly.
        """
        rows = [case for case in self.cases if case.kind is CaseKind.ANSWERABLE]
        observed = [case for case in rows if case.verdict is not Verdict.BLOCKED]
        if not observed:
            return None
        return sum(1 for case in rows if case.verdict is Verdict.PASS) / len(rows)

    def answerable_interval(self) -> tuple[float, float] | None:
        """The Wilson score interval for that rate, or None when the batch has holes.

        Wilson rather than the normal approximation: at these counts and at rates near the
        ends, ``p +/- 1.96*sqrt(p(1-p)/n)`` produces bounds outside [0, 1] and reads as more
        certainty than 120 binary outcomes carry.

        Refused outright if any answerable case is BLOCKED. The interval describes a sample
        of independent binary outcomes, and a BLOCKED case is not one of those -- it is a
        missing observation. Widening the interval cannot express that; the honest answer is
        that no interval can be quoted for a batch with holes in it.
        """
        rows = [case for case in self.cases if case.kind is CaseKind.ANSWERABLE]
        if not rows or any(case.verdict is Verdict.BLOCKED for case in rows):
            return None
        successes = sum(1 for case in rows if case.verdict is Verdict.PASS)
        return wilson_interval(successes, len(rows))


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total <= 0:
        raise ValueError("a confidence interval needs at least one observation")
    phat = successes / total
    denominator = 1 + z * z / total
    centre = (phat + z * z / (2 * total)) / denominator
    spread = z / denominator * math.sqrt(phat * (1 - phat) / total + z * z / (4 * total * total))
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def _not_observed(observation: CaseObservation) -> str | None:
    """Why this run is not something to judge, or None when it is.

    Checked before any class rule, because every class rule reads a field that a run which
    never got there does not have -- and each of those reads would otherwise fail *open*:
    a run with no reviewer decision trivially satisfies "the reviewer did not pass it", and
    a run with no citations trivially satisfies "it did not cite the restricted document".
    Both of those are the vacuous pass the acceptance work already rejected once.
    """
    if observation.errors:
        return (
            f"{observation.case_id}: the driver recorded errors ({'; '.join(observation.errors)})"
        )
    if observation.terminal_status not in TERMINAL_STATUSES:
        return (
            f"{observation.case_id}: the run did not reach a terminal status "
            f"(saw {observation.terminal_status!r})"
        )
    if observation.unreadable_citations:
        return (
            f"{observation.case_id}: {observation.unreadable_citations} citation rows would not "
            "validate, so the set of documents this run cited is not known"
        )
    return None


def _judge(case: QualityCase, observation: CaseObservation) -> tuple[Verdict, tuple[str, ...]]:
    missing = _not_observed(observation)
    if missing is not None:
        return Verdict.BLOCKED, (missing,)

    if observation.observed_username is not None and observation.observed_username != case.subject:
        # The run happened, but not as the subject the case is about. Every assertion in this
        # module is about what happens when *this* identity asks, so a run by another one is
        # not a weaker observation -- it is an observation of a different case.
        return Verdict.BLOCKED, (
            f"{case.id}: the run was made as {observation.observed_username!r}, but the case "
            f"is about {case.subject!r}, so it did not test what it claims to",
        )

    cited = set(observation.citations)
    expected = set(case.expected_citations)
    forbidden = set(case.forbidden_citations)
    findings: list[str] = []

    match case.kind:
        case CaseKind.ANSWERABLE:
            answered = (
                observation.terminal_status == "succeeded"
                and observation.reviewer_decision == ASSERTING_DECISION
            )
            if not answered:
                findings.append(
                    f"{case.id}: did not answer. terminal={observation.terminal_status!r}, "
                    f"review={observation.reviewer_decision!r}"
                )
            absent = sorted(expected - cited)
            if absent:
                findings.append(
                    f"{case.id}: the answer is in {absent} and the run did not cite it "
                    f"(cited {sorted(cited)})"
                )
            return (Verdict.FAIL if findings else Verdict.PASS), tuple(findings)

        case CaseKind.VERSION_CONFLICT:
            # The hard half: a superseded procedure must never be cited, whatever else the
            # run said. Checked even when the run did not answer, because citing it is the
            # defect regardless of whether the answer was any good.
            stale = sorted(forbidden & cited)
            if stale:
                findings.append(f"{case.id}: cited the superseded document {stale}")
            answered = (
                observation.terminal_status == "succeeded"
                and observation.reviewer_decision == ASSERTING_DECISION
            )
            if not answered:
                findings.append(
                    f"{case.id}: did not answer. terminal={observation.terminal_status!r}, "
                    f"review={observation.reviewer_decision!r}"
                )
            absent = sorted(expected - cited)
            if absent:
                findings.append(f"{case.id}: did not cite the current document {absent}")
            return (Verdict.FAIL if findings else Verdict.PASS), tuple(findings)

        case CaseKind.MUST_REFUSE_ACCESS:
            # A reviewer decision is required before this can pass. Without one the run
            # never evaluated anything, and "it did not cite the restricted document" is
            # true of a run that read nothing at all -- the empty-result-as-isolation-
            # proof failure, which this class exists to be the opposite of.
            if observation.reviewer_decision is None:
                return Verdict.BLOCKED, (
                    f"{case.id}: the run reached no reviewer decision, so it never evaluated "
                    "evidence and its silence about the restricted document proves nothing",
                )
            leaked = sorted(forbidden & cited)
            if leaked:
                findings.append(
                    f"{case.id}: {case.subject} was shown the restricted document {leaked}"
                )
            return (Verdict.FAIL if findings else Verdict.PASS), tuple(findings)

        case CaseKind.INSUFFICIENT_EVIDENCE:
            # The question is one the corpus deliberately cannot answer, so there is no
            # expected document to check against and the fabrication has to be caught by
            # reading what the run asserted. These are the readings of that.
            #
            # ``reviewer_decision == "passed"`` is deliberately *not* one of them. It was,
            # until a smoke run over the seeded corpus falsified it: the reviewer's feedback
            # for a case the platform handled exactly right said "The analysis correctly
            # declines to invent an ordering procedure absent from the evidence" -- the
            # decision is about whether the claims are supported, not about whether the
            # question was answered, so failing on ``passed`` failed every correct refusal.
            if observation.terminal_status != "succeeded":
                findings.append(
                    f"{case.id}: the run did not reach a resting success "
                    f"(terminal={observation.terminal_status!r}); a question the corpus cannot "
                    "answer is still one the platform is expected to process"
                )
            unsupported = observation.unsupported_claims
            if unsupported < 0:
                # Recorded as BLOCKED rather than PASS: a run whose review shape this reader
                # does not recognise is a run nothing was learned about, and a silent zero
                # here is indistinguishable from a genuine "no fabrication found".
                return Verdict.BLOCKED, (
                    f"{case.id}: the run's review does not carry unsupported_claims in a "
                    "shape this rule can read, so whether anything was fabricated is unknown",
                )
            if unsupported:
                findings.append(
                    f"{case.id}: the reviewer recorded {unsupported} claim(s) its citations "
                    f"do not support, on a question the corpus does not cover "
                    f"(cited {sorted(cited)})"
                )
            if observation.proposed_actions > 0:
                findings.append(
                    f"{case.id}: the analysis proposed {observation.proposed_actions} action(s) "
                    "for a question the corpus does not answer, so the steps came from "
                    "somewhere other than the evidence"
                )
            return (Verdict.FAIL if findings else Verdict.PASS), tuple(findings)

    raise AssertionError(f"unhandled case kind {case.kind!r}")


def grade(
    case_set: QualityCaseSet,
    observations: Iterable[CaseObservation],
    *,
    generated_at: datetime,
) -> QualityOutcome:
    recorded = list(observations)
    by_id = {observation.case_id: observation for observation in recorded}
    outcomes: list[CaseOutcome] = []
    blockers: list[str] = []

    for case in case_set.cases:
        observation = by_id.get(case.id)
        if observation is None:
            finding = f"{case.id}: no observation was recorded for this case"
            blockers.append(finding)
            outcomes.append(
                CaseOutcome(
                    case_id=case.id,
                    kind=case.kind,
                    subject=case.subject,
                    question=case.question,
                    verdict=Verdict.BLOCKED,
                    findings=(finding,),
                )
            )
            continue
        verdict, findings = _judge(case, observation)
        blockers.extend(findings)
        outcomes.append(
            CaseOutcome(
                case_id=case.id,
                kind=case.kind,
                subject=case.subject,
                question=case.question,
                verdict=verdict,
                findings=findings,
            )
        )

    counts = {name: 0 for name in (item.value for item in Verdict)}
    for outcome in outcomes:
        counts[outcome.verdict.value] += 1

    answerable = [case for case in outcomes if case.kind is CaseKind.ANSWERABLE]
    answered = sum(1 for case in answerable if case.verdict is Verdict.PASS)
    rate = (answered / len(answerable)) if answerable else None

    # The three hard classes and the answerable class are decided by different rules, and
    # this is where the report's own claim ("three absolute, one rate") is either true or
    # not. Getting it the other way round is easy and silent: if a single unanswered
    # answerable case ended the batch by itself, the registered threshold could only ever
    # be met at a rate of exactly 1.0, and the number in the case list would be decoration.
    #
    # So: a FAIL in a hard class ends the batch -- one fabrication, one retired procedure
    # cited, one leaked document is not a rate. A FAIL in the answerable class is counted,
    # not fatal: it is precisely what the threshold is a threshold on. BLOCKED anywhere
    # outranks the threshold, because a rate computed over cases nobody measured is the
    # absence of a measurement and not a low one.
    hard_failures = [
        case
        for case in outcomes
        if case.verdict is Verdict.FAIL and case.kind is not CaseKind.ANSWERABLE
    ]
    if hard_failures:
        overall = Verdict.FAIL
        blockers.append(
            f"{len(hard_failures)} case(s) in an absolute class failed, and those classes "
            "have no rate at which a violation becomes acceptable"
        )
    elif counts[Verdict.BLOCKED.value]:
        overall = Verdict.BLOCKED
    elif rate is not None and rate < case_set.answerable_rate_target:
        overall = Verdict.FAIL
        blockers.append(
            f"the end-to-end answerable rate is {rate:.4f}, below the "
            f"{case_set.answerable_rate_target:.2f} registered in the case list before the "
            f"batch ran ({answered}/{len(answerable)} answered)"
        )
    else:
        overall = Verdict.PASS

    return QualityOutcome(
        schema_version=case_set.schema_version,
        generated_at=generated_at,
        verdict=overall,
        cases_digest=case_set_digest(case_set),
        # Sorted by case id before digesting, because the set of observations is unordered
        # and the digest is a statement about the observations rather than about the order
        # a runner happened to finish them in. A runner that finishes case 40 before case 39
        # on a different day would otherwise produce a different digest for the same batch
        # and trip the report-integrity check for no reason.
        observations_digest=observation_digest(
            [
                observation.model_dump(mode="python")
                for observation in sorted(recorded, key=lambda item: item.case_id)
            ]
        ),
        answerable_rate_target=case_set.answerable_rate_target,
        answerable_rate_target_source=case_set.answerable_rate_target_source,
        cases=tuple(outcomes),
        blockers=tuple(blockers),
    )


def dump_outcome(outcome: QualityOutcome) -> str:
    return json.dumps(outcome.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)


def outcome_summary(outcome: QualityOutcome) -> dict[str, Any]:
    rate = outcome.answerable_rate()
    interval = outcome.answerable_interval()
    answerable = [case for case in outcome.cases if case.kind is CaseKind.ANSWERABLE]
    return {
        "verdict": outcome.verdict.value,
        "counts": outcome.counts(),
        "counts_by_kind": outcome.counts_by_kind(),
        "answerable_rate": rate,
        "answerable_rate_target": outcome.answerable_rate_target,
        "answerable_rate_interval_95": list(interval) if interval else None,
        # Travels beside the rate so that a rate over a batch with holes is never read as a
        # rate over the whole class. ``answerable_rate`` being None means every case in the
        # class was unobserved, which is a different statement again.
        "answerable_unobserved": sum(1 for case in answerable if case.verdict is Verdict.BLOCKED),
        "answerable_total": len(answerable),
        "cases_digest": outcome.cases_digest,
        "observations_digest": outcome.observations_digest,
        "blockers": list(outcome.blockers),
    }


def render_report(outcome: QualityOutcome) -> str:
    counts = outcome.counts()
    by_kind = outcome.counts_by_kind()
    rate = outcome.answerable_rate()
    interval = outcome.answerable_interval()

    lines = [
        "# P7.6.6 业务质量集 —— 端到端观测报告",
        "",
        "> 本文件由 `scripts/gate_phase7_quality.py` 依据 `evaluation/quality/replays/` 的观测生成，"
        "**不得手写**。案例清单改了而报告没重跑，gate 以退出码 3 拒绝。",
        "",
        f"- 生成时间：`{outcome.generated_at.isoformat()}`",
        f"- 案例清单摘要：`{outcome.cases_digest}`",
        f"- 观测摘要：`{outcome.observations_digest}`",
        f"- 判定：**{outcome.verdict.value}** —— "
        + " / ".join(f"{name} {counts[name]}" for name in ("PASS", "FAIL", "BLOCKED")),
        "",
        "## 核心数字：端到端 Reviewer 可答率",
        "",
    ]

    if rate is None:
        lines += ["未能计算：本批次没有可答类观测。"]
    else:
        lines += [
            f"- 实测：**{rate:.4f}**（{sum(1 for c in outcome.cases if c.kind is CaseKind.ANSWERABLE and c.verdict is Verdict.PASS)}"
            f" / {REQUIRED_COMPOSITION['answerable']}）",
            f"- 预先登记的门槛：**{outcome.answerable_rate_target:.2f}**",
            f"- 来源：{outcome.answerable_rate_target_source}",
            (f"- 95% Wilson 区间：**[{interval[0]:.4f}, {interval[1]:.4f}]**" if interval else ""),
            "",
            "**这个数字取代的是什么。** `0.275` 是**检索 top-score 阈值代理**，"
            "由检索分数算出，其中没有模型、没有复核器、也没有答案——"
            "`scripts/audit_rag_quality_state.py` 已如此标注，其 "
            "`answerability_signal.is_end_to_end_reviewer_measurement` 为 `false`。"
            "两者不是同一个测量，代理值也**不是**本值的下界：一个说的是「检索分数的分布」，"
            "另一个说的是「真实用户提问后被正确作答的比例」。",
            "",
            "**区间比点估计重要。** 120 条的二值结果承载的确定性有限，"
            "只报点估计会把区间宽度藏起来。",
        ]

    lines += [
        "",
        "## 按类别",
        "",
        "| 类别 | 条数 | PASS | FAIL | BLOCKED | 判定方式 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    verdict_kind = {
        "answerable": "速率，对照预先登记的门槛",
        # Not "must not answer": whether it answered is not readable from the observation.
        # What is readable is the platform's own record of unsupported claims.
        "insufficient-evidence": "绝对：不得有「证据不支持」的断言或凭空提出的动作",
        "version-conflict": "绝对：必须不引用已废止版本",
        "must-refuse-access": "绝对：必须不出现受组限制的文档",
    }
    for kind in REQUIRED_COMPOSITION:
        row = by_kind[kind]
        lines.append(
            f"| {kind} | {sum(row.values())} | {row['PASS']} | {row['FAIL']} | "
            f"{row['BLOCKED']} | {verdict_kind[kind]} |"
        )

    failed = [case for case in outcome.cases if case.verdict is Verdict.FAIL]
    blocked = [case for case in outcome.cases if case.verdict is Verdict.BLOCKED]

    if failed:
        lines += ["", f"## 未通过（{len(failed)} 条）", ""]
        for case in failed:
            lines.append(
                f"- **{case.case_id}**（{case.kind.value}，{case.subject}）{case.question}"
            )
            for finding in case.findings:
                lines.append(f"  - {finding}")

    if blocked:
        lines += ["", f"## 未观测到（{len(blocked)} 条）——不是通过，也不是失败", ""]
        for case in blocked[:40]:
            lines.append(
                f"- **{case.case_id}**（{case.kind.value}）{case.findings[0] if case.findings else ''}"
            )
        if len(blocked) > 40:
            lines.append(f"- …其余 {len(blocked) - 40} 条见 JSON 报告")

    lines += [
        "",
        "## 本批次**不**证明的内容",
        "",
        "- 语料是自建的 44 篇文档，不是租户的真实知识库。可答率是在这个语料上测的，"
        "换一个语料会得到另一个数字——它衡量的是「在这个已声明语料上端到端是否正确」，"
        "不是「在任何语料上都正确」。",
        "- 40 条证据不足的问题由**本清单作者**判定为「语料无法回答」。"
        "其中若有任何一条其实能从语料推出答案，那条就会把一次**正确作答**判成一次**不合格**。"
        "这个判定没有第二方复核。",
        "- 证据不足这一类**无法独立判定「是否作答」**。可答类有一条外部判据（是否引用到"
        "指定的那篇文档），这一类没有——语料故意不含答案，就没有可比对的期望文档。"
        "因此它只能读平台自己发布的编造信号：`review.unsupported_claims` 与 "
        "`analysis.proposed_actions`。**它抓得住的是平台自己复核出的编造**；"
        "平台漏掉的编造，这一类抓不住。这是结构性限制，不是配置问题。",
        "- 只测了读取路径。本批次所有 run 都是 `request_write=false`，"
        "没有覆盖审批、写入、回读的任何一步——那是 ACC-09/10/11 的范围。",
        "- 只用了两个主体（各持一个组）。组轴上的结论来自这 20 条拒绝访问案例，"
        "不覆盖实体轴与角色轴。",
        "- 延迟只作为观测量记录，未纳入判定；负载与长稳属于 P7.6.7。",
    ]

    if outcome.blockers:
        lines += ["", "## 阻断项明细", ""] + [f"- {item}" for item in outcome.blockers]

    lines.append("")
    return "\n".join(lines)
