"""The pure judge over recorded security-scenario evidence. No I/O, no stack, no clock.

Same split as ``acceptance_grader``: the runner records, the gate judges, and a recorded
observation plus a scenario list is a complete description of the outcome. CI can re-derive
every verdict in this file without a database, a model or a network.

Three verdicts, and the third is not a failure:

``PASS``     every piece of evidence the scenario names came back green.
``FAIL``     a named test failed, a named script exited non-zero, or a mutation removed the
             behaviour and the test that is supposed to notice stayed green. The last one
             is a finding about the test rather than the code, and it is graded FAIL rather
             than BLOCKED on purpose: the platform has a behaviour whose only pin has no
             teeth, which is a defect in the suite today, not a run that did not happen.
``BLOCKED``  the evidence exists but was not produced -- a skipped test, a missing replay,
             a mutation that broke the file instead of changing it. Nothing was learned,
             which is what BLOCKED is for, and it is never folded into PASS.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from servicemind.evaluation.security import (
    EvidenceKind,
    SecurityScenarioSet,
    observation_digest,
    scenario_set_digest,
)


class EvidenceOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    #: The mutation removed the behaviour and the pinned tests stayed green.
    UNDETECTED = "undetected"
    #: The evidence was named but never produced -- no replay, no script on disk.
    MISSING = "missing"
    #: The pinned tests all skipped: the behaviour was never reached.
    UNEXERCISED = "unexercised"
    #: The mutation left the file unparseable, so nothing was graded.
    UNRUNNABLE = "unrunnable"


class Verdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


#: Which verdict an evidence outcome forces. Stated once, as a table: a branch per call
#: site is how "the mutation was not detected" and "the test failed" drift into the same
#: verdict, and they call for opposite responses.
_VERDICT_FOR_OUTCOME: dict[EvidenceOutcome, Verdict] = {
    EvidenceOutcome.PASSED: Verdict.PASS,
    EvidenceOutcome.FAILED: Verdict.FAIL,
    EvidenceOutcome.UNDETECTED: Verdict.FAIL,
    EvidenceOutcome.MISSING: Verdict.BLOCKED,
    EvidenceOutcome.UNEXERCISED: Verdict.BLOCKED,
    EvidenceOutcome.UNRUNNABLE: Verdict.BLOCKED,
}


class ObservedEvidence(BaseModel):
    """What one piece of a scenario's evidence did when it was run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EvidenceKind
    ref: str
    mutation: str | None = None
    outcome: EvidenceOutcome
    #: The last line of the test run, the mutation's own verdict line, or why it is
    #: missing. Recorded so a reader can see what "passed" was passed *on*.
    detail: str = ""

    @property
    def identity(self) -> tuple[EvidenceKind, str, str | None]:
        return (self.kind, self.ref, self.mutation)


