"""Score the path a run took, from the path the run itself recorded.

**The gap this fills.** Every Phase 7 measurement so far asks whether a case ended well: the
right document was cited, the reviewer passed, no write leaked. None of them asks how the run
got there. A run that retrieved once, analysed once and finalised, and a run that dispatched,
revised its plan twice, re-dispatched the same task and only then finalised, can both end at
``succeeded`` with the same citations -- and only the first is what the task plan describes. The
path was recorded all along: ``agent_runs.result["trajectory"]`` accumulates a token per node
entered, ``run_events`` holds one row per agent completion and per supervisor decision, and
``model_invocations`` holds every model call. This script reads those and counts.

**Why it costs nothing.** It reads runs that already happened, joined by ``run_id`` to the replays
that attribute them to a revision (:mod:`servicemind.evaluation.recorded`). No model is called and
no run is made, so the report describes the same frozen revision the corpus does rather than a new
one.

**Two surfaces that look interchangeable and are not.** Both were measured before either was used,
because both would have produced a confident wrong number:

* ``result["agent_invocations"]`` is the *subagent envelope* list, and the knowledge node builds no
  envelope. Scored from it, retrieval never participates -- in 121 runs out of 121. Participation
  is therefore read from ``run_events``' ``agent.completed`` events, and the size of the gap is
  reported rather than smoothed over.
* An agent envelope's ``attempts`` is that agent's own aggregate, and the DataAgent sums its tool
  calls into it (``agents/data.py``), so a three-tool data task reports ``attempts=3`` while the
  other three agents report ``1`` in every run. It is not a retry count and not comparable between
  agents. Retries are read from ``model_invocations.retries``, which is provider-level and means the
  same thing for every role.

**What rests on an authored contract.** Plan conformance, repetition and shape are internal to the
run and assume nothing: the plan is the run's own declaration of the work, and "every planned task
ran and nothing else did" is checkable against it. ``REQUIRED_AGENTS`` does assume something -- it
says which agents a case of each kind must pass through -- and it is declared here, in one place, so
a reader can disagree with it. A run that fails it is reported as a finding rather than quietly
relaxed to match what the platform did.

Usage::

    uv run python scripts/report_phase7_trajectory.py --tenant-id 2222... \\
        [--replays evaluation/quality/replays] [--check]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import sqlalchemy as sa

from servicemind.evaluation.recorded import RecordedRun, inputs_block, read_corpus
from servicemind.evaluation.source_revision import source_revision

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPLAYS = REPO_ROOT / "evaluation" / "quality" / "replays"
REPORT_JSON = REPO_ROOT / "evaluation" / "reports" / "phase7_trajectory_latest.json"
REPORT_MD = REPO_ROOT / "evaluation" / "reports" / "phase7_trajectory_latest.md"

#: An agent step in the trajectory: ``<agent>:<task id>``, plus the one non-task agent token the
#: retrieval path emits when it declines to look (``knowledge:declined``). Anchored on the four
#: agent names the task plan actually schedules, so a future token that merely contains a colon is
#: not silently counted as work.
AGENT_STEP = re.compile(r"^(data|knowledge|analysis|reviewer):(T\d+|declined)$")

#: The supervisor's arbitration, named ``decision:<action>`` by the node that emits it.
DECISION_STEP = re.compile(r"^decision:(.+)$")

#: Which agents a case of each kind must pass through, **by contract rather than by measurement**.
#: An answerable question that was never analysed was not answered, and a question whose evidence
#: was thin still has to reach the reviewer -- the reviewer is the node that decides insufficiency,
#: so a run that stops before it has not abstained, it has stopped.
REQUIRED_AGENTS: dict[str, tuple[str, ...]] = {
    "answerable": ("knowledge", "analysis", "reviewer"),
    "insufficient-evidence": ("knowledge", "reviewer"),
}

#: Nodes that must not be entered on a run that asked for no write. The executor is the one
#: component that can change the outside world, and a read-only case that reached it would have
#: changed it for a reason nobody asked for.
FORBIDDEN_WHEN_READ_ONLY = ("execute", "action", "glpi.followup.created")


def parse_run(run: RecordedRun) -> dict[str, Any]:
    """One run's trajectory reading, and the raw material every number in it came from."""
    trajectory = [str(step) for step in (run.field("trajectory") or [])]
    agent_steps = [step for step in trajectory if AGENT_STEP.match(step)]
    decisions = [match.group(1) for step in trajectory if (match := DECISION_STEP.match(step))]
    plan = run.field("task_plan") or {}
    planned = [str(task.get("task_id")) for task in (plan.get("tasks") or [])]
    completions = run.payloads("agent.completed")
    invoked = [str(record.get("task_id")) for record in (run.field("agent_invocations") or [])]
    executed = [str(record.get("task_id")) for record in completions]
    actors = [str(record.get("agent_name")) for record in completions]
    metrics = [record.get("metrics") or {} for record in completions]
    agents = {step.split(":", 1)[0] for step in agent_steps}
    timings = run.field("branch_timings") or []

    executed_counts = Counter(executed)
    repeated = sorted(
        task for task, count in executed_counts.items() if count > 1 and task != "None"
    )
    planned_set, executed_set = set(planned), set(executed)
    kind = str(run.replay.get("kind") or "")
    forbidden = (
        [
            step
            for step in trajectory
            if any(step.startswith(name) for name in FORBIDDEN_WHEN_READ_ONLY)
        ]
        if run.request_write is False
        else []
    )

    return {
        "case_id": run.case_id,
        "run_id": run.run_id,
        "kind": kind or None,
        "request_write": run.request_write,
        "terminal_status": run.status,
        "replay_status": run.replay.get("terminal_status"),
        "trajectory_length": len(trajectory),
        "agent_steps": len(agent_steps),
        "supervisor_decisions": len(decisions),
        "decision_actions": dict(sorted(Counter(decisions).items())),
        "review_outcome": next(
            (step.split(":", 1)[1] for step in trajectory if step.startswith("review:")), None
        ),
        "distinct_agents": sorted(agents),
        "planned_tasks": planned,
        "executed_tasks": executed,
        "plan_conformance": (
            round(len(planned_set & executed_set) / len(planned_set), 4) if planned_set else None
        ),
        "unplanned_executions": sorted(executed_set - planned_set - {"None"}),
        "unexecuted_tasks": sorted(planned_set - executed_set),
        "repeated_task_executions": repeated,
        "loop_detected": bool(repeated) or decisions.count("replan") > 1,
        "plan_revision": run.field("plan_revision"),
        "max_parallel_branches": _max_parallel(timings),
        "unfinished_branches": _unfinished(timings, completions),
        # ``result["agent_invocations"]`` holds subagent envelopes and the knowledge node builds
        # none, so retrieval is absent from that list in every run. The gap is reported rather than
        # smoothed over: a reader scoring participation from ``agent_invocations`` would conclude
        # knowledge never ran, 121 runs out of 121.
        "envelope_records": len(invoked),
        "completed_but_absent_from_agent_invocations": sorted(
            f"{actor}:{task}" for actor, task in zip(actors, executed) if task not in set(invoked)
        ),
        "model_calls": len(run.calls),
        "tool_calls": sum(int(entry.get("tool_calls") or 0) for entry in metrics),
        "retried_model_calls": sum(1 for call in run.calls if int(call.get("retries") or 0) > 0),
        "failed_model_calls": sum(
            1 for call in run.calls if str(call.get("status") or "") != "succeeded"
        ),
        "model_call_errors": sorted(
            {str(call["error_code"]) for call in run.calls if call.get("error_code")}
        ),
        "reported_agent_attempts": dict(
            sorted(
                Counter(
                    f"{actor}:{entry.get('attempts')}" for actor, entry in zip(actors, metrics)
                ).items()
            )
        ),
        "missing_required_agents": [
            name for name in REQUIRED_AGENTS.get(kind, ()) if name not in agents
        ],
        "forbidden_steps": forbidden,
        "evidence_rows_joined": len(run.payloads("evidence.joined")),
        "errors": run.replay.get("errors") or [],
    }


