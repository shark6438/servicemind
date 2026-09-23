#!/usr/bin/env python
"""The P7.6 acceptance gate, and the machine-generated record of the run.

Run it with no arguments to judge the replays already on disk against the frozen case
list and rewrite the report::

    uv run python scripts/gate_phase7_acceptance.py --check

Exit codes, which are the interface:

``0``
    Every blocking case passed.
``1``
    Something blocking failed. The verdict is the platform's, not the harness's.
``2``
    Not enough was observed. A case with no replay, or an acceptance that was never
    run, is neither a pass nor a failure.
``3``
    The gate itself is misconfigured: the case list does not validate, a replay names
    a case that is not in it, or the report on disk was generated from a different case
    list than the one it sits beside.

Why ``3`` exists as its own code rather than folding into ``1``: a report and a case
list drift apart silently. Somebody edits a case's expectation, the replays still
describe the old one, and a gate that only returned PASS/FAIL would keep printing the
old verdict as though it were about the new text. Binding the report to
``cases_digest`` and refusing to render when they disagree turns that into a loud
failure at the one moment somebody can still see what changed.

What this file is *not*: it does not run the acceptance. ``--live`` is accepted only
so the documented command line does not lie about what happened, and it is rejected
with an explanation, because a gate that quietly grades yesterday's replays while its
invocation says "--live" is worse than one that refuses.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from servicemind.evaluation.acceptance import (
    AcceptanceCaseSet,
    CaseExecution,
    case_set_digest,
    execution_digest,
)
from servicemind.evaluation.acceptance_grader import (
    AcceptanceOutcome,
    Verdict,
    dump_outcome,
    grade,
    render_coverage_markdown,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

CASES = REPO_ROOT / "evaluation" / "acceptance" / "cases.v1.json"
REPLAYS = REPO_ROOT / "evaluation" / "acceptance" / "replays"
REPORTS = REPO_ROOT / "evaluation" / "reports"
REPORT_JSON = REPORTS / "phase7_acceptance_latest.json"
REPORT_MD = REPORTS / "phase7_acceptance_latest.md"
PIPELINE = REPORTS / "phase7_pipeline_evidence.json"

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INSUFFICIENT = 2
EXIT_CONFIGURATION = 3

#: Which replay fields a reader needs to see to believe a case, in the order they
#: would ask for them. Named here rather than inlined so the report's per-case section
#: and its reader cannot disagree about what counts as raw evidence.
EVIDENCE_FIELDS = (
    "run_id",
    "terminal_status",
    "total_seconds",
    "followups_before",
    "followups_after",
    "citations",
    "selection_manifest",
    "memory_records",
    "graph_readings",
)


class ConfigurationError(Exception):
    """The gate cannot judge, as opposed to having judged and disliked the answer."""


def load_case_set() -> AcceptanceCaseSet:
    if not CASES.exists():
        raise ConfigurationError(f"case list is missing: {CASES}")
    try:
        return AcceptanceCaseSet.model_validate(json.loads(CASES.read_text(encoding="utf-8")))
    except ValidationError as exc:
        raise ConfigurationError(f"{CASES} does not validate: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{CASES} is not valid JSON: {exc}") from exc


def load_replays(case_set: AcceptanceCaseSet) -> list[CaseExecution]:
    """Read every replay, refusing any that does not belong to this case list.

    A replay for a case that no longer exists is not ignorable: it is either a case
    that was deleted without deleting its evidence, or a typo in a case id, and both
    mean the report would describe a case list nobody has.
    """
    if not REPLAYS.is_dir():
        raise ConfigurationError(f"no replays directory: {REPLAYS}")
    known = {case.id for case in case_set.cases}
    executions: list[CaseExecution] = []
    for path in sorted(REPLAYS.glob("*.json")):
        try:
            execution = CaseExecution.model_validate_json(path.read_text(encoding="utf-8"))
        except ValidationError as exc:
            raise ConfigurationError(f"{path} does not validate as a CaseExecution: {exc}") from exc
        if execution.case_id not in known:
            raise ConfigurationError(
                f"{path} is a replay for {execution.case_id!r}, which is not in {CASES.name}"
            )
        executions.append(execution)
    if not executions:
        raise ConfigurationError(f"no replays found under {REPLAYS}")
    return executions


def check_report_matches(
    case_set: AcceptanceCaseSet, executions: list[CaseExecution]
) -> dict[str, str]:
    """Refuse to replace a report whose *expectations* differ from the case list.

    The failure this guards is specific and silent: somebody edits what a case expects,
    the replays still record the old run, and the report on disk keeps printing a
    verdict that was reached against text nobody can see any more. ``cases_digest``
    catches exactly that, because it is computed from what the cases *say*.

    ``observation_digest`` is deliberately not a refusal. A mismatch there means the
    replays are newer than the report, which is the ordinary consequence of running the
    acceptance again -- and that is the moment the report most needs rewriting. Making
    it fatal would mean every legitimate sweep required ``--force``, which would train
    the operator to pass the one flag that also disables the check that matters.

    Returns the drift note, if any, for the caller to print.
    """
    if not REPORT_JSON.exists():
        return {}
    try:
        recorded = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{REPORT_JSON} is not valid JSON: {exc}") from exc

    expected = case_set_digest(case_set)
    if recorded.get("cases_digest") != expected:
        raise ConfigurationError(
            f"{REPORT_JSON} was generated from a different case list "
            f"(report {recorded.get('cases_digest')}, cases {expected}); "
            "the recorded verdicts were reached against expectations that have since "
            "changed. Re-run the acceptance, or pass --force to replace it anyway"
        )
    observed = execution_digest(executions)
    if recorded.get("observation_digest") != observed:
        return {
            "rewriting": "the report on disk describes older observations",
            "previous_observation_digest": str(recorded.get("observation_digest")),
            "current_observation_digest": observed,
        }
    return {}


def render_pipeline(executions: list[CaseExecution]) -> list[str]:
    """The steps before and after the cases, with the exit code each one actually had.

    Absent evidence is reported as absent, never as success. The pipeline file is
    produced by ``collect_phase7_pipeline_evidence.py``; if the stages were not
    collected, the report says so and the reader knows the preconditions were not
    observed in this run rather than inferring they held.
    """
    lines: list[str] = ["## 二、完整流程（按时间顺序）", ""]
    environment = executions[0].environment
    lines += [
        "### 1. 环境与版本冻结",
        "",
        f"- 受测端点：`{environment.base_url}`",
        f"- 租户：`{environment.tenant_id}`",
        f"- 部署版本：`{environment.deployed_revision}`",
        f"- 观测时刻：`{environment.recorded_at.isoformat()}`",
        f"- 核验器已配置：`{environment.entitlement_verifier_configured}`",
        "",
    ]
    for note in environment.notes:
        lines.append(f"  > {note}")
    lines.append("")

    if not PIPELINE.exists():
        lines += [
            "### 2. 前置条件与回归",
            "",
            "**本次未采集。** 运行 `scripts/collect_phase7_pipeline_evidence.py` 以记录身份播种、"
            "夹具、索引生命周期与全仓回归的实际退出码。**未采集不等于通过**，上表各项在本报告中"
            "没有观测支持。",
            "",
        ]
        return lines

    recorded = json.loads(PIPELINE.read_text(encoding="utf-8"))
    lines += [
        "### 2. 前置条件与回归",
        "",
        f"采集时刻：`{recorded.get('generated_at')}`；采集时源码版本：`{recorded.get('repo_revision')}`。",
        f"记录前已按值脱敏的密钥变量名（共 {len(recorded.get('secret_keys_scrubbed') or [])} 个）："
        + "、".join(f"`{item}`" for item in (recorded.get("secret_keys_scrubbed") or []))
        + "；本报告不含任何密钥值。",
        "",
        "| 阶段 | 目的 | 退出码 | 耗时 s |",
        "|---|---|---|---|",
    ]
    for stage in recorded.get("stages", []):
        # The command is deliberately not a table cell. These commands are shell pipelines
        # full of ``|`` and embedded newlines, and a pipe inside a cell ends the cell: the
        # row would silently lose its exit code, which is the one column the table exists
        # to show. Each stage states its own command below, in a fenced block where the
        # text is literal.
        lines.append(
            f"| `{stage['id']}` | {stage['purpose']} | {stage['exit_code']} "
            f"| {stage['elapsed_seconds']} |"
        )
    lines.append("")
    for stage in recorded.get("stages", []):
        lines += [
            f"**`{stage['id']}`**",
            "",
            f"目的：{stage['purpose']}",
            "",
            "命令：",
            "",
            "```bash",
            stage["command"],
            "```",
            "",
            "退出码：`"
            + str(stage["exit_code"])
            + "`；耗时："
            + str(stage["elapsed_seconds"])
            + " s",
            "",
        ]
        if (stage.get("stdout") or "").strip():
            lines += ["stdout：", "", "```", (stage.get("stdout") or "").strip(), "```", ""]
        else:
            lines += ["stdout：（空）", ""]
        if (stage.get("stderr") or "").strip():
            lines += ["stderr：", "", "```", (stage.get("stderr") or "").strip(), "```", ""]
        else:
            lines += ["stderr：（空）", ""]
    return lines


def render_case_detail(
    outcome,
    execution: CaseExecution | None,
) -> list[str]:
    """One case's record, in the fixed shape the baseline document requires.

    Every section is emitted even when empty, and says so. A missing ``结果怎么样``
    section reads as a case that had no results, which is a different claim from a case
    whose results were not recorded -- and only one of those is true.
    """
    lines: list[str] = [f"### {outcome.case_id} — {outcome.title}", ""]
    lines += [
        f"- **目标**：{outcome.goal}",
        f"- **来源**：{outcome.source}",
        f"- **覆盖模块行**：{', '.join(outcome.modules) or '（未声明）'}",
        f"- **判定**：{outcome.verdict.value}"
        + (
            "（阻断验收关闭）"
            if outcome.verdict is Verdict.BLOCKED and outcome.blocks_acceptance_when_blocked
            else ""
        ),
        f"- **终态**：{outcome.terminal_status or '（无运行：本案例不提交 run）'}",
        f"- **耗时**：{outcome.total_seconds if outcome.total_seconds is not None else '（无）'} s",
        f"- **run_id**：{outcome.run_id or '（无）'}",
        "",
    ]

    lines += ["**逐条断言判定**", "", "| 断言 | 期望 | 实际 | 判定 |", "|---|---|---|---|"]
    for assertion in outcome.assertions:
        expectation = json.dumps(assertion.expectation, ensure_ascii=False, sort_keys=True)
        detail = assertion.detail.replace("|", "\\|")
        lines.append(
            f"| `{assertion.assertion_id}` | `{expectation}` | {detail} "
            f"| {assertion.verdict.value} |"
        )
    lines.append("")

    if outcome.failure_reasons:
        lines += ["**失败原因**", ""] + [f"- {item}" for item in outcome.failure_reasons] + [""]
    if outcome.blocked_reasons:
        lines += ["**阻断原因**", ""] + [f"- {item}" for item in outcome.blocked_reasons] + [""]
    if outcome.driver_errors:
        lines += ["**驱动错误**", ""] + [f"- {item}" for item in outcome.driver_errors] + [""]

    if execution is None:
        lines += ["**原始证据**：本案例没有回放记录。", ""]
        return lines

    lines += ["**逐步轨迹**", "", "| 步 | 动作 | 结果 | 耗时 s | 详情 |", "|---|---|---|---|---|"]
    for step in execution.steps:
        detail = (step.detail or "").replace("\n", " ").replace("|", "\\|")[:400]
        lines.append(
            f"| `{step.id}` | {step.action} | {step.outcome} | "
            f"{step.elapsed_seconds if step.elapsed_seconds is not None else ''} | {detail} |"
        )
    lines.append("")

    lines += ["**原始证据**", ""]
    payload = execution.model_dump(mode="json")
    for field in EVIDENCE_FIELDS:
        value = payload.get(field)
        if value in (None, [], {}):
            lines.append(f"- `{field}`: （空）")
            continue
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if len(rendered) > 4000:
            rendered = rendered[:4000] + " …（截断，全文见报告 JSON）"
        lines.append(f"- `{field}`: `{rendered}`")
    lines.append("")
    return lines


def render_report(outcome: AcceptanceOutcome, executions: list[CaseExecution]) -> str:
    """The whole report, assembled from the outcome rather than from prose."""
    lines: list[str] = [
        "# P7.6 核心业务闭环验收报告",
        "",
        "> 本文件由 `scripts/gate_phase7_acceptance.py` 依据案例清单与回放自动生成，"
        "**禁止手写**。任何手改都会在下一次 gate 运行时被覆盖，或在摘要不一致时以退出码 3 拒绝。",
        "",
        "## 一、本次判定的绑定",
        "",
        f"- 生成时间：`{outcome.generated_at.isoformat()}`",
        f"- 案例清单摘要 `cases_digest`：`{outcome.cases_digest}`",
        f"- 观测摘要 `observation_digest`：`{outcome.observation_digest}`",
        f"- 案例数：{len(outcome.cases)}；模块 roster：{len(outcome.coverage)}",
        f"- **总判定：{outcome.verdict.value}**",
        "",
        "摘要的用途是让「案例改了而报告没重跑」这件事无法悄悄发生：报告与案例清单以 "
        "`cases_digest` 绑定，案例改一个字符而回放未更新，下一次 gate 即退出 3。",
        "",
    ]

    lines += render_pipeline(executions)

    if outcome.blockers:
        lines += ["## 三、阻断项", ""] + [f"- {item}" for item in outcome.blockers] + [""]
    else:
        lines += ["## 三、阻断项", "", "（无）", ""]

    counts = {verdict.value: 0 for verdict in Verdict}
    for case in outcome.cases:
        counts[case.verdict.value] += 1
    lines += [
        "## 四、案例总览",
        "",
        "| 案例 | 标题 | 终态 | 耗时 s | 判定 |",
        "|---|---|---|---|---|",
    ]
    for case in outcome.cases:
        lines.append(
            f"| `{case.case_id}` | {case.title} | {case.terminal_status or '（无）'} "
            f"| {case.total_seconds if case.total_seconds is not None else ''} "
            f"| {case.verdict.value} |"
        )
    lines += [
        "",
        "计数："
        + "，".join(f"{name} {value}" for name, value in sorted(counts.items()))
        + "。BLOCKED 不与 FAIL 混同，也不计入通过：它表示该案例未能被观测，"
        "因此无法支持任何结论；标了「阻断验收关闭」的 BLOCKED 会直接阻断本次验收。",
        "",
        "## 五、覆盖表",
        "",
        render_coverage_markdown(outcome),
        "",
        "「涉及」与「验证了具体行为」是两件事：链路经过某模块，不等于对它的可观察行为做过断言。"
        "本表由断言上的 `verifies_module` 生成，而非由案例经过的模块推断。",
        "",
        "## 六、逐案例记录",
        "",
    ]
    by_id = {item.case_id: item for item in executions}
    for case in outcome.cases:
        lines += render_case_detail(case, by_id.get(case.case_id))
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="judge the replays on disk")
    parser.add_argument(
        "--live",
        action="store_true",
        help="accepted for compatibility only; the gate never runs the acceptance itself",
    )
    parser.add_argument("--replay-only", action="store_true", help="grade recorded replays")
    parser.add_argument("--format", default="markdown", choices=("markdown", "json"))
    parser.add_argument("--report", type=Path, default=None, help="where to write the report")
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace a report whose digests disagree with the inputs, instead of refusing",
    )
    args = parser.parse_args()

    if args.live:
        print(
            json.dumps(
                {
                    "error": "this gate does not run the acceptance",
                    "why": "the observations must be produced by whatever version of the "
                    "platform is deployed, and a gate that graded older replays under a "
                    "--live invocation would report a run that never happened",
                    "instead": "uv run python scripts/verify_phase7_acceptance_live.py",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return EXIT_CONFIGURATION

    try:
        case_set = load_case_set()
        executions = load_replays(case_set)
        drift = {} if args.force else check_report_matches(case_set, executions)
    except ConfigurationError as exc:
        print(json.dumps({"configuration_error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return EXIT_CONFIGURATION

    outcome = grade(case_set, executions, generated_at=datetime.now(tz=UTC))

    report_json = args.report.with_suffix(".json") if args.report else REPORT_JSON
    report_md = (
        args.report
        if args.report and args.report.suffix == ".md"
        else (REPORT_MD if args.report is None else args.report.with_suffix(".md"))
    )
    REPORTS.mkdir(parents=True, exist_ok=True)
    report_json.write_text(dump_outcome(outcome), encoding="utf-8")
    report_md.write_text(render_report(outcome, executions), encoding="utf-8")

    summary = {
        "verdict": outcome.verdict.value,
        "cases": {case.case_id: case.verdict.value for case in outcome.cases},
        "counts": {
            name: sum(1 for case in outcome.cases if case.verdict.value == name)
            for name in (item.value for item in Verdict)
        },
        "blockers": outcome.blockers,
        "cases_digest": outcome.cases_digest,
        "observation_digest": outcome.observation_digest,
        "report_json": str(report_json),
        "report_md": str(report_md),
        **drift,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if outcome.verdict is Verdict.PASS:
        return EXIT_PASS
    if outcome.verdict is Verdict.FAIL:
        return EXIT_FAIL
    # BLOCKED. Named on stderr so the two ways to reach it stay distinguishable in a
    # log: a case that ran and could not be judged is a finding about the platform, and
    # a case with no replay is the acceptance having not been run. Both are insufficient
    # observation, which is one exit code -- but a reader should not have to guess which.
    missing = [
        case.id for case in case_set.cases if case.id not in {item.case_id for item in executions}
    ]
    if missing:
        print(json.dumps({"not_run": missing}, ensure_ascii=False), file=sys.stderr)
    return EXIT_INSUFFICIENT


if __name__ == "__main__":
    raise SystemExit(main())