class ScenarioObservation(BaseModel):
    """One scenario's recorded evidence, as the runner saw it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str
    observed_at: datetime
    deployed_revision: str
    scenarios_digest: str
    evidence: tuple[ObservedEvidence, ...]


class ScenarioVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str
    category: str
    title: str
    verdict: Verdict
    #: The scenario is backed by a mutation that turned its pin red. A scenario without
    #: this is not wrong -- some behaviours are only pinned by a passing test -- but the
    #: report counts them separately, because "the test is green" and "the test would go
    #: red" are different strengths of claim and only the second survives a refactor.
    has_teeth: bool
    findings: tuple[str, ...]


class SecurityOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    generated_at: datetime
    verdict: Verdict
    scenarios_digest: str
    observations_digest: str
    cases: tuple[ScenarioVerdict, ...]
    blockers: tuple[str, ...]
    #: Carried into the report rather than left in the scenario list, because the report is
    #: what a reader sees: a green table with the exclusions in another file is a table
    #: that reads as complete.
    not_covered: tuple[str, ...]
    #: The code these observations describe, read off the observations rather than from the
    #: tree the gate happens to run in. A report that named only the revision of the process
    #: generating it would describe the checkout, not the thing that was measured, and the
    #: two differ exactly when it matters: after somebody edits the source and re-runs the
    #: gate without re-running the batch.
    deployed_revisions: tuple[str, ...] = ()
    #: When the observations were taken, as opposed to when they were graded. ``generated_at``
    #: is the gate's clock; a report that showed only that would date a week-old batch today.
    observed_from: datetime | None = None
    observed_to: datetime | None = None

    def counts(self) -> dict[str, int]:
        totals = {name: 0 for name in (item.value for item in Verdict)}
        for case in self.cases:
            totals[case.verdict.value] += 1
        return totals


def _describe(outcome: EvidenceOutcome, where: str, scenario_id: str, detail: str) -> str:
    match outcome:
        case EvidenceOutcome.FAILED:
            return f"{scenario_id}: {where} failed ({detail})"
        case EvidenceOutcome.UNDETECTED:
            return (
                f"{scenario_id}: {where} was removed and the pinned test stayed green -- "
                f"this scenario has no teeth ({detail})"
            )
        case EvidenceOutcome.MISSING:
            return f"{scenario_id}: {where} produced no observation ({detail})"
        case EvidenceOutcome.UNEXERCISED:
            return f"{scenario_id}: {where} ran nothing but skips ({detail})"
        case EvidenceOutcome.UNRUNNABLE:
            return f"{scenario_id}: {where} did not grade, the mutation was not applied ({detail})"
        case EvidenceOutcome.PASSED:
            return ""


def _where(kind: EvidenceKind, ref: str, mutation: str | None) -> str:
    return f"{ref}::{mutation}" if mutation else f"{ref} [{kind.value}]"


def grade(
    scenario_set: SecurityScenarioSet,
    observations: Iterable[ScenarioObservation],
    *,
    generated_at: datetime,
) -> SecurityOutcome:
    """Judge every scenario in the set against the observations recorded for it.

    A scenario with no recorded observation is BLOCKED, not skipped and not PASS: the
    set says it has evidence, and a report that silently dropped the rows it could not
    find would show a shorter table with no indication that anything is absent.
    """
    recorded_observations = list(observations)
    by_id = {observation.scenario_id: observation for observation in recorded_observations}
    verdicts: list[ScenarioVerdict] = []
    blockers: list[str] = []

    for scenario in scenario_set.scenarios:
        observation = by_id.get(scenario.id)
        if observation is None:
            finding = f"{scenario.id}: no observation was recorded for this scenario"
            blockers.append(finding)
            verdicts.append(
                ScenarioVerdict(
                    scenario_id=scenario.id,
                    category=scenario.category,
                    title=scenario.title,
                    verdict=Verdict.BLOCKED,
                    has_teeth=False,
                    findings=(finding,),
                )
            )
            continue

        declared = {
            item.kind.value + "\0" + item.ref + "\0" + (item.mutation or ""): item
            for item in scenario.evidence
        }
        findings: list[str] = []
        forced: list[Verdict] = []

        # An observation that does not cover what the scenario declares is a gap in the
        # run, not in the platform, and it must be named here rather than inferred from a
        # count -- the two sets can differ in either direction.
        seen = {
            item.kind.value + "\0" + item.ref + "\0" + (item.mutation or "")
            for item in observation.evidence
        }
        for key, item in declared.items():
            if key not in seen:
                findings.append(
                    f"{scenario.id}: declared evidence {_where(item.kind, item.ref, item.mutation)} "
                    "was not run"
                )
                forced.append(Verdict.BLOCKED)

        for item in observation.evidence:
            key = item.kind.value + "\0" + item.ref + "\0" + (item.mutation or "")
            if key not in declared:
                findings.append(
                    f"{scenario.id}: {_where(item.kind, item.ref, item.mutation)} was run but the "
                    "scenario does not declare it"
                )
                forced.append(Verdict.FAIL)
                continue
            if item.outcome is EvidenceOutcome.PASSED:
                continue
            forced.append(_VERDICT_FOR_OUTCOME[item.outcome])
            findings.append(
                _describe(
                    item.outcome,
                    _where(item.kind, item.ref, item.mutation),
                    scenario.id,
                    item.detail,
                )
            )

        if Verdict.FAIL in forced:
            verdict = Verdict.FAIL
        elif Verdict.BLOCKED in forced:
            verdict = Verdict.BLOCKED
        else:
            verdict = Verdict.PASS
        blockers.extend(findings)

        has_teeth = any(
            item.kind is EvidenceKind.MUTATION and item.outcome is EvidenceOutcome.PASSED
            for item in observation.evidence
        )
        # A scenario whose evidence is green but whose teeth were never exercised is a
        # note, not a blocker: it is reported, and the report counts it, but the platform
        # is not broken by it and grading it FAIL would make the release gate depend on
        # having written a mutation for every control.
        verdicts.append(
            ScenarioVerdict(
                scenario_id=scenario.id,
                category=scenario.category,
                title=scenario.title,
                verdict=verdict,
                has_teeth=has_teeth,
                findings=tuple(findings),
            )
        )

    overall = (
        Verdict.FAIL
        if any(case.verdict is Verdict.FAIL for case in verdicts)
        else Verdict.BLOCKED
        if any(case.verdict is Verdict.BLOCKED for case in verdicts)
        else Verdict.PASS
    )

    # A batch recorded across more than one revision is not a weaker measurement of one
    # build, it is measurements of two, and every scenario in it is graded against whichever
    # one its own replay happens to name. This is reachable in ordinary use -- re-run a few
    # scenarios with ``--only`` after editing the source and the rest keep their old stamp --
    # and it is the one thing a reader cannot recover from the table, since a green row looks
    # identical either way. BLOCKED rather than FAIL: nothing was learned, the platform is
    # not implicated. Every observation the runner writes carries the same revision because
    # it is read once at the start of the sweep, so this can only mean two runs.
    revisions = sorted({observation.deployed_revision for observation in recorded_observations})
    if len(revisions) > 1:
        blockers.append(
            "these observations were recorded against more than one revision "
            f"({', '.join(revisions)}); no single build is described by this batch -- "
            "re-run scripts/verify_phase7_security.py over the whole set"
        )
        if overall is Verdict.PASS:
            overall = Verdict.BLOCKED

    observed = [observation.observed_at for observation in recorded_observations]
    return SecurityOutcome(
        schema_version=scenario_set.schema_version,
        generated_at=generated_at,
        verdict=overall,
        scenarios_digest=scenario_set_digest(scenario_set),
        observations_digest=observation_digest(
            [observation.model_dump(mode="python") for observation in recorded_observations]
        ),
        cases=tuple(verdicts),
        blockers=tuple(blockers),
        not_covered=scenario_set.not_covered,
        deployed_revisions=tuple(revisions),
        observed_from=min(observed) if observed else None,
        observed_to=max(observed) if observed else None,
    )


def dump_outcome(outcome: SecurityOutcome) -> str:
    return json.dumps(outcome.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)


def render_report(outcome: SecurityOutcome) -> str:
    counts = outcome.counts()
    teeth = [case for case in outcome.cases if case.has_teeth]
    no_teeth = [case for case in outcome.cases if not case.has_teeth]
    by_category: dict[str, list[ScenarioVerdict]] = {}
    for case in outcome.cases:
        by_category.setdefault(case.category, []).append(case)

    lines = [
        "# P7.6.5 安全与故障场景集 —— 观测报告",
        "",
        "> 本文件由 `scripts/gate_phase7_security.py` 依据 `evaluation/security/replays/` "
        "中的观测生成，**不得手写**。修改场景后必须重跑，否则 gate 以退出码 3 拒绝。",
        "",
        f"- 生成时间：`{outcome.generated_at.isoformat()}`（gate 运行时刻，非观测时刻）",
        "- 被测版本："
        + (
            " / ".join(f"`{revision}`" for revision in outcome.deployed_revisions)
            if outcome.deployed_revisions
            else "**未记录**（没有观测文件，本报告没有描述任何构建）"
        ),
        "- 观测时间窗："
        + (
            f"`{outcome.observed_from.isoformat()}` → `{outcome.observed_to.isoformat()}`"
            if outcome.observed_from and outcome.observed_to
            else "**未记录**"
        ),
        f"- 场景清单摘要：`{outcome.scenarios_digest}`",
        f"- 观测摘要：`{outcome.observations_digest}`",
        f"- 判定：**{outcome.verdict.value}** —— "
        + " / ".join(f"{name} {counts[name]}" for name in ("PASS", "FAIL", "BLOCKED")),
        "",
        f"- 有变异背书的场景（`teeth`）：**{len(teeth)} / {len(outcome.cases)}**。"
        "其余场景只由通过的测试背书——「测试是绿的」与「删掉这段行为测试会变红」"
        "**不是同一个强度的结论**，报告分列，不合并。",
        "",
        "## 按类别",
        "",
        "| 类别 | 场景数 | PASS | FAIL | BLOCKED |",
        "| --- | --- | --- | --- | --- |",
    ]
    for category in sorted(by_category):
        rows = by_category[category]
        lines.append(
            f"| {category} | {len(rows)} | "
            f"{sum(1 for row in rows if row.verdict is Verdict.PASS)} | "
            f"{sum(1 for row in rows if row.verdict is Verdict.FAIL)} | "
            f"{sum(1 for row in rows if row.verdict is Verdict.BLOCKED)} |"
        )

    lines += ["", "## 逐条场景", ""]
    for case in outcome.cases:
        backing = "有变异背书" if case.has_teeth else "仅由通过的测试背书"
        lines.append(
            f"- **{case.scenario_id}**（{case.category}）{case.title} — **{case.verdict.value}**（{backing}）"
        )
        for finding in case.findings:
            lines.append(f"  - {finding}")

    if no_teeth:
        lines += ["", "## 无变异背书的场景（不是缺陷，是覆盖强度的说明）", ""]
        lines += [f"- {case.scenario_id} — {case.title}" for case in no_teeth]
    if outcome.blockers:
        lines += ["", "## 阻断项", ""] + [f"- {item}" for item in outcome.blockers]
    lines += [
        "",
        "## 本场景集**不**覆盖的内容（手写，非机器生成）",
        "",
        "下面是本表**没有**背书的威胁。它按清单作者的判断写就，因此它可能不完整——"
        "把「表有多长」读成「威胁面有多大」是这张表最容易造成的误读，这一节就是用来挡住它的。",
        "",
    ]
    lines += [f"- {item}" for item in outcome.not_covered]
    lines.append("")
    return "\n".join(lines)


def outcome_summary(outcome: SecurityOutcome) -> dict[str, Any]:
    return {
        "verdict": outcome.verdict.value,
        "counts": outcome.counts(),
        "scenarios": {case.scenario_id: case.verdict.value for case in outcome.cases},
        "with_teeth": sum(1 for case in outcome.cases if case.has_teeth),
        "without_teeth": [case.scenario_id for case in outcome.cases if not case.has_teeth],
        "blockers": list(outcome.blockers),
        "scenarios_digest": outcome.scenarios_digest,
        "observations_digest": outcome.observations_digest,
        "deployed_revisions": list(outcome.deployed_revisions),
        "observed_from": outcome.observed_from.isoformat() if outcome.observed_from else None,
        "observed_to": outcome.observed_to.isoformat() if outcome.observed_to else None,
    }
