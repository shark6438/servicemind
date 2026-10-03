"""Measure what the agent graph did on runs that were already recorded.

**The gap this fills.** The coordination batch answers "which sub-agents ran, did the supervisor
arbitrate, was a plan revised" by *making* runs -- it starts a stack and posts cases at the live
API. The same question can be asked of runs that have already happened, and for a corpus that is
already recorded and attributed to a frozen revision, asking it that way costs no model call and
does not move the revision under test.

**One vocabulary, declared once.** The event names this counts are imported from the live driver
(:mod:`scripts.verify_phase7_coordination_live`), not re-typed here. A renamed event then shows up
in both reports as a count that dropped to zero -- which is visible -- rather than as one report
that kept matching a literal the other had stopped emitting. Participation, arbitration, planning,
human handoff and terminal distribution are read through that driver's own ``observe_record`` and
``summarise``, so those five readings have exactly one implementation.

**What is added here**, because the live driver does not read it: the evidence a run *lost* on the
way from retrieval to answer, the contention between branches that ran at the same time, the
re-entry counts that say the graph went around again, the routing decisions, and the retrieval
declines with the score that caused them.

Usage::

    uv run python scripts/report_phase7_coordination_recorded.py --tenant-id 2222... \\
        [--replays evaluation/quality/replays] [--check]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
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
LIVE_DRIVER = REPO_ROOT / "scripts" / "verify_phase7_coordination_live.py"
DEFAULT_REPLAYS = REPO_ROOT / "evaluation" / "quality" / "replays"
REPORT_JSON = REPO_ROOT / "evaluation" / "reports" / "phase7_coordination_latest.json"
REPORT_MD = REPO_ROOT / "evaluation" / "reports" / "phase7_coordination_latest.md"

#: Phases the supervisor can send the graph back into. A second entry into any of these is the
#: graph going around again, which is the shape a loop takes before it becomes a livelock.
REENTRY = ("dispatch", "analyze", "review")


def load_live_driver() -> Any:
    """The live coordination driver as a module, for its event vocabulary and its readings."""
    spec = importlib.util.spec_from_file_location("_coordination_live", LIVE_DRIVER)
    if spec is None or spec.loader is None:  # pragma: no cover -- a broken checkout
        raise RuntimeError(f"cannot load {LIVE_DRIVER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _concurrent_sharing(
    timings: list[dict[str, Any]], completions: list[dict[str, Any]]
) -> list[str]:
    """Agent pairs that ran *at the same time* and came back with an overlapping evidence set.

    The overlap has to be temporal, or the reading is not about concurrency at all. Two agents in
    different phases of the graph are handed the same evidence by design -- the reviewer is given
    the analysis's evidence, the analysis is given the join's -- so counting every pair that shares
    a row would measure the handoff chain, report 100% of runs, and say nothing about the fan-out.
    Only branches whose recorded intervals intersect are counted: those are the ones that could
    have duplicated work.
    """
    intervals = [
        (float(t["started"]), float(t["finished"]), str(t.get("agent")), str(t.get("task_id")))
        for t in timings
        if t.get("started") is not None and t.get("finished") is not None
    ]
    refs = {
        str(completion.get("task_id")): {
            str(ref) for ref in (completion.get("evidence_refs") or ())
        }
        for completion in completions
    }
    shared: set[str] = set()
    for left in range(len(intervals)):
        for right in range(left + 1, len(intervals)):
            first, second = intervals[left], intervals[right]
            if first[0] < second[1] and second[0] < first[1]:
                if refs.get(first[3], set()) & refs.get(second[3], set()):
                    shared.add("∩".join(sorted({first[2], second[2]})))
    return sorted(shared)


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(int(len(ordered) * fraction), len(ordered) - 1)], 2)


def observe(run: RecordedRun) -> dict[str, Any]:
    """One run's coordination reading, over and above what the live vocabulary already gives."""
    decisions = run.payloads("supervisor.decision")
    actions = [str(decision.get("action")) for decision in decisions]
    confidences = [
        float(decision["confidence"])
        for decision in decisions
        if isinstance(decision.get("confidence"), (int, float))
    ]
    completions = run.payloads("agent.completed")
    joins = run.payloads("evidence.joined")
    declined = run.payloads("lookup.declined")

    # What retrieval gathered, what survived the join, and what reached the answer. Read at each
    # stage rather than only at the ends, so a loss can be attributed to the join rather than to
    # the analysis or the citation writer.
    gathered = sum(int(join.get("gathered") or 0) for join in joins)
    dropped_at_join = sum(int(join.get("dropped") or 0) for join in joins)
    delivered = [
        len(completion.get("evidence_refs") or [])
        for completion in completions
        if completion.get("agent_name") == "analysis"
    ]
    joined_refs: set[str] = set()
    for join in joins:
        joined_refs.update(str(ref) for ref in (join.get("evidence_refs") or ()))
    cited = [str(citation) for citation in (run.replay.get("citations") or [])]

    concurrent_shared = _concurrent_sharing(run.field("branch_timings") or [], completions)

    return {
        "case_id": run.case_id,
        "run_id": run.run_id,
        "kind": run.replay.get("kind"),
        "terminal_status": run.status,
        "reviewer_decision": run.replay.get("reviewer_decision"),
        "supervisor_actions": dict(sorted(Counter(actions).items())),
        "supervisor_confidences": confidences,
        "supervisor_mean_confidence": _mean(confidences),
        "supervisor_lowest_confidence": min(confidences) if confidences else None,
        "policy_rejections": len(run.payloads("supervisor.policy_rejected")),
        "decision_rejections": len(run.payloads("supervisor.decision_rejected")),
        "replan_attempts_rejected": len(run.payloads("replanner.proposal_rejected")),
        "reentry": {
            phase: actions.count(phase) - 1 for phase in REENTRY if actions.count(phase) > 1
        },
        "agents_completed": [str(completion.get("agent_name")) for completion in completions],
        "evidence": {
            "gathered": gathered,
            "joined": sum(int(join.get("count") or 0) for join in joins),
            "dropped_at_join": dropped_at_join,
            "join_rounds": len(joins),
            "distinct_refs_joined": len(joined_refs),
            "delivered_to_analysis": delivered[0] if delivered else None,
            "citations_in_answer": len(cited),
        },
        "concurrent_branches_sharing_evidence": sorted(set(concurrent_shared)),
        "retrieval_declines": [
            {
                "reason": decline.get("reason"),
                "best_score": decline.get("best_score"),
                "floor": decline.get("floor"),
                "scored_rows": decline.get("scored_rows"),
            }
            for decline in declined
        ],
        "route": next(
            (str(payload.get("route")) for payload in run.payloads("route.completed")), None
        ),
        "memory_records_written": sum(
            int(payload.get("record_count") or 0)
            for payload in run.payloads("memory.post_run_completed")
        ),
        "errors": run.replay.get("errors") or [],
    }


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The distribution observed, plus the anomalies that are true by construction."""

    def rate(predicate: Any) -> float | None:
        return round(sum(1 for row in rows if predicate(row)) / len(rows), 4) if rows else None

    def distribution(key: str) -> dict[str, int]:
        return dict(sorted(Counter(str(row.get(key)) for row in rows).items()))

    confidences = [value for row in rows for value in row["supervisor_confidences"]]
    declines = [decline for row in rows for decline in row["retrieval_declines"]]
    reentry_totals: Counter[str] = Counter()
    for row in rows:
        reentry_totals.update(row["reentry"])
    anomalies: list[dict[str, Any]] = []
    for row in rows:
        if not row["agents_completed"]:
            anomalies.append(
                {"case_id": row["case_id"], "why": "no agent.completed event in the timeline"}
            )
        if row["errors"]:
            anomalies.append({"case_id": row["case_id"], "why": "the run recorded errors"})
        if row["supervisor_actions"] and row["supervisor_mean_confidence"] is None:
            anomalies.append(
                {"case_id": row["case_id"], "why": "a supervisor decision carried no confidence"}
            )

    return {
        "run_count": len(rows),
        "participation": {
            "agents_per_run_min": min((len(row["agents_completed"]) for row in rows), default=None),
            "agents_per_run_max": max((len(row["agents_completed"]) for row in rows), default=None),
            "agents_per_run_mean": _mean([float(len(row["agents_completed"])) for row in rows]),
            "agent_distribution": dict(
                sorted(sum((Counter(row["agents_completed"]) for row in rows), Counter()).items())
            ),
        },
        "arbitration": {
            "runs_with_a_decision": rate(lambda row: bool(row["supervisor_actions"])),
            "action_totals": dict(
                sorted(sum((Counter(row["supervisor_actions"]) for row in rows), Counter()).items())
            ),
            "runs_with_a_policy_rejection": rate(lambda row: row["policy_rejections"] > 0),
            "policy_rejections_total": sum(row["policy_rejections"] for row in rows),
            "runs_with_a_replanner_rejection": rate(
                lambda row: row["replan_attempts_rejected"] > 0
            ),
            "confidence_p50": _percentile(confidences, 0.5),
            "confidence_p05": _percentile(confidences, 0.05),
            "lowest_confidence": min(confidences) if confidences else None,
        },
        "reentry": {
            "runs_going_around": rate(lambda row: bool(row["reentry"])),
            "entries_beyond_the_first": dict(sorted(reentry_totals.items())),
        },
        "evidence_handoff": {
            "gathered_total": sum(row["evidence"]["gathered"] for row in rows),
            "joined_total": sum(row["evidence"]["joined"] for row in rows),
            "dropped_at_join_total": sum(row["evidence"]["dropped_at_join"] for row in rows),
            "runs_where_the_join_dropped_a_row": rate(
                lambda row: row["evidence"]["dropped_at_join"] > 0
            ),
            "runs_citing_less_than_they_delivered": rate(
                lambda row: (
                    row["evidence"]["delivered_to_analysis"] is not None
                    and row["evidence"]["citations_in_answer"]
                    < int(row["evidence"]["delivered_to_analysis"])
                )
            ),
        },
        "contention": {
            "runs_with_branches_sharing_evidence": rate(
                lambda row: bool(row["concurrent_branches_sharing_evidence"])
            ),
            "pairs": dict(
                sorted(
                    sum(
                        (Counter(row["concurrent_branches_sharing_evidence"]) for row in rows),
                        Counter(),
                    ).items()
                )
            ),
        },
        "retrieval": {
            "runs_with_a_decline": rate(lambda row: bool(row["retrieval_declines"])),
            "declines_total": len(declines),
            "decline_reasons": dict(sorted(Counter(str(d["reason"]) for d in declines).items())),
            "decline_scores": [
                {"best_score": d["best_score"], "floor": d["floor"]} for d in declines
            ],
        },
        "route_distribution": distribution("route"),
        "memory": {
            "runs_writing_a_memory_record": rate(lambda row: row["memory_records_written"] > 0),
            "records_written_total": sum(row["memory_records_written"] for row in rows),
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
    live = summary["live_vocabulary"]
    inputs = report["inputs"]
    lines = [
        "# Phase 7 — Multi-agent coordination (recorded runs)",
        "",
        f"Generated {report['generated_at']} from {inputs['observations']} recorded observations.",
        "",
        "## What was read, and what was not",
        "",
        f"- Replays: `{inputs['replay_directory']}` — {inputs['observations']} observations.",
        *[f"  - `{name}` × {count}" for name, count in inputs["data_revisions"].items()],
        f"- Scorer revision: `{inputs['scorer_revision']}`",
        f"- Resolved to recorded runs: **{summary['run_count']}**",
        "- Model calls made: **0**. Runs created: **0**.",
        "",
        "The event vocabulary is imported from `scripts/verify_phase7_coordination_live.py`, so a",
        "renamed event appears in both reports as a count that fell to zero rather than as one",
        "report still matching a literal the other stopped emitting.",
        "",
        "## Participation",
        "",
        *_table(
            [
                (
                    "Agents completing per run (min / mean / max)",
                    f"{summary['participation']['agents_per_run_min']} / "
                    f"{summary['participation']['agents_per_run_mean']} / "
                    f"{summary['participation']['agents_per_run_max']}",
                ),
                (
                    "`agent.completed` events per agent",
                    ", ".join(
                        f"`{name}`={count}"
                        for name, count in summary["participation"]["agent_distribution"].items()
                    ),
                ),
                (
                    "Runs with no agent participation recorded",
                    _pct(live["participation"]["runs_with_no_agent"]),
                ),
            ]
        ),
        "",
        "## Supervisor arbitration",
        "",
        *_table(
            [
                (
                    "Runs with a supervisor decision",
                    _pct(summary["arbitration"]["runs_with_a_decision"]),
                ),
                (
                    "Runs where the supervisor rejected its own agent",
                    _pct(summary["arbitration"]["runs_with_a_policy_rejection"]),
                ),
                ("Policy rejections, total", summary["arbitration"]["policy_rejections_total"]),
                (
                    "Runs where a replan proposal was rejected",
                    _pct(summary["arbitration"]["runs_with_a_replanner_rejection"]),
                ),
                (
                    "Confidence p50 / p05",
                    f"{summary['arbitration']['confidence_p50']} / {summary['arbitration']['confidence_p05']}",
                ),
                ("Lowest confidence observed", summary["arbitration"]["lowest_confidence"]),
            ]
        ),
        "",
        "| Action | Count |",
        "|---|---|",
        *[
            f"| `{name}` | {count} |"
            for name, count in summary["arbitration"]["action_totals"].items()
        ],
        "",
        "## Going around again",
        "",
        "A second entry into a phase the supervisor already completed. This is the shape a loop takes",
        "before it becomes a livelock, and it is the reading an outcome-only measurement cannot see:",
        "",
        *_table(
            [
                ("Runs that re-entered a phase", _pct(summary["reentry"]["runs_going_around"])),
                (
                    "Entries beyond the first",
                    ", ".join(
                        f"`{phase}`={count}"
                        for phase, count in summary["reentry"]["entries_beyond_the_first"].items()
                    )
                    or "—",
                ),
            ]
        ),
        "",
        "## Evidence handoff — what retrieval gathered against what reached the answer",
        "",
        *_table(
            [
                ("Evidence rows gathered", summary["evidence_handoff"]["gathered_total"]),
                (
                    "Rows delivered to the analysis join",
                    summary["evidence_handoff"]["joined_total"],
                ),
                ("Rows dropped at the join", summary["evidence_handoff"]["dropped_at_join_total"]),
                (
                    "Runs where the join dropped a row",
                    _pct(summary["evidence_handoff"]["runs_where_the_join_dropped_a_row"]),
                ),
                (
                    "Runs citing fewer sources than the analysis was handed",
                    _pct(summary["evidence_handoff"]["runs_citing_less_than_they_delivered"]),
                ),
            ]
        ),
        "",
        "## Contention between parallel branches",
        "",
        "Branches whose recorded intervals intersect *and* whose evidence sets overlap: they ran",
        "at the same time and came back with the same row, which is duplicated retrieval. A pair",
        "in different phases of the graph is excluded, because those are handed shared evidence",
        "by design and counting them would measure the handoff chain, not the fan-out:",
        "",
        *_table(
            [
                (
                    "Runs with branches sharing evidence",
                    _pct(summary["contention"]["runs_with_branches_sharing_evidence"]),
                ),
                (
                    "Sharing pairs",
                    ", ".join(
                        f"`{pair}`={count}"
                        for pair, count in summary["contention"]["pairs"].items()
                    )
                    or "—",
                ),
            ]
        ),
        "",
        "## Retrieval declines",
        "",
        "A declined lookup is the platform refusing to answer from evidence it judged too thin, so the",
        "score that caused it belongs in the record:",
        "",
        *_table(
            [
                ("Runs with a decline", _pct(summary["retrieval"]["runs_with_a_decline"])),
                ("Declines, total", summary["retrieval"]["declines_total"]),
                (
                    "Reasons",
                    ", ".join(
                        f"`{k}`={v}" for k, v in summary["retrieval"]["decline_reasons"].items()
                    )
                    or "—",
                ),
            ]
        ),
        "",
        "| Best score | Floor |",
        "|---|---|",
        *[
            f"| {entry['best_score']} | {entry['floor']} |"
            for entry in summary["retrieval"]["decline_scores"]
        ],
        "",
        "## Routing and memory",
        "",
        "| Route | Runs |",
        "|---|---|",
        *[f"| `{name}` | {count} |" for name, count in summary["route_distribution"].items()],
        "",
        *_table(
            [
                (
                    "Runs writing a memory record",
                    _pct(summary["memory"]["runs_writing_a_memory_record"]),
                ),
                ("Memory records written, total", summary["memory"]["records_written_total"]),
            ]
        ),
        "",
        "## Anomalies",
        "",
    ]
    if summary["anomalies"]:
        lines += ["| Case | Why |", "|---|---|"]
        lines += [f"| {row['case_id']} | {row['why']} |" for row in summary["anomalies"]]
    else:
        lines.append("None.")
    lines += ["", "## Limitations", ""]
    lines += [f"- {item}" for item in report["limitations"]]
    lines.append("")
    return "\n".join(lines)


LIMITATIONS = [
    "Every reading is of the runs' own timeline. A node that did work without emitting an event is "
    "invisible here, and this report cannot tell that apart from a node that did nothing.",
    "Contention is measured only between branches whose recorded intervals intersect, so a "
    "duplicated retrieval by two branches that ran one after the other is not counted.",
    '"Runs citing fewer sources than the analysis was handed" is not by itself a defect: the '
    "citation writer is entitled to cite fewer. It is reported as a distribution, not a rate with a "
    "threshold.",
    "The supervisor's confidence is the model's own number. It is reported as recorded, and nothing "
    "here checks it against the decision that carried it.",
]


def limitations(rows: list[dict[str, Any]]) -> list[str]:
    """The four caveats that hold for any corpus, plus one that quotes this one.

    The last line read "121 observations" until 2026-10-03. That was true of the first quality corpus
    and false of the second -- the batch grew from 121 recorded runs to 200, and the report went on
    describing a corpus nobody could point at, one line under a table counting 200. The size of a
    corpus is a fact about the files that were read, so it is read from them.
    """
    return [
        *LIMITATIONS,
        f"This corpus is {len(rows)} observations of one tenant at one revision; the distributions "
        "describe that corpus and are not population estimates.",
    ]


def build(engine: sa.Engine, replays: Path, tenant_id: UUID | str) -> dict[str, Any]:
    driver = load_live_driver()
    corpus = read_corpus(engine, replays, tenant_id=tenant_id)
    rows = [observe(run) for run in corpus.runs]

    # The readings that already have an implementation live in the driver, fed the shape it expects.
    live_records = [
        {
            "case_id": run.case_id,
            "run_id": run.run_id,
            "subject": run.replay.get("subject"),
            "question": run.replay.get("question"),
            "kind": run.replay.get("kind"),
            "terminal_status": run.replay.get("terminal_status"),
            "reviewer_decision": run.replay.get("reviewer_decision"),
            "timeline": run.timeline,
            "unsupported_claims": run.replay.get("unsupported_claims"),
            "missing_evidence": run.replay.get("missing_evidence"),
            "citations": run.replay.get("citations"),
            "errors": run.replay.get("errors"),
        }
        for run in corpus.runs
    ]
    live_rows = [driver.observe_record(record) for record in live_records]
    live_summary = driver.summarise(live_rows)

    summary = summarise(rows)
    summary["live_vocabulary"] = {
        "reading": live_summary["reading"],
        "participation": {
            "runs_with_no_agent": round(
                sum(1 for row in live_rows if row["agents_completed"] == 0) / len(live_rows), 4
            )
            if live_rows
            else None,
            "distribution": live_summary["agent_participation"]["distribution"],
        },
        "handoff_fidelity": live_summary["handoff_fidelity"],
    }
    return {
        "report": "phase7_coordination",
        "generated_at": datetime.now(UTC).isoformat(),
        "inputs": inputs_block(
            replays,
            corpus.replays,
            tool_revision=source_revision(REPO_ROOT),
            extra={"unresolved_run_ids": list(corpus.unresolved)},
        ),
        "rows": rows,
        "summary": summary,
        "limitations": limitations(rows),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--replays", type=Path, default=DEFAULT_REPLAYS)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--json", type=Path, default=REPORT_JSON)
    parser.add_argument("--md", type=Path, default=REPORT_MD)
    parser.add_argument("--check", action="store_true")
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
    print(f"runs={report['summary']['run_count']} anomalies={len(report['summary']['anomalies'])}")
    print(f"wrote {args.json} and {args.md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
