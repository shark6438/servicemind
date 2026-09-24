"""Judge the recorded load observations, and refuse to judge stale ones.

The same two responsibilities as the acceptance, security and quality gates, and the same
exit codes, because a reader who has learned one should not have to learn the other:

   0  PASS             every declared run happened and behaved the way the baseline says it should
   1  FAIL             at least one run changed under load, or wedged, or lost its citation
   2  INSUFFICIENT     runs were not observed; nothing was learned about them
   3  CONFIGURATION    the observations and the plan have drifted apart

**This gate never runs the platform.** There is no ``--live``: the observations have to come
from whatever version of the platform is deployed, and a gate that graded older observations
under a live invocation would report a run that never happened. Producing observations is
``scripts/verify_phase7_load_live.py``'s job and only its job.

**Why the drift check is the load-bearing part.** Plan digests are the same device the other
three gates use, and they carry more weight here, because a load measurement is a measurement
*of a workload*. Editing one of the twenty questions changes what the platform was asked to
do; editing a tier's concurrency changes what it was asked to do it under. Either way the
recorded latencies still parse, still average, and no longer describe anything current -- so
the gate compares rather than trusting, and exits 3.

A report on disk whose digests disagree with its inputs is refused for the same reason, and
``--force`` is the only way past it. Using ``--force`` to make a load run pass is exactly the
thing the digests exist to make visible, so it is named in the report when used.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from servicemind.evaluation.load import (
    LoadPlan,
    Workload,
    load_plan,
    plan_digest,
    resolve_workload,
    workload_digest,
)
from servicemind.evaluation.load_grader import (
    BatchHeader,
    LoadOutcome,
    RunObservation,
    Verdict,
    dump_outcome,
    grade,
    observations_digest,
    outcome_summary,
    render_report,
    unplanned_tiers,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
LOAD = REPO_ROOT / "evaluation" / "load"
PLAN = LOAD / "plan.v1.json"
REPLAYS = LOAD / "replays"
BATCH = REPLAYS / "_batch.json"
CASES = REPO_ROOT / "evaluation" / "quality" / "cases.v1.json"
REPORTS = REPO_ROOT / "evaluation" / "reports"
REPORT_JSON = REPORTS / "phase7_load_latest.json"
REPORT_MD = REPORTS / "phase7_load_latest.md"

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INSUFFICIENT = 2
EXIT_CONFIGURATION = 3

#: The keys a recorded observation must carry before it can be graded, for the same reason
#: the quality gate requires its own: a field added to the model later would be silently
#: defaulted on every old replay, and a batch recorded before a rule existed would be graded
#: as though it had satisfied the rule.
REQUIRED_OBSERVATION_FIELDS = (
    "tier",
    "concurrency",
    "repeat_index",
    "case_id",
    "terminal_status",
    "citations",
    "expected_citations",
    "errors",
    "plan_digest",
    "workload_digest",
)


class ConfigurationError(RuntimeError):
    """The inputs do not describe one another. Exit 3, never a verdict."""


def load_observations() -> list[RunObservation]:
    """Every recorded run, whichever tier it names.

    A run whose tier the plan no longer declares is *kept* rather than dropped: the grader
    names it as a finding, because the alternatives are worse. Dropping it silently is how a
    batch reports full coverage over a directory it only partly read.
    """
    if not REPLAYS.exists():
        return []
    observations: list[RunObservation] = []
    for path in sorted(REPLAYS.glob("*.json")):
        if path.name.startswith("_"):
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        missing = [field for field in REQUIRED_OBSERVATION_FIELDS if field not in payload]
        if missing:
            raise ConfigurationError(
                f"{path.name} is missing {missing}; it was recorded by an older driver and "
                "cannot be graded without the fields the current rules read"
            )
        observations.append(RunObservation.model_validate(payload))
    return observations


def load_batch() -> BatchHeader | None:
    if not BATCH.exists():
        return None
    return BatchHeader.model_validate(json.loads(BATCH.read_text(encoding="utf-8")))


def check_observations_match_plan(
    plan: LoadPlan,
    workload: Workload,
    observations: list[RunObservation],
) -> dict[str, Any]:
    """The drift between what was observed and what the plan now says.

    Three comparisons, because they catch different edits. The per-observation plan digest
    catches a tier whose concurrency or repeats moved under a stable name. The workload digest
    catches an edit to a question -- which no plan digest can see, since the plan names the
    case list rather than its contents. And the tier names catch an observation left behind by
    a plan that ran a tier this one does not.
    """
    expected_plan = plan_digest(plan)
    expected_workload = workload_digest(workload)
    notes: dict[str, Any] = {}

    stale_plan = [item.slot() for item in observations if item.plan_digest != expected_plan]
    stale_workload = [
        item.slot() for item in observations if item.workload_digest != expected_workload
    ]
    if stale_plan:
        notes["stale_plan"] = sorted(set(stale_plan))[:20]
    if stale_workload:
        notes["stale_workload"] = sorted(set(stale_workload))[:20]
    unplanned = unplanned_tiers(plan, observations)
    if unplanned:
        notes["unplanned_tiers"] = unplanned

    header = load_batch()
    if header is not None:
        if header.plan_digest and header.plan_digest != expected_plan:
            notes["batch_plan_digest"] = {
                "previous": header.plan_digest,
                "current": expected_plan,
            }
        if header.workload_digest and header.workload_digest != expected_workload:
            notes["batch_workload_digest"] = {
                "previous": header.workload_digest,
                "current": expected_workload,
            }
    elif observations:
        notes["batch_file_missing"] = (
            f"{BATCH.relative_to(REPO_ROOT)} does not exist, so the tier wall clocks and the "
            "revision these runs were taken under are unknown"
        )
    return notes


def check_report_is_current(outcome: LoadOutcome, report_json: Path) -> dict[str, Any]:
    """The drift between the report on disk and the inputs it was generated from."""
    if not report_json.exists():
        return {}
    recorded = json.loads(report_json.read_text(encoding="utf-8"))
    notes: dict[str, Any] = {}
    if recorded.get("plan_digest") != outcome.plan_digest:
        notes["report_plan_digest"] = {
            "previous": str(recorded.get("plan_digest")),
            "current": outcome.plan_digest,
        }
    if recorded.get("workload_digest") != outcome.workload_digest:
        notes["report_workload_digest"] = {
            "previous": str(recorded.get("workload_digest")),
            "current": outcome.workload_digest,
        }
    return notes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="judge the observations on disk")
    parser.add_argument("--replay-only", action="store_true", help="grade recorded observations")
    parser.add_argument(
        "--live",
        action="store_true",
        help="accepted for compatibility only; this gate never runs the platform",
    )
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
                    "error": "this gate does not run the load batch",
                    "why": "the observations must be produced by whatever version of the "
                    "platform is deployed; a gate that graded older observations under a "
                    "--live invocation would report a run that never happened",
                    "instead": "uv run python scripts/verify_phase7_load_live.py",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return EXIT_CONFIGURATION

    try:
        plan = load_plan(PLAN)
        workload = resolve_workload(plan, CASES)
        observations = load_observations()
        drift = {} if args.force else check_observations_match_plan(plan, workload, observations)
        batch = load_batch()
    except ConfigurationError as exc:
        print(json.dumps({"configuration_error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return EXIT_CONFIGURATION
    except ValueError as exc:
        # ``resolve_workload`` refuses a case list that no longer holds the declared number of
        # questions. That is a configuration error, not a verdict: nothing was measured.
        print(json.dumps({"configuration_error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return EXIT_CONFIGURATION

    outcome = grade(
        plan,
        workload,
        observations,
        generated_at=datetime.now(tz=UTC),
        batch=batch,
    )

    report_json = args.report.with_suffix(".json") if args.report else REPORT_JSON
    report_md = (
        args.report
        if args.report and args.report.suffix == ".md"
        else (REPORT_MD if args.report is None else args.report.with_suffix(".md"))
    )

    if not args.force:
        try:
            drift |= check_report_is_current(outcome, report_json)
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"the existing report at {report_json} will not parse: {exc}") from exc

    REPORTS.mkdir(parents=True, exist_ok=True)
    report_json.write_text(dump_outcome(outcome), encoding="utf-8")
    report_md.write_text(render_report(outcome), encoding="utf-8")

    summary = {
        **outcome_summary(outcome),
        "observations_digest": observations_digest(observations),
        # After the summary's own keys, so a drift note can never be shadowed by a
        # same-named field in the outcome.
        "report_json": str(report_json),
        "report_md": str(report_md),
        "forced": bool(args.force),
        **drift,
    }
    if args.format == "markdown":
        print(render_report(outcome))
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if outcome.verdict is Verdict.PASS:
        return EXIT_PASS
    if outcome.verdict is Verdict.FAIL:
        return EXIT_FAIL
    # BLOCKED. Named on stderr so the two ways to reach it stay distinguishable in a log: a
    # tier that ran and could not be judged is a finding about the platform, and a tier with
    # no observations is the batch having not been run. Both are insufficient observation --
    # one exit code -- but a reader should not have to guess which.
    unrun: list[str] = []
    for tier in plan.tiers:
        seen = {item.slot() for item in observations if item.tier == tier.name}
        for repeat in range(tier.repeats):
            for case in workload.cases:
                label = f"{case.id}#{repeat}"
                if label not in seen:
                    unrun.append(f"{tier.name}:{label}")
    if unrun:
        print(
            json.dumps({"not_run": sorted(unrun)[:40], "total": len(unrun)}, ensure_ascii=False),
            file=sys.stderr,
        )
    return EXIT_INSUFFICIENT


if __name__ == "__main__":
    raise SystemExit(main())
