"""Judge the recorded quality observations, and refuse to judge stale ones.

The same two responsibilities as the acceptance and security gates, and the same exit
codes, because a reader who has learned one should not have to learn the other:

   0  PASS             every case came out the way the case list said it would
   1  FAIL             at least one case did not, or the answerable rate fell short
   2  INSUFFICIENT     cases were not observed; nothing was learned about them
   3  CONFIGURATION    the observations and the case list have drifted apart

**This gate never runs the platform.** There is no ``--live``: the observations have to come
from whatever version of the platform is deployed, and a gate that graded older observations
under a live invocation would report a run that never happened. Producing observations is
``scripts/verify_phase7_quality_live.py``'s job and only its job.

**Why the drift check is the load-bearing part.** Somebody edits a case's expected citation,
and the recorded observations still describe the old question. Graded again, they produce a
verdict that looks like a measurement of the new case list and is really a measurement of the
old one. Each observation carries the ``cases_digest`` it was taken under, and the batch file
carries the digest of the whole set it ran against, so the comparison is between what the
cases said and what they say now -- not between two timestamps.

A report on disk whose digests disagree with its inputs is refused for the same reason, and
``--force`` is the only way past it. Using ``--force`` to make an acceptance run pass is
exactly the thing the digests exist to make visible, so it is named in the report when used.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from servicemind.evaluation.quality import (
    QualityCaseSet,
    case_set_digest,
    load_quality_cases,
)
from servicemind.evaluation.quality_grader import (
    CaseObservation,
    QualityOutcome,
    Verdict,
    dump_outcome,
    grade,
    outcome_summary,
    render_report,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
QUALITY = REPO_ROOT / "evaluation" / "quality"
CASES = QUALITY / "cases.v1.json"
REPLAYS = QUALITY / "replays"
BATCH = REPLAYS / "_batch.json"
REPORTS = REPO_ROOT / "evaluation" / "reports"
REPORT_JSON = REPORTS / "phase7_quality_latest.json"
REPORT_MD = REPORTS / "phase7_quality_latest.md"

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INSUFFICIENT = 2
EXIT_CONFIGURATION = 3

#: The keys a recorded observation must carry before it can be graded. A field added to the
#: model later would otherwise be silently defaulted on every old replay, which is how a
#: batch recorded before a rule existed gets graded as though it had satisfied the rule.
REQUIRED_OBSERVATION_FIELDS = (
    "case_id",
    "terminal_status",
    "reviewer_decision",
    "citations",
    "errors",
    "cases_digest",
    # The insufficient-evidence rule reads these, and each of them means something different
    # when absent than when zero: no ``unsupported_claims`` key at all is a run nothing was
    # learned about, while ``unsupported_claims == 0`` is a run that was read and came back
    # clean. Requiring the keys is what keeps the first from being recorded as the second.
    "unsupported_claims",
    "missing_evidence",
    "proposed_actions",
    "review_findings",
)


class ConfigurationError(RuntimeError):
    """The inputs do not describe one another. Exit 3, never a verdict."""


def load_case_set() -> QualityCaseSet:
    if not CASES.exists():
        raise ConfigurationError(f"{CASES.relative_to(REPO_ROOT)} does not exist")
    return load_quality_cases(CASES)


def load_observations(case_set: QualityCaseSet) -> list[CaseObservation]:
    """Every observation on disk, whichever cases it names.

    Observations for ids the case list no longer contains are dropped with a note rather
    than raising: deleting a case is a legitimate edit, and the drift check below is what
    catches the case that was *changed* rather than removed.
    """
    if not REPLAYS.exists():
        return []
    known = {case.id for case in case_set.cases}
    observations: list[CaseObservation] = []
    for path in sorted(REPLAYS.glob("*.json")):
        if path.name.startswith("_"):
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("case_id") not in known:
            continue
        missing = [field for field in REQUIRED_OBSERVATION_FIELDS if field not in payload]
        if missing:
            raise ConfigurationError(
                f"{path.name} is missing {missing}; it was recorded by an older driver and "
                "cannot be graded without the fields the current rules read"
            )
        observations.append(
            CaseObservation.model_validate(
                {
                    key: value
                    for key, value in payload.items()
                    # The recording carries the case's question, kind and subject for a human
                    # reading the file. The model forbids extra fields, deliberately, so the
                    # reader keeps only what a verdict may depend on.
                    if key in CaseObservation.model_fields
                }
            )
        )
    return observations


def check_observations_match_cases(
    case_set: QualityCaseSet, observations: list[CaseObservation]
) -> dict[str, Any]:
    """The drift between what was observed and what the case list now says.

    Two comparisons, because they catch different edits. Per-observation digests catch a
    case whose *content* changed -- the expectation that moved under a stable id. The batch
    digest catches a case that was added or removed, which no per-observation digest can
    see, since the observations that would have carried it do not exist.
    """
    expected = case_set_digest(case_set)
    notes: dict[str, Any] = {}

    unstamped = [item.case_id for item in observations if not item.cases_digest]
    stale = [item.case_id for item in observations if item.cases_digest != expected]
    if stale:
        notes["stale_cases"] = sorted(stale)
    if unstamped:
        notes["unstamped_cases"] = sorted(unstamped)

    if BATCH.exists():
        recorded = json.loads(BATCH.read_text(encoding="utf-8"))
        if recorded.get("cases_digest") != expected:
            notes["batch_cases_digest"] = {
                "previous": str(recorded.get("cases_digest")),
                "current": expected,
            }
    elif observations:
        notes["batch_file_missing"] = (
            f"{BATCH.relative_to(REPO_ROOT)} does not exist, so the concurrency and revision "
            "these observations were recorded under are unknown"
        )
    return notes


def check_report_is_current(outcome: QualityOutcome, report_json: Path) -> dict[str, Any]:
    """The drift between the report on disk and the inputs it was generated from."""
    if not report_json.exists():
        return {}
    recorded = json.loads(report_json.read_text(encoding="utf-8"))
    notes: dict[str, Any] = {}
    if recorded.get("cases_digest") != outcome.cases_digest:
        notes["report_cases_digest"] = {
            "previous": str(recorded.get("cases_digest")),
            "current": outcome.cases_digest,
        }
    if recorded.get("observations_digest") != outcome.observations_digest:
        notes["report_observations_digest"] = {
            "previous": str(recorded.get("observations_digest")),
            "current": outcome.observations_digest,
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
                    "error": "this gate does not run the quality batch",
                    "why": "the observations must be produced by whatever version of the "
                    "platform is deployed; a gate that graded older observations under a "
                    "--live invocation would report a run that never happened",
                    "instead": "uv run python scripts/verify_phase7_quality_live.py",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return EXIT_CONFIGURATION

    try:
        case_set = load_case_set()
        observations = load_observations(case_set)
        drift = {} if args.force else check_observations_match_cases(case_set, observations)
    except ConfigurationError as exc:
        print(json.dumps({"configuration_error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return EXIT_CONFIGURATION

    outcome = grade(case_set, observations, generated_at=datetime.now(tz=UTC))

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
    # case that ran and could not be judged is a finding about the platform, and a case with
    # no observation is the batch having not been run. Both are insufficient observation --
    # one exit code -- but a reader should not have to guess which.
    missing = [
        case.id
        for case in case_set.cases
        if case.id not in {observation.case_id for observation in observations}
    ]
    if missing:
        print(
            json.dumps({"not_run": missing[:40], "total": len(missing)}, ensure_ascii=False),
            file=sys.stderr,
        )
    return EXIT_INSUFFICIENT


if __name__ == "__main__":
    raise SystemExit(main())
