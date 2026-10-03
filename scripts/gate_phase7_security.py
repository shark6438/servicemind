"""The P7.6.5 security-scenario gate: judge the recorded observations, write the report.

Exit codes, matching the acceptance gate so one CI job can read both:

``0``  every scenario in the set passed on evidence that was actually produced
``1``  a scenario failed -- a test red, a script non-zero, or a mutation that removed the
       behaviour without the pinned test noticing
``2``  not enough observation -- a replay missing, a test skipped, a mutation that did not
       grade. Nothing was learned about those scenarios, which is not the same as passing
``3``  the gate is misconfigured -- a replay recorded against a different scenario list, or
       a scenario list that does not validate

It never runs the scenarios. A gate that produced its own observations would grade whatever
it happened to execute and call it the platform, which is what ``--force`` exists to
prevent. ``--replay-only`` is accepted and is the only way it does anything, because
judging the files on disk is the whole job.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from servicemind.evaluation.refusal import stamp_refusal
from servicemind.evaluation.revisions import recorded_revisions, revision_problems
from servicemind.evaluation.security import (
    SecurityScenarioSet,
    load_security_scenarios,
    scenario_set_digest,
)
from servicemind.evaluation.security_grader import (
    ScenarioObservation,
    Verdict,
    dump_outcome,
    grade,
    outcome_summary,
    render_report,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = REPO_ROOT / "evaluation" / "security" / "scenarios.v1.json"
REPLAYS = REPO_ROOT / "evaluation" / "security" / "replays"
REPORTS = REPO_ROOT / "evaluation" / "reports"

#: Repo-relative, so a refusal stamped into a report names the gate that stamped it.
GATE_NAME = str(Path(__file__).resolve().relative_to(REPO_ROOT))
REPORT_JSON = REPORTS / "phase7_security_latest.json"
REPORT_MD = REPORTS / "phase7_security_latest.md"

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INSUFFICIENT = 2
EXIT_CONFIGURATION = 3


class ConfigurationError(Exception):
    """The gate cannot say anything about the platform until this is fixed."""


def load_scenario_set() -> SecurityScenarioSet:
    try:
        return load_security_scenarios(SCENARIOS)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise ConfigurationError(f"{SCENARIOS.relative_to(REPO_ROOT)}: {exc}") from exc


def load_replays(scenario_set: SecurityScenarioSet) -> list[ScenarioObservation]:
    """Every recorded observation on disk, refusing ones from another scenario list.

    A replay whose ``scenarios_digest`` disagrees was recorded against a different set, and
    its PASS is about a scenario that no longer reads the way it did. Silently dropping it
    would turn "this scenario was never verified" into "this scenario is absent from the
    table", which is the failure mode the whole gate is built to avoid.
    """
    expected = scenario_set_digest(scenario_set)
    known = {scenario.id for scenario in scenario_set.scenarios}
    observations: list[ScenarioObservation] = []
    stale: list[str] = []
    for path in sorted(REPLAYS.glob("*.json")):
        try:
            observation = ScenarioObservation.model_validate(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise ConfigurationError(f"{path.relative_to(REPO_ROOT)}: {exc}") from exc
        if observation.scenario_id not in known:
            stale.append(f"{path.name}: no scenario {observation.scenario_id!r} in the set")
        elif observation.scenarios_digest != expected:
            stale.append(
                f"{path.name}: recorded against scenario list "
                f"{observation.scenarios_digest[:16]}..., the set on disk is {expected[:16]}..."
            )
        else:
            observations.append(observation)
    if stale:
        raise ConfigurationError("; ".join(stale))
    return observations


def check_replays_are_one_revision(
    observations: list[ScenarioObservation], *, expect: str | None
) -> dict[str, Any]:
    """Refuse to grade replays that do not all describe the same platform revision.

    ``scenarios_digest`` binds an observation to the scenario it was taken against and says
    nothing about which code answered. The grader already collects the distinct revisions
    for its report -- it renders them as ``被测版本`` -- and this is the same set, made a
    precondition instead of a caption. See ``evaluation/revisions.py``.

    Returns the revision the batch describes, for the caller to print alongside the verdict.
    """
    problems = revision_problems([item.deployed_revision for item in observations], expect=expect)
    if problems:
        raise ConfigurationError(
            "; ".join(problems) + ". Re-run scripts/verify_phase7_security.py on the "
            "deployment you mean to describe"
        )
    return {
        "deployed_revisions": list(
            recorded_revisions([item.deployed_revision for item in observations])
        )
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="judge the observations on disk")
    parser.add_argument("--replay-only", action="store_true", help="grade recorded observations")
    parser.add_argument("--format", default="markdown", choices=("markdown", "json"))
    parser.add_argument("--report", type=Path, default=None, help="where to write the report")
    parser.add_argument(
        "--expect-revision",
        default=None,
        help=(
            "the source revision these replays must have been taken against, as the driver "
            "records it (``<sha>`` or ``<sha>+patch(<hex>)``). Without it the gate still "
            "refuses replays spanning more than one revision, but cannot tell a single "
            "stale revision from the current one"
        ),
    )
    args = parser.parse_args()

    # Resolved before the provenance check rather than after it, so the refusal path can
    # annotate the very files this run would have written. A refusal that returns 3 and
    # leaves yesterday's ``PASS`` on disk is a report contradicting its own exit code.
    report_json = args.report.with_suffix(".json") if args.report else REPORT_JSON
    report_md = (
        args.report
        if args.report and args.report.suffix == ".md"
        else (REPORT_MD if args.report is None else args.report.with_suffix(".md"))
    )

    try:
        scenario_set = load_scenario_set()
        observations = load_replays(scenario_set)
        provenance = check_replays_are_one_revision(observations, expect=args.expect_revision)
    except ConfigurationError as exc:
        print(json.dumps({"configuration_error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        stamp_refusal(report_json, report_md, gate=GATE_NAME, reason=str(exc), argv=sys.argv)
        return EXIT_CONFIGURATION

    outcome = grade(scenario_set, observations, generated_at=datetime.now(tz=UTC))

    REPORTS.mkdir(parents=True, exist_ok=True)
    report_json.write_text(dump_outcome(outcome) + "\n", encoding="utf-8")
    report_md.write_text(render_report(outcome), encoding="utf-8")

    summary = {
        **outcome_summary(outcome),
        "report_json": str(report_json),
        "report_md": str(report_md),
        **provenance,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if outcome.verdict is Verdict.PASS:
        return EXIT_PASS
    if outcome.verdict is Verdict.FAIL:
        return EXIT_FAIL
    missing = [
        scenario.id
        for scenario in scenario_set.scenarios
        if scenario.id not in {observation.scenario_id for observation in observations}
    ]
    if missing:
        print(json.dumps({"not_run": missing}, ensure_ascii=False), file=sys.stderr)
    return EXIT_INSUFFICIENT


if __name__ == "__main__":
    raise SystemExit(main())
