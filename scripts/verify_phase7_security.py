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
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from servicemind.evaluation.acceptance import case_set_digest, load_acceptance_cases
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

REPO_ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = REPO_ROOT / "evaluation" / "security" / "scenarios.v1.json"
REPLAYS = REPO_ROOT / "evaluation" / "security" / "replays"
REPORTS = REPO_ROOT / "evaluation" / "reports"
ACCEPTANCE_REPORT = REPORTS / "phase7_acceptance_latest.json"
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
    "XPASS": EvidenceOutcome.PASSED,
}


def source_revision() -> str:
    """The working tree this run graded, as ``<sha>`` or ``<sha>+dirty(N files)``.

    The scenario list is checked against the tree, not against a commit: the pinned
    behaviours live in the source, so a report naming only the commit would describe code
    the tests may never have imported.
    """
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    ).stdout.strip()
    if not head:
        return "unknown"
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    ).stdout.strip()
    return f"{head}+dirty({len(dirty.splitlines())} files)" if dirty else head


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
        completed = subprocess.run(
            [
                "uv",
                "run",
                "pytest",
                *ids,
                "-v",
                "--no-header",
                "--tb=short",
                "-p",
                "no:randomly",
                *(["--run-docker"] if run_docker else []),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=3600,
        )
        output = completed.stdout + completed.stderr
        # A scenario names a test, not a parametrisation of it, so one requested node id can
        # stand for several output lines (`::test_x[verified]`, `::test_x[unknown]`, ...).
        # Matching the printed id exactly would record every parametrised test as missing --
        # a false BLOCKED, which is safe in direction but wrong about the platform.
        seen: dict[str, list[tuple[str, EvidenceOutcome]]] = {node_id: [] for node_id in ids}
        for line in output.splitlines():
            match = _PYTEST_LINE.match(line.strip())
            if not match:
                continue
            printed = match["nodeid"]
            for node_id in ids:
                if printed == node_id or printed.startswith(f"{node_id}["):
                    seen[node_id].append((match["status"], _PYTEST_OUTCOMES[match["status"]]))
                    break

        for node_id in ids:
            ran = seen[node_id]
            if not ran:
                outcome = EvidenceOutcome.MISSING
                detail = _tail(output)
            else:
                outcome = _aggregate(ran)
                # Counted rather than summarised as one word: "4 passed, 1 skipped" and
                # "5 passed" are different observations of the same node id.
                detail = ", ".join(f"{count} {status.lower()}" for status, count in _tally(ran))
            recorded[node_id] = ObservedEvidence(
                kind=EvidenceKind.TEST, ref=node_id, outcome=outcome, detail=detail
            )
    return recorded


def _aggregate(ran: list[tuple[str, EvidenceOutcome]]) -> EvidenceOutcome:
    """One verdict for every parametrisation of a node id.

    A single red parametrisation makes the whole test red: the scenario claims a behaviour,
    and one case where it does not hold is enough to deny that. Skips only count when
    nothing passed -- a mixed run is reported PASSED with the skip visible in ``detail``
    rather than promoted to BLOCKED, because BLOCKED stops the release gate and a single
    skip-guarded parametrisation would then be able to do that on its own.
    """
    outcomes = [outcome for _, outcome in ran]
    if EvidenceOutcome.FAILED in outcomes:
        return EvidenceOutcome.FAILED
    if EvidenceOutcome.PASSED in outcomes:
        return EvidenceOutcome.PASSED
    if EvidenceOutcome.UNEXERCISED in outcomes:
        return EvidenceOutcome.UNEXERCISED
    return EvidenceOutcome.MISSING


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
    return recorded


def acceptance_verdicts() -> tuple[dict[str, str], str]:
    """Read the acceptance report's per-case verdicts, and refuse a stale one.

    Returns the verdicts and a note explaining any refusal. The binding is the case-list
    digest: an acceptance report graded against a different case list is a statement about
    a different set of cases, and borrowing its PASS would make this report cite evidence
    that was never about the thing it is claimed to be about.
    """
    if not ACCEPTANCE_REPORT.exists():
        return {}, f"no acceptance report at {ACCEPTANCE_REPORT.relative_to(REPO_ROOT)}"
    report = json.loads(ACCEPTANCE_REPORT.read_text(encoding="utf-8"))
    expected = case_set_digest(load_acceptance_cases(ACCEPTANCE_CASES))
    recorded = report.get("cases_digest")
    if recorded != expected:
        return (
            {},
            "the acceptance report is bound to a different case list "
            f"({recorded} != {expected}); rerun scripts/gate_phase7_acceptance.py first",
        )
    return {case["case_id"]: case["verdict"] for case in report.get("cases", [])}, ""


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
                        else EvidenceOutcome.BLOCKED
                        if recorded_verdict == "BLOCKED"
                        else EvidenceOutcome.FAILED
                    )
                    detail = f"acceptance case {item.ref} recorded {recorded_verdict}"
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
    args = parser.parse_args()

    scenario_set = load_security_scenarios(SCENARIOS)
    only = {item.strip() for item in args.only.split(",") if item.strip()} if args.only else set()
    unknown = only - {scenario.id for scenario in scenario_set.scenarios}
    if unknown:
        print(f"unknown scenario ids: {sorted(unknown)}", file=sys.stderr)
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

    REPLAYS.mkdir(parents=True, exist_ok=True)
    for observation in observations:
        (REPLAYS / f"{observation.scenario_id}.json").write_text(
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