def _max_parallel(timings: list[dict[str, Any]]) -> int:
    """The widest overlap of branch intervals recorded for one run.

    A swept count over interval endpoints rather than a pairing of branches: the question is how
    many ran at once, and a branch that started before another ended is concurrent with it whether
    or not they were scheduled together. Ends are ordered before starts at the same instant, so
    touching intervals count as sequential.
    """
    edges: list[tuple[float, int]] = []
    for timing in timings:
        started, finished = timing.get("started"), timing.get("finished")
        if started is None or finished is None:
            continue
        edges.append((float(started), 1))
        edges.append((float(finished), -1))
    edges.sort(key=lambda edge: (edge[0], edge[1]))
    width = running = 0
    for _, delta in edges:
        running += delta
        width = max(width, running)
    return width


def _unfinished(timings: list[dict[str, Any]], completions: list[dict[str, Any]]) -> list[str]:
    """Branches that started and never reported a finish, or reported a degraded outcome."""
    stalled = [
        str(timing.get("agent")) for timing in timings if timing.get("finished") in (None, 0)
    ]
    degraded = [
        str(completion.get("agent_name"))
        for completion in completions
        if str(completion.get("status") or "") not in ("succeeded", "")
    ]
    return sorted(set(stalled) | set(degraded))


def summarise(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """The distribution the corpus shows, plus the anomalies that are true by construction."""

    def distribution(key: str) -> dict[str, int]:
        return dict(sorted(Counter(str(run.get(key)) for run in runs).items()))

    def rate(predicate: Any) -> float | None:
        return round(sum(1 for run in runs if predicate(run)) / len(runs), 4) if runs else None

    lengths = [run["trajectory_length"] for run in runs]
    anomalies: list[dict[str, Any]] = []
    for run in runs:
        if run["missing_required_agents"]:
            anomalies.append(
                {
                    "case_id": run["case_id"],
                    "why": f"never reached {', '.join(run['missing_required_agents'])}",
                }
            )
        if run["repeated_task_executions"]:
            anomalies.append(
                {
                    "case_id": run["case_id"],
                    "why": f"executed {', '.join(run['repeated_task_executions'])} more than once",
                }
            )
        if run["unexecuted_tasks"]:
            anomalies.append(
                {
                    "case_id": run["case_id"],
                    "why": f"planned but never ran {', '.join(run['unexecuted_tasks'])}",
                }
            )
        if run["forbidden_steps"]:
            anomalies.append(
                {
                    "case_id": run["case_id"],
                    "why": f"read-only run entered {', '.join(run['forbidden_steps'])}",
                }
            )
        if run["errors"]:
            anomalies.append({"case_id": run["case_id"], "why": "the run recorded errors"})

    return {
        "run_count": len(runs),
        "trajectory_length": {
            "min": min(lengths) if lengths else None,
            "max": max(lengths) if lengths else None,
            "mean": round(sum(lengths) / len(lengths), 2) if lengths else None,
        },
        "by_kind": dict(sorted(Counter(str(run["kind"]) for run in runs).items())),
        "plan_conformance": {
            "perfect": rate(lambda run: run["plan_conformance"] == 1.0),
            "runs_with_unplanned_step": rate(lambda run: bool(run["unplanned_executions"])),
            "runs_with_unexecuted_task": rate(lambda run: bool(run["unexecuted_tasks"])),
        },
        "repetition": {
            "runs_repeating_a_task": rate(lambda run: bool(run["repeated_task_executions"])),
            "runs_revising_the_plan": rate(lambda run: int(run["plan_revision"] or 0) > 0),
            "runs_replanning": rate(lambda run: "replan" in run["decision_actions"]),
        },
        "review_outcome_distribution": distribution("review_outcome"),
        "decision_action_totals": dict(
            sorted(sum((Counter(run["decision_actions"]) for run in runs), Counter()).items())
        ),
        "concurrency": {
            "max_parallel_branches": max((run["max_parallel_branches"] for run in runs), default=0),
            "runs_with_a_stalled_branch": rate(lambda run: bool(run["unfinished_branches"])),
        },
        "effort": {
            "model_calls_total": sum(run["model_calls"] for run in runs),
            "tool_calls_total": sum(run["tool_calls"] for run in runs),
            "runs_with_a_retried_model_call": rate(lambda run: run["retried_model_calls"] > 0),
            "failed_model_calls": sum(run["failed_model_calls"] for run in runs),
            "model_call_error_codes": dict(
                sorted(sum((Counter(run["model_call_errors"]) for run in runs), Counter()).items())
            ),
            "agent_attempts_as_reported": dict(
                sorted(
                    sum(
                        (Counter(run["reported_agent_attempts"]) for run in runs), Counter()
                    ).items()
                )
            ),
        },
        "participation_surface": {
            "runs_where_agent_invocations_is_short": rate(
                lambda run: bool(run["completed_but_absent_from_agent_invocations"])
            ),
            "records_missing_from_agent_invocations": sum(
                len(run["completed_but_absent_from_agent_invocations"]) for run in runs
            ),
            "read_from": "run_events.agent.completed",
        },
        "contract": {
            "required_agents": {kind: list(names) for kind, names in REQUIRED_AGENTS.items()},
            "forbidden_when_read_only": list(FORBIDDEN_WHEN_READ_ONLY),
            "runs_failing_the_contract": sum(1 for run in runs if run["missing_required_agents"]),
        },
        "anomalies": anomalies,
    }


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _table(rows: list[tuple[str, Any]]) -> list[str]:
    return ["| Reading | Value |", "|---|---|", *[f"| {name} | {value} |" for name, value in rows]]


def render(report: dict[str, Any]) -> str:
    """The report as markdown. A pure function of the JSON, so ``--check`` can compare them."""
    summary = report["summary"]
    inputs = report["inputs"]
    effort = summary["effort"]
    lines = [
        "# Phase 7 — Trajectory scoring (recorded runs)",
        "",
        f"Generated {report['generated_at']} from {inputs['observations']} recorded observations.",
        "",
        "## What was read, and what was not",
        "",
        f"- Replays: `{inputs['replay_directory']}` — {inputs['observations']} observations.",
        *[f"  - `{name}` × {count}" for name, count in inputs["data_revisions"].items()],
        f"- Scorer revision: `{inputs['scorer_revision']}`",
        f"- Resolved to recorded runs: **{summary['run_count']}**"
        + (
            f" — unresolved: {inputs['unresolved_run_ids']}"
            if inputs.get("unresolved_run_ids")
            else ""
        ),
        "- Model calls made: **0**. Runs created: **0**.",
        "",
        "The two revisions differ on purpose and are not interchangeable. `evaluation/` and `docs/`",
        "are exempt from the source fingerprint; `scripts/` is not, so a scorer written after a",
        "batch carries its own revision. The data revision above is the platform's; the scorer",
        "revision is this report's.",
        "",
        "## Shape",
        "",
        *_table(
            [
                (
                    "Trajectory length (min / mean / max)",
                    f"{summary['trajectory_length']['min']} / "
                    f"{summary['trajectory_length']['mean']} / {summary['trajectory_length']['max']}",
                ),
                ("Widest parallel branch fan-out", summary["concurrency"]["max_parallel_branches"]),
                (
                    "Runs with a stalled or degraded branch",
                    _pct(summary["concurrency"]["runs_with_a_stalled_branch"]),
                ),
                ("Model calls across the corpus", effort["model_calls_total"]),
                ("Tool calls across the corpus", effort["tool_calls_total"]),
                (
                    "Runs with a provider-level retry",
                    _pct(effort["runs_with_a_retried_model_call"]),
                ),
                ("Model calls that did not succeed", effort["failed_model_calls"]),
                (
                    "`agent.completed` attempts, as each agent reports it",
                    ", ".join(f"`{k}`={v}" for k, v in effort["agent_attempts_as_reported"].items())
                    or "—",
                ),
            ]
        ),
        "",
        "## Plan conformance (internal to the run — no outside contract)",
        "",
        "The plan is the run's own declaration of the work, so this reading needs no outside opinion:",
        "it asks whether every planned task ran and whether anything ran that no plan asked for.",
        "",
        *_table(
            [
                (
                    "Runs where every planned task ran and nothing else did",
                    _pct(summary["plan_conformance"]["perfect"]),
                ),
                (
                    "Runs with a step no plan called for",
                    _pct(summary["plan_conformance"]["runs_with_unplanned_step"]),
                ),
                (
                    "Runs that planned a task and never ran it",
                    _pct(summary["plan_conformance"]["runs_with_unexecuted_task"]),
                ),
                (
                    "Runs that executed a task more than once",
                    _pct(summary["repetition"]["runs_repeating_a_task"]),
                ),
                (
                    "Runs that revised their plan (retrieve-more or replan)",
                    _pct(summary["repetition"]["runs_revising_the_plan"]),
                ),
                (
                    "Runs that replanned",
                    _pct(summary["repetition"]["runs_replanning"]),
                ),
            ]
        ),
        "",
        "## The participation surface, and its one trap",
        "",
        "Participation is read from `run_events`' `agent.completed` events. `agent_runs.result`'s",
        "`agent_invocations` list holds *subagent envelopes*, and the knowledge node builds none,",
        "so scored from that list retrieval would appear never to run:",
        "",
        *_table(
            [
                (
                    "Runs where `agent_invocations` is short of the timeline",
                    _pct(summary["participation_surface"]["runs_where_agent_invocations_is_short"]),
                ),
                (
                    "Records in the timeline but absent from `agent_invocations`",
                    summary["participation_surface"]["records_missing_from_agent_invocations"],
                ),
            ]
        ),
        "",
        "## Supervisor arbitration",
        "",
        "| Action | Count |",
        "|---|---|",
        *[f"| `{name}` | {count} |" for name, count in summary["decision_action_totals"].items()],
        "",
        "## Reviewer outcome, as the trajectory recorded it",
        "",
        "| Outcome | Runs |",
        "|---|---|",
        *[
            f"| `{name}` | {count} |"
            for name, count in summary["review_outcome_distribution"].items()
        ],
        "",
        "## Authored contract — the one reading that assumes something",
        "",
        "Required agents per case kind, declared in `scripts/report_phase7_trajectory.py`:",
        "",
        "| Kind | Must pass through |",
        "|---|---|",
        *[
            f"| `{kind}` | {', '.join(names)} |"
            for kind, names in summary["contract"]["required_agents"].items()
        ],
        "",
        f"Runs failing that contract: **{summary['contract']['runs_failing_the_contract']}** "
        f"of {summary['run_count']}.",
        "",
        "Nodes forbidden on a read-only run: "
        + ", ".join(f"`{name}`" for name in summary["contract"]["forbidden_when_read_only"])
        + ".",
        "",
        "## Anomalies",
        "",
    ]
    if summary["anomalies"]:
        lines += ["| Case | Why |", "|---|---|"]
        lines += [f"| {row['case_id']} | {row['why']} |" for row in summary["anomalies"]]
    else:
        lines.append(
            "None: every recorded run matched its plan, its kind's contract, and its own terminal state."
        )
    lines += ["", "## Limitations", ""]
    lines += [f"- {item}" for item in report["limitations"]]
    lines.append("")
    return "\n".join(lines)


LIMITATIONS = [
    "The trajectory is the token list the run recorded, not an independent trace: a node that ran "
    "without emitting a token is invisible here.",
    "'Distinct agents' is read from the trajectory's agent-step tokens; one agent working two tasks "
    "counts once.",
    "Plan conformance compares execution to the run's *own* plan. A run whose plan was itself wrong "
    "conforms perfectly, and this reading will not notice.",
    "REQUIRED_AGENTS is an authored contract, not a measurement of the platform.",
    "An agent envelope's `attempts` is not a retry count and is not comparable between agents: the "
    "DataAgent sums its tool calls into it (`agents/data.py`), so a three-tool data task reports "
    "attempts=3 while the other three agents report 1 in every run. Retries are read from "
    "`model_invocations.retries`, which is provider-level and means the same thing for every role.",
    "`agent_invocations` is not a complete participation record: the knowledge node emits "
    "`agent.completed` and a `task_completions` entry but no subagent envelope, so it is absent from "
    "that list in every run. Participation here is read from `run_events` instead, and the size of "
    "the gap is reported above.",
]


def build(engine: sa.Engine, replays: Path, tenant_id: UUID | str) -> dict[str, Any]:
    corpus = read_corpus(engine, replays, tenant_id=tenant_id)
    runs = [parse_run(run) for run in corpus.runs]
    return {
        "report": "phase7_trajectory",
        "generated_at": datetime.now(UTC).isoformat(),
        "inputs": inputs_block(
            replays,
            corpus.replays,
            tool_revision=source_revision(REPO_ROOT),
            extra={"unresolved_run_ids": list(corpus.unresolved)},
        ),
        "runs": runs,
        "summary": summarise(runs),
        "limitations": LIMITATIONS,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--replays", type=Path, default=DEFAULT_REPLAYS)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--json", type=Path, default=REPORT_JSON)
    parser.add_argument("--md", type=Path, default=REPORT_MD)
    parser.add_argument(
        "--check",
        action="store_true",
        help="re-render the markdown from the stored JSON and fail if it differs",
    )
    args = parser.parse_args(argv)

    if args.check:
        stored = json.loads(args.json.read_text(encoding="utf-8"))
        if args.md.read_text(encoding="utf-8") != render(stored):
            print(f"{args.md} does not match {args.json}", file=sys.stderr)
            return 1
        print(f"{args.md} matches {args.json}")
        return 0

    url = args.database_url or os.environ["SERVICEMIND_DATABASE_URL"]
    report = build(sa.create_engine(url), args.replays, args.tenant_id)
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.md.write_text(render(report), encoding="utf-8")
    summary = report["summary"]
    print(f"runs={summary['run_count']} anomalies={len(summary['anomalies'])}")
    print(f"contract failures={summary['contract']['runs_failing_the_contract']}")
    print(f"wrote {args.json} and {args.md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
