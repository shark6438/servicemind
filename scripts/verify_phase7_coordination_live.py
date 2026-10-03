"""Measure what the agent graph actually did on a run, from its own timeline.

**The gap this fills.** The acceptance batch asserts *outcomes*: a run settled, a document
was cited, a write happened once. Coordination is the layer under those outcomes -- which
sub-agents ran, whether the supervisor arbitrated or accepted the first proposal, whether a
plan was revised, whether work was escalated to a person. Every one of those is emitted as a
timeline event by the node that did it, and until now nothing read the timeline as a whole
and counted it. A run whose supervisor rejected its own agent's decision twice and a run
whose supervisor accepted immediately both end at ``succeeded``; the outcome assertions
cannot tell them apart, and this batch exists so a reader can.

**What is read, and from where.** Two independent surfaces, deliberately:

* the run's timeline, for participation -- ``agent.completed`` per sub-agent, the
  supervisor's ``decision`` / ``policy_rejected`` / ``decision_rejected``, ``plan.validated``,
  ``review.completed`` and ``run.escalation_accepted``;
* the run's result, for handoff fidelity -- the reviewer's ``unsupported_claims`` and
  ``missing_evidence`` counts, which are the platform's own account of evidence that did not
  survive the trip from retrieval to the answer.

Reading both matters: a timeline that shows every agent completing says the graph ran, not
that it ran *usefully*; the reviewer's counts say whether what the agents produced held up.
Neither alone answers "did coordination work".

**What this is not.** It is not a scoring model and it invents no threshold. It reports the
distribution it observed and the anomalies that are true by construction -- a run with no
``run.created``, or a run that reached a terminal state with no agent participation recorded
at all, is reported as an anomaly rather than averaged into a rate. Whether a particular
distribution is good is a judgment for whoever reads it next.

Usage:

    uv run python scripts/verify_phase7_coordination_live.py --tenant-id 2222... [--per-kind 3]
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from servicemind.evaluation.deployment import refuse_stale_deployment

REPO_ROOT = Path(__file__).resolve().parents[1]
QUALITY_DRIVER = REPO_ROOT / "scripts" / "verify_phase7_quality_live.py"
REPLAYS = REPO_ROOT / "evaluation" / "coordination" / "replays"
BATCH = REPLAYS / "_batch.json"

DEFAULT_BASE_URL = "http://127.0.0.1:18080"
DEFAULT_CASE_BUDGET_SECONDS = 240.0
DEFAULT_PER_KIND = 3

#: The timeline events this batch counts, grouped by the coordination question each answers.
#: Named as literals here rather than derived, because the point of the batch is to fix the
#: vocabulary it measured against: a renamed event should show up as a count that dropped to
#: zero, which is visible, not as a comparison that silently kept matching.
PARTICIPATION = ("agent.completed",)
ARBITRATION = ("supervisor.decision", "supervisor.policy_rejected", "supervisor.decision_rejected")
PLANNING = ("plan.validated",)
HUMAN = ("review.completed", "run.escalation_accepted")
TERMINALS = ("run.succeeded", "run.failed", "run.abstained", "run.cancelled", "run.rejected")


def load_quality_driver() -> Any:
    """The quality driver as a module, for its ``Stack``, ``Recorder`` helpers and loaders.

    Loaded by path and registered in ``sys.modules`` before ``exec_module`` for the dataclass
    resolution reason both other drivers document; see
    ``scripts/verify_phase7_reliability_live.py``.
    """
    spec = importlib.util.spec_from_file_location("_quality_driver", QUALITY_DRIVER)
    if spec is None or spec.loader is None:  # pragma: no cover -- a broken checkout
        raise RuntimeError(f"cannot load {QUALITY_DRIVER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def stratified(cases: list[Any], per_kind: int) -> list[Any]:
    """The sample: up to ``per_kind`` cases of each kind, in case-list order."""
    seen: dict[str, int] = {}
    chosen = []
    for case in cases:
        kind = case.kind.value
        if seen.get(kind, 0) < per_kind:
            seen[kind] = seen.get(kind, 0) + 1
            chosen.append(case)
    return chosen


def count_events(events: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        counts[event] = counts.get(event, 0) + 1
    return counts


def observe_record(record: dict[str, Any]) -> dict[str, Any]:
    """The coordination reading of one run, from its timeline events and its result."""
    events = record.get("timeline") or []
    counts = count_events(events)
    return {
        "case_id": record["case_id"],
        "run_id": record.get("run_id"),
        "subject": record.get("subject"),
        "question": record.get("question"),
        "kind": record.get("kind"),
        "terminal_status": record.get("terminal_status"),
        "reviewer_decision": record.get("reviewer_decision"),
        "agents_completed": sum(counts.get(name, 0) for name in PARTICIPATION),
        "supervisor_decisions": counts.get("supervisor.decision", 0),
        "supervisor_rejections": counts.get("supervisor.policy_rejected", 0)
        + counts.get("supervisor.decision_rejected", 0),
        "plan_validations": sum(counts.get(name, 0) for name in PLANNING),
        "human_handoffs": sum(counts.get(name, 0) for name in HUMAN),
        "terminal_events": sorted(name for name in TERMINALS if counts.get(name, 0)),
        "unsupported_claims": record.get("unsupported_claims"),
        "missing_evidence": record.get("missing_evidence"),
        "citations": len(record.get("citations") or []),
        "errors": record.get("errors") or [],
        "event_count": len(events),
    }


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The distribution the batch observed, plus the anomalies that are true by construction."""

    def _dist(key: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in rows:
            value = str(row.get(key))
            out[value] = out.get(value, 0) + 1
        return dict(sorted(out.items()))

    agent_counts = [row["agents_completed"] for row in rows]
    anomalies: list[dict[str, Any]] = []
    for row in rows:
        if row["errors"]:
            anomalies.append({"case_id": row["case_id"], "why": "the run recorded errors"})
        elif row["agents_completed"] == 0:
            anomalies.append(
                {
                    "case_id": row["case_id"],
                    "why": "reached a terminal state with no agent.completed event recorded",
                }
            )
        elif row["terminal_events"] == []:
            anomalies.append(
                {"case_id": row["case_id"], "why": "no terminal run.* event in the timeline"}
            )

    fidelity = {
        "runs_reporting_unsupported_claims": sum(
            1 for row in rows if (row.get("unsupported_claims") or 0) > 0
        ),
        "runs_reporting_missing_evidence": sum(
            1 for row in rows if (row.get("missing_evidence") or 0) > 0
        ),
        "runs_with_no_fidelity_signal": sum(
            1 for row in rows if row.get("unsupported_claims") is None
        ),
    }

    return {
        "rows": rows,
        "run_count": len(rows),
        "agent_participation": {
            "min": min(agent_counts) if agent_counts else None,
            "max": max(agent_counts) if agent_counts else None,
            "mean": round(sum(agent_counts) / len(agent_counts), 2) if agent_counts else None,
            "distribution": _dist("agents_completed"),
        },
        "supervisor": {
            "runs_with_a_decision": sum(1 for row in rows if row["supervisor_decisions"] > 0),
            "runs_with_a_rejection": sum(1 for row in rows if row["supervisor_rejections"] > 0),
            "rejections_total": sum(row["supervisor_rejections"] for row in rows),
        },
        "planning": {
            "runs_with_a_validated_plan": sum(1 for row in rows if row["plan_validations"] > 0),
        },
        "human_handoffs": {
            "runs_escalated_or_reviewed": sum(1 for row in rows if row["human_handoffs"] > 0),
        },
        "terminal_distribution": _dist("terminal_status"),
        "reviewer_decision_distribution": _dist("reviewer_decision"),
        "handoff_fidelity": fidelity,
        "anomalies": anomalies,
        "reading": (
            "Participation is read from the run timeline; fidelity is the reviewer's own "
            "count of unsupported claims and missing evidence. A run can show full agent "
            "participation and still carry unsupported claims -- that is the case this batch "
            "exists to make visible. No threshold is applied here; the distribution is the "
            "measurement."
        ),
    }


async def run(args: argparse.Namespace) -> int:
    # Refused before anything is observed, because the failure does not announce itself:
    # a process running code older than the tree answers every request competently, so the
    # batch completes and every record carries the revision of code the platform never ran.
    refusal = refuse_stale_deployment(REPO_ROOT, allow=args.allow_stale_deployment, argv=sys.argv)
    if refusal:
        return refusal

    driver = load_quality_driver()
    acceptance = driver.load_acceptance_driver()

    case_set = driver.load_quality_cases(driver.CASES)
    digest = driver.case_set_digest(case_set)
    tickets = driver.load_tickets()
    revision = acceptance.source_revision()

    selected = stratified(list(case_set.cases), args.per_kind)
    if not selected:
        raise SystemExit("the filters selected no cases")

    REPLAYS.mkdir(parents=True, exist_ok=True)
    stack = acceptance.Stack(
        base_url=args.base_url, tenant_id=UUID(args.tenant_id), timeout_scale=1.0
    )

    print(
        f"observing {len(selected)} runs for coordination, revision {revision}",
        flush=True,
    )
    rows: list[dict[str, Any]] = []
    try:
        await stack.await_health(seconds=args.health_timeout)
        for case in selected:
            # ``run_id``, ``subject`` and ``question`` are recorded because the batch's
            # readings are only half the measurement: the trajectory, the tool audit and
            # the control counters live in the database against this run, and a replay
            # that names no run cannot be joined to them afterwards. The question and the
            # subject are kept beside it so a run whose id was lost can still be matched
            # by what was asked, which is unique within a batch.
            record: dict[str, Any] = {
                "case_id": case.id,
                "kind": case.kind.value,
                "subject": case.subject,
                "question": case.question,
                "errors": [],
            }
            try:
                ticket_id = tickets.get(case.ticket_ref)
                response = await stack.create_run(
                    case.subject, ticket_id=ticket_id, goal=case.question, request_write=False
                )
                body = response.json()
                run_id = UUID(str(body["id"]))
                record["run_id"] = str(run_id)
                settled = await stack.settle(case.subject, run_id, time.monotonic() + args.budget)
                payload = settled.json()
                if payload.get("status") == "waiting_review":
                    await stack.resolve_review(
                        driver.RESOLVING_SUBJECT,
                        run_id,
                        decision="continue",
                        comment=driver.RESOLVING_COMMENT,
                    )
                    settled = await stack.settle(
                        case.subject, run_id, time.monotonic() + args.budget
                    )
                    payload = settled.json()
                result = payload.get("result")
                record["terminal_status"] = payload.get("status")
                record["reviewer_decision"] = driver.reviewer_decision(result)
                record["citations"] = list(driver.read_citations(result).source_record_ids)
                signals = driver.read_review_signals(result)
                record["unsupported_claims"] = signals.unsupported_claims
                record["missing_evidence"] = signals.missing_evidence
                record["timeline"] = await stack.timeline_events(case.subject, run_id)
            except Exception as exc:  # noqa: BLE001 -- one run must not end the batch
                record["errors"].append(f"{type(exc).__name__}: {exc}")
            observed = observe_record(record)
            observed["errors"] = record["errors"]
            rows.append(observed)
            (REPLAYS / f"{case.id}.json").write_text(
                json.dumps(observed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            marker = "ERR " if observed["errors"] else "    "
            print(
                f"{marker}{case.id} {observed['kind'][:20]:20} "
                f"{observed['terminal_status'] or '-':18} "
                f"agents={observed['agents_completed']:>2} "
                f"sup={observed['supervisor_decisions']}/{observed['supervisor_rejections']} "
                f"plan={observed['plan_validations']} "
                f"unsupported={observed['unsupported_claims']}",
                flush=True,
            )
    finally:
        await stack.aclose()

    summary = summarise(rows)
    summary.update(
        {
            "mode": "live",
            "base_url": args.base_url,
            "tenant_id": args.tenant_id,
            "recorded_at": datetime.now(UTC).isoformat(),
            "deployed_revision": revision,
            "cases_digest": digest,
            "concurrency": 1,
            "selected": [case.id for case in selected],
            "case_budget_seconds": args.budget,
        }
    )
    BATCH.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps({k: v for k, v in summary.items() if k != "rows"}, ensure_ascii=False, indent=1)
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True, help="the tenant the cases are in")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--per-kind", type=int, default=DEFAULT_PER_KIND)
    parser.add_argument("--budget", type=float, default=DEFAULT_CASE_BUDGET_SECONDS)
    parser.add_argument("--health-timeout", type=float, default=120.0)
    parser.add_argument(
        "--allow-stale-deployment",
        action="store_true",
        help=(
            "observe even though the serving process predates the tree, recording the gap "
            "as a note instead of refusing; the evidence will describe the older code"
        ),
    )
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
