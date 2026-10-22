"""Run the P7.6.5 security and fault scenarios and record what each one did.

This script only *records*. It never decides whether a scenario is satisfied -- that is
``servicemind.evaluation.security_grader``, a pure function over the files this script
writes. Keeping the two apart is what makes a PASS in the report traceable to a command
somebody can re-run: the report names the observation, the observation names the exit
status, and the exit status came from here.

Three kinds of evidence are executed, and they are executed differently on purpose:

* A **test** node id is run, not trusted. The set is a claim about the platform at this
  revision, and a test that passed last week is not evidence about this one.
* A **mutation** is a named entry in one of ``scripts/mutate_*.py``. The scripts mutate a
  file, run the pinned tests, restore the file and print one line per mutation; this script
  runs each experiment once and reads those lines back. A mutation whose tests stay green
  after the behaviour is removed is recorded as ``undetected`` -- the scenario has no
  teeth, which is a finding about the test rather than about the code underneath it.
* An **acceptance** entry names a case already recorded under
  ``evaluation/acceptance/replays/``. It is read, not re-run: re-observing ACC-22's
  identity-provider outage would need the same stack up, would take minutes, and would
  produce a second and weaker observation of the one thing that case already records. It
  is bound to the acceptance report's ``cases_digest`` so a replay from a different case
  list cannot be borrowed as evidence for this one.

Exits 0 when every scenario was executed. It does **not** exit non-zero for a FAIL -- the
gate does that, so that a red scenario still leaves behind the observation that says why.
Exit codes here are about the *run*: 0 recorded, 2 nothing to grade, 3 could not run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from servicemind.evaluation.acceptance import (
    CaseExecution,
    case_set_digest,
    execution_digest,
    load_acceptance_cases,
)
from servicemind.evaluation.acceptance_grader import grade as grade_acceptance
from servicemind.evaluation.deployment import refuse_stale_deployment
from servicemind.evaluation.pytest_evidence import grade_test_phases
from servicemind.evaluation.security import (
    EvidenceKind,
    SecurityScenarioSet,
    load_security_scenarios,
    observation_digest,
    scenario_set_digest,
)
from servicemind.evaluation.security_grader import (
    EvidenceOutcome,
    ObservedEvidence,
    ScenarioObservation,
)
from servicemind.evaluation.source_revision import (
    source_revision as evaluate_source_revision,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = REPO_ROOT / "evaluation" / "security" / "scenarios.v1.json"
REPLAYS = REPO_ROOT / "evaluation" / "security" / "replays"
REPORTS = REPO_ROOT / "evaluation" / "reports"
ACCEPTANCE_REPORT = REPORTS / "phase7_acceptance_latest.json"
ACCEPTANCE_REPLAYS = REPO_ROOT / "evaluation" / "acceptance" / "replays"
ACCEPTANCE_CASES = REPO_ROOT / "evaluation" / "acceptance" / "cases.v1.json"

#: ``mutation_harness`` prints ``{status:28} | {name}``. The status strings are its
#: vocabulary, read back verbatim rather than re-derived, and each one maps to an outcome
#: with a different meaning: RED is the experiment working, GREEN is the experiment finding
#: a hole, and the other two are the experiment not having run at all.
_MUTATION_LINE = re.compile(r"^(?P<status>\S.*?)\s+\|\s+(?P<name>.+)$")
_MUTATION_OUTCOMES = {
    "RED (good)": EvidenceOutcome.PASSED,
    "GREEN (BAD - no teeth)": EvidenceOutcome.UNDETECTED,
    "UNEXERCISED (all skipped)": EvidenceOutcome.UNEXERCISED,
    "UNRUN (no test ran)": EvidenceOutcome.UNRUNNABLE,
}

#: ``pytest -v`` prints ``{node id} {STATUS} [ {pct}%]``.
#:
#: Anchored on the trailing percentage rather than on ``\S+``, because a parametrised node
#: id is not a single token: ``::test_x[1000-aaaa...-a short digest can never match]``
#: contains spaces, and a ``\S+?`` node id stops at the first one and then fails to find a
#: status after it, so the whole line goes unmatched and the test reads as never having run.
_PYTEST_LINE = re.compile(
    r"^(?P<nodeid>.+?)\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+\[\s*\d+%\]\s*$"
)
_PYTEST_OUTCOMES = {
    "PASSED": EvidenceOutcome.PASSED,
    "FAILED": EvidenceOutcome.FAILED,
    "ERROR": EvidenceOutcome.FAILED,
    "SKIPPED": EvidenceOutcome.UNEXERCISED,
    "XFAIL": EvidenceOutcome.UNEXERCISED,
    "XPASS": EvidenceOutcome.UNEXERCISED,
}


def source_revision() -> str:
    """The source revision this run graded, as ``<sha>`` or ``<sha>+patch(<hex>)``.

    The scenario list is checked against the tree, not against a commit: the pinned
    behaviours live in the source, so a report naming only the commit would describe code
    the tests may never have imported. ``"unknown"`` rather than ``None`` here because this
    driver has always recorded a string, and the security grader reads it as one.
    """
    return evaluate_source_revision(REPO_ROOT) or "unknown"


def _tail(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1].strip() if lines else ""


def run_tests(node_ids: list[str], *, run_docker: bool = False) -> dict[str, ObservedEvidence]:
    """Execute the pinned tests, one pytest process per file, and read back each verdict.

    Grouped by file rather than run in one process: a collection error in one module would
    otherwise take every other module's verdict down with it, and the whole point of this
    run is to find out which scenarios did not hold. Node ids that never appear in the
    output are recorded as ``missing`` -- a test that could not be collected is evidence
    that was not produced, not evidence that passed.

    ``run_docker`` is passed through because two scenarios -- SEC-OUTBOX-02 and
    SEC-TENANT-06, the outbox retention sweep -- pin tests marked ``@pytest.mark.docker``,
    which ``tests/conftest.py`` skips unless the option is given. Without it those two are
    recorded ``unexercised``: the sweep's two central claims (that it retires delivered
    rows, and that RLS confines it to one tenant) sit behind a flag, and a scenario whose
    evidence did not run is a scenario that asserted nothing. The default stays off so the
    verifier still runs on a host with no PostgreSQL; the flag is the difference between
    "the evidence was not exercised" and "the host could not exercise it", and only the
    caller knows which is true.
    """
    by_file: dict[str, list[str]] = {}
    for node_id in node_ids:
        by_file.setdefault(node_id.split("::", 1)[0], []).append(node_id)

    recorded: dict[str, ObservedEvidence] = {}
    for path, ids in by_file.items():
        with tempfile.TemporaryDirectory(prefix="security-pytest-") as directory:
            evidence_path = Path(directory) / "phases.json"
            try:
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pytest",
                        *ids,
                        "-q",
                        "--tb=short",
                        "-p",
                        "no:randomly",
                        "-p",
                        "servicemind.evaluation.pytest_evidence",
                        *(["--run-docker"] if run_docker else []),
                    ],
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                    timeout=3600,
                    env={**os.environ, "SERVICEMIND_PYTEST_EVIDENCE": str(evidence_path)},
                )
                payload = json.loads(evidence_path.read_text()) if evidence_path.exists() else {}
                reports = payload.get("reports", [])
                process_ok = (
                    completed.returncode in {0, 1}
                    and payload.get("exitstatus") == completed.returncode
                )
                detail = _tail(completed.stdout + completed.stderr)
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                reports, process_ok, detail = [], False, type(exc).__name__
        for node_id in ids:
            rows = [
                r
                for r in reports
                if r["nodeid"] == node_id or r["nodeid"].startswith(node_id + "[")
            ]
            outcome = grade_test_phases(rows, process_ok=process_ok)
            recorded[node_id] = ObservedEvidence(
                kind=EvidenceKind.TEST,
                ref=node_id,
                outcome=outcome,
                detail=json.dumps({"summary": detail, "phases": rows}, sort_keys=True),
            )
    return recorded


def _aggregate(ran: list[tuple[str, EvidenceOutcome]]) -> EvidenceOutcome:
    outcomes = [outcome for _, outcome in ran]
    for outcome in (EvidenceOutcome.FAILED, EvidenceOutcome.MISSING, EvidenceOutcome.UNEXERCISED):
        if outcome in outcomes:
            return outcome
    return EvidenceOutcome.PASSED if outcomes else EvidenceOutcome.MISSING


def _tally(ran: list[tuple[str, EvidenceOutcome]]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for status, _ in ran:
        counts[status] = counts.get(status, 0) + 1
    return sorted(counts.items())


def run_mutation_script(script: str) -> dict[str, ObservedEvidence]:
    """Run one mutation experiment and read its per-mutation verdict lines.

    A script that cannot be found, or that dies before printing a verdict, leaves every
    mutation in it ungraded; each is recorded as its own ``missing`` row rather than
    collapsing the whole experiment to one line, because the scenarios name mutations
    individually and a scenario whose mutation was never graded is a scenario nothing was
    learned about.
    """
    completed = subprocess.run(
        ["uv", "run", "python", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=7200,
    )
    output = completed.stdout + completed.stderr
    recorded: dict[str, ObservedEvidence] = {}
    for line in output.splitlines():
        match = _MUTATION_LINE.match(line)
        if not match:
            continue
        status = match["status"].strip()
        if status not in _MUTATION_OUTCOMES:
            continue
        recorded[match["name"].strip()] = ObservedEvidence(
            kind=EvidenceKind.MUTATION,
            ref=script,
            mutation=match["name"].strip(),
            outcome=_MUTATION_OUTCOMES[status],
            detail=status,
        )
    if completed.returncode != 0:
        for name, row in list(recorded.items()):
            if row.outcome == EvidenceOutcome.PASSED:
                recorded[name] = row.model_copy(
                    update={
                        "outcome": EvidenceOutcome.MISSING,
                        "detail": "mutation experiment did not complete cleanly: " + _tail(output),
                    }
                )
    return recorded


def acceptance_verdicts() -> tuple[dict[str, str], str]:
    try:
        cases = load_acceptance_cases(ACCEPTANCE_CASES)
        report_bytes = ACCEPTANCE_REPORT.read_bytes()
        report = json.loads(report_bytes)
        digest = case_set_digest(cases)
        if report.get("cases_digest") != digest:
            return {}, "acceptance case-list digest mismatch"
        executions = [
            CaseExecution.model_validate_json(path.read_text())
            for path in sorted(ACCEPTANCE_REPLAYS.glob("*.json"))
        ]
        if not executions or len({e.case_id for e in executions}) != len(executions):
            return {}, "missing or duplicate acceptance observations"
        if {e.case_id for e in executions} != {case.id for case in cases.cases}:
            return {}, "acceptance observations do not cover exactly the declared cases"
        revision = source_revision()
        now = datetime.now(UTC)
        for execution in executions:
            env = execution.environment
            if env.deployed_revision != revision or revision == "unknown":
                return {}, "acceptance observations belong to a different source revision"
            if execution.cases_digest != digest:
                return {}, "acceptance replay case-list digest mismatch"
            if env.recorded_at.tzinfo is None or not timedelta(
                0
            ) <= now - env.recorded_at <= timedelta(hours=24):
                return {}, "acceptance observations are stale or have invalid timestamps"
        if report.get("observation_digest") != execution_digest(executions):
            return {}, "acceptance report does not match its raw observations"
        outcome = grade_acceptance(cases, executions, generated_at=now)
        expected = {case.case_id: case.verdict.value for case in outcome.cases}
        actual = {case["case_id"]: case["verdict"] for case in report["cases"]}
        if actual != expected or len(actual) != len(report["cases"]):
            return {}, "acceptance verdicts do not match independent replay grading"
        return expected, ""
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {}, f"acceptance evidence unavailable: {type(exc).__name__}"


def collect_evidence(
    scenario_set: SecurityScenarioSet,
    *,
    only: set[str],
    with_mutations: bool,
    run_docker: bool = False,
) -> dict[str, ObservedEvidence]:
    """Everything the run touches, keyed by ``(kind, ref, mutation)`` in string form."""
    wanted = [s for s in scenario_set.scenarios if not only or s.id in only]

    node_ids: list[str] = []
    scripts: list[str] = []
    needs_acceptance = False
    for scenario in wanted:
        for item in scenario.evidence:
            if item.kind is EvidenceKind.TEST and item.ref not in node_ids:
                node_ids.append(item.ref)
            elif item.kind is EvidenceKind.MUTATION and item.ref not in scripts:
                scripts.append(item.ref)
            elif item.kind is EvidenceKind.ACCEPTANCE:
                needs_acceptance = True

    collected: dict[str, ObservedEvidence] = {}

    def key_for(item: ObservedEvidence) -> str:
        return f"{item.kind.value}\0{item.ref}\0{item.mutation or ''}"

    print(f"running {len(node_ids)} pinned tests ...", file=sys.stderr)
    for item in run_tests(node_ids, run_docker=run_docker).values():
        collected[key_for(item)] = item

    if with_mutations:
        for script in scripts:
            print(f"running mutation experiment {script} ...", file=sys.stderr)
            for item in run_mutation_script(script).values():
                collected[key_for(item)] = item
    else:
        # Recorded as ungraded rather than omitted: the scenario declares this evidence,
        # and an observation that simply lacks the row would be reported as a run that
        # forgot it instead of a run that was asked not to do it.
        for scenario in wanted:
            for item in scenario.evidence:
                if item.kind is not EvidenceKind.MUTATION:
                    continue
                stub = ObservedEvidence(
                    kind=item.kind,
                    ref=item.ref,
                    mutation=item.mutation,
                    outcome=EvidenceOutcome.MISSING,
                    detail="--skip-mutations was given; the experiment was not run",
                )
                collected[key_for(stub)] = stub

    if needs_acceptance:
        verdicts, note = acceptance_verdicts()
        for scenario in wanted:
            for item in scenario.evidence:
                if item.kind is not EvidenceKind.ACCEPTANCE:
                    continue
                if note:
                    outcome, detail = EvidenceOutcome.MISSING, note
                elif item.ref not in verdicts:
                    outcome, detail = (
                        EvidenceOutcome.MISSING,
                        f"{item.ref} is not in the acceptance report",
                    )
                else:
                    recorded_verdict = verdicts[item.ref]
                    outcome = (
                        EvidenceOutcome.PASSED
                        if recorded_verdict == "PASS"
                        else EvidenceOutcome.UNEXERCISED
                        if recorded_verdict == "BLOCKED"
                        else EvidenceOutcome.FAILED
                    )
                    replay_path = ACCEPTANCE_REPLAYS / f"{item.ref}.json"
                    detail = json.dumps(
                        {
                            "case": item.ref,
                            "verdict": recorded_verdict,
                            "source_revision": source_revision(),
                            "report_sha256": hashlib.sha256(
                                ACCEPTANCE_REPORT.read_bytes()
                            ).hexdigest(),
                            "replay_sha256": hashlib.sha256(replay_path.read_bytes()).hexdigest(),
                            "observed_at": json.loads(replay_path.read_text())["environment"][
                                "recorded_at"
                            ],
                        },
                        sort_keys=True,
                    )
                stub = ObservedEvidence(
                    kind=EvidenceKind.ACCEPTANCE,
                    ref=item.ref,
                    outcome=outcome,
                    detail=detail,
                )
                collected[key_for(stub)] = stub

    return collected


def build_observations(
    scenario_set: SecurityScenarioSet,
    collected: dict[str, ObservedEvidence],
    *,
    revision: str,
    observed_at: datetime,
    only: set[str],
) -> list[ScenarioObservation]:
    digest = scenario_set_digest(scenario_set)
    observations: list[ScenarioObservation] = []
    for scenario in scenario_set.scenarios:
        if only and scenario.id not in only:
            continue
        rows: list[ObservedEvidence] = []
        for item in scenario.evidence:
            key = f"{item.kind.value}\0{item.ref}\0{item.mutation or ''}"
            row = collected.get(key)
            if row is None:
                row = ObservedEvidence(
                    kind=item.kind,
                    ref=item.ref,
                    mutation=item.mutation,
                    outcome=EvidenceOutcome.MISSING,
                    detail="the runner did not execute this evidence item",
                )
            rows.append(row)
        observations.append(
            ScenarioObservation(
                scenario_id=scenario.id,
                observed_at=observed_at,
                deployed_revision=revision,
                scenarios_digest=digest,
                evidence=tuple(rows),
            )
        )
    return observations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--deployment-pid",
        type=int,
        default=None,
        help="PID of the isolated deployment, when verifying a live service",
    )
    parser.add_argument("--replays", type=Path, default=REPLAYS)
    parser.add_argument("--scenarios", type=Path, default=SCENARIOS)
    parser.add_argument("--acceptance-replays", type=Path, default=ACCEPTANCE_REPLAYS)
    parser.add_argument("--acceptance-report", type=Path, default=ACCEPTANCE_REPORT)
    parser.add_argument("--only", default=None, help="a comma-separated list of scenario ids")
    parser.add_argument(
        "--skip-mutations",
        action="store_true",
        help="record the mutation experiments as ungraded instead of running them",
    )
    parser.add_argument(
        "--run-docker",
        action="store_true",
        help="also run the pinned tests marked `docker`, which need PostgreSQL; without it "
        "the outbox retention scenarios are recorded unexercised",
    )
    parser.add_argument(
        "--allow-stale-deployment",
        action="store_true",
        help=(
            "observe even though the serving process predates the tree, recording the gap "
            "as a note instead of refusing; the evidence will describe the older code"
        ),
    )
    args = parser.parse_args()
    globals()["ACCEPTANCE_REPLAYS"] = args.acceptance_replays
    globals()["ACCEPTANCE_REPORT"] = args.acceptance_report

    # Refused before anything is observed, because the failure does not announce itself:
    # a process running code older than the tree answers every request competently, so the
    # batch completes and every record carries the revision of code the platform never ran.
    refusal = (
        refuse_stale_deployment(
            REPO_ROOT, pid=args.deployment_pid, allow=args.allow_stale_deployment, argv=sys.argv
        )
        if args.deployment_pid is not None
        else 0
    )
    if refusal:
        return refusal

    scenario_set = load_security_scenarios(args.scenarios)
    only = {item.strip() for item in args.only.split(",") if item.strip()} if args.only else set()
    unknown = only - {scenario.id for scenario in scenario_set.scenarios}
    if unknown:
        print(f"unknown scenario ids: {sorted(unknown)}", file=sys.stderr)
        return 3

    if args.replays.exists() and any(args.replays.glob("*.json")):
        print("Refusing to overwrite an existing evidence batch", file=sys.stderr)
        return 3
    observed_at = datetime.now(tz=UTC)
    revision = source_revision()
    collected = collect_evidence(
        scenario_set,
        only=only,
        with_mutations=not args.skip_mutations,
        run_docker=args.run_docker,
    )
    observations = build_observations(
        scenario_set, collected, revision=revision, observed_at=observed_at, only=only
    )

    if args.replays.exists() and any(args.replays.glob("*.json")):
        print("Refusing to overwrite an existing evidence batch", file=sys.stderr)
        return 3
    args.replays.mkdir(parents=True, exist_ok=True)
    for observation in observations:
        (args.replays / f"{observation.scenario_id}.json").write_text(
            json.dumps(
                observation.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True
            )
            + "\n",
            encoding="utf-8",
        )

    counts: dict[str, int] = {}
    for observation in observations:
        for item in observation.evidence:
            counts[item.outcome.value] = counts.get(item.outcome.value, 0) + 1
    print(
        json.dumps(
            {
                "recorded_scenarios": len(observations),
                "observed_at": observed_at.isoformat(),
                "deployed_revision": revision,
                "scenarios_digest": scenario_set_digest(scenario_set),
                "observations_digest": observation_digest(
                    [observation.model_dump(mode="python") for observation in observations]
                ),
                "evidence_outcomes": dict(sorted(counts.items())),
                # Recorded because it changes what an ``unexercised`` outcome means: with the
                # flag, the docker-marked evidence was asked to run and did not; without it,
                # nobody asked. The same word would otherwise cover both.
                "run_docker": bool(args.run_docker),
                "mutations": not args.skip_mutations,
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
