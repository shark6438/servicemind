"""The P7.6.5 security-scenario contract, judged on its own. No services, no cluster.

Same split as ``test_phase7_acceptance_contract``: the grader is a pure function, so the
things the online run relies on -- that a scenario without teeth is reported rather than
passed, that a declared-but-unrun piece of evidence cannot be spent as a pass, that a
mutation which removed the behaviour and was not noticed is a FAIL rather than a BLOCKED
-- are pinned here, in CI, on every pull request, against no infrastructure.

Two of the checks below are not about the grader at all. They are about the shipped
``scenarios.v1.json`` referring to things that exist: every mutation it names must be a
name in the script it names, and every pytest node id must be a test function in the file
it names. Those two are the failure mode this file exists for. A typo in either is
*silent* -- the scenario still loads, still validates, still appears in the report, and
grades BLOCKED forever with a finding that reads like a run that did not happen rather
than like a misspelling. Nothing else in the suite would catch it.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from servicemind.evaluation.security import (
    MINIMUM_SCENARIOS,
    SECURITY_CATEGORIES,
    EvidenceKind,
    ScenarioEvidence,
    SecurityScenario,
    SecurityScenarioSet,
    load_security_scenarios,
    observation_digest,
    scenario_set_digest,
)
from servicemind.evaluation.security_grader import (
    EvidenceOutcome,
    ObservedEvidence,
    ScenarioObservation,
    Verdict,
    grade,
    render_report,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIOS_PATH = REPO_ROOT / "evaluation" / "security" / "scenarios.v1.json"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from verify_phase7_security import (  # noqa: E402 -- needs the sys.path line above
    _MUTATION_OUTCOMES,
    _PYTEST_LINE,
    _PYTEST_OUTCOMES,
    _aggregate,
    _tally,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _set(scenarios: list[SecurityScenario]) -> SecurityScenarioSet:
    """A scenario set built without the shipped-set floor.

    ``SecurityScenarioSet`` refuses fewer than ``MINIMUM_SCENARIOS`` rows, which is a
    property of the list that ships, not of the grader. The grader's behaviour has to be
    checkable on two scenarios, so ``model_construct`` is used deliberately here: bypassing
    validation is the point, and the shipped file's own floor is asserted separately below.
    """
    return SecurityScenarioSet.model_construct(
        schema_version="phase7-security-v1",
        description="a set built for one grader test",
        scenarios=tuple(scenarios),
        not_covered=("nothing is covered here; this is a fixture",),
    )


def _scenario(scenario_id: str, evidence: list[ScenarioEvidence]) -> SecurityScenario:
    return SecurityScenario(
        id=scenario_id,
        category="tenant-isolation",
        title="a scenario",
        defends_against="something",
        teeth="a test would go red",
        evidence=tuple(evidence),
    )


def _observation(
    scenario_set: SecurityScenarioSet,
    scenario_id: str,
    evidence: list[ObservedEvidence],
    *,
    revision: str = "deadbeef",
    observed_at: datetime = NOW,
) -> ScenarioObservation:
    return ScenarioObservation(
        scenario_id=scenario_id,
        observed_at=observed_at,
        deployed_revision=revision,
        scenarios_digest=scenario_set_digest(scenario_set),
        evidence=tuple(evidence),
    )


def _test(ref: str, outcome: EvidenceOutcome) -> ObservedEvidence:
    return ObservedEvidence(kind=EvidenceKind.TEST, ref=ref, outcome=outcome, detail=outcome.value)


def _mutation(ref: str, name: str, outcome: EvidenceOutcome) -> ObservedEvidence:
    return ObservedEvidence(
        kind=EvidenceKind.MUTATION, ref=ref, mutation=name, outcome=outcome, detail=outcome.value
    )


# --- the shipped list -----------------------------------------------------------------


def test_the_shipped_scenario_set_clears_its_own_floor() -> None:
    scenario_set = load_security_scenarios(SCENARIOS_PATH)
    assert len(scenario_set.scenarios) >= MINIMUM_SCENARIOS
    assert scenario_set.not_covered, "a security table without its exclusions reads as complete"


def test_every_category_in_the_closed_set_is_used_by_a_scenario() -> None:
    """A category with no rows is a taxonomy nobody writes to.

    The closed set is what the report groups by, so an unused name is a heading that never
    appears -- harmless-looking, and it is also the shape a category takes when its last
    scenario is deleted rather than re-filed.
    """
    scenario_set = load_security_scenarios(SCENARIOS_PATH)
    used = {scenario.category for scenario in scenario_set.scenarios}
    assert used == set(SECURITY_CATEGORIES), sorted(set(SECURITY_CATEGORIES) - used)


def test_every_scenario_says_what_its_teeth_are_and_names_evidence() -> None:
    """Non-empty, which is all a machine can check about prose.

    A length threshold was tried here first and rejected: thirty scenarios say "该项只由通过
    的测试背书，没有对应的变异实验。" in twenty-two characters, which is the whole truth about
    them, and a floor on character count would have been paid for in filler. Whether a teeth
    note says something is a reviewer's question; the machine-checkable half -- that the
    evidence a scenario names actually exists -- is the two tests below.
    """
    scenario_set = load_security_scenarios(SCENARIOS_PATH)
    for scenario in scenario_set.scenarios:
        assert scenario.evidence, scenario.id
        assert scenario.teeth.strip(), scenario.id


def test_every_declared_mutation_is_a_name_in_the_script_it_names() -> None:
    """The check that catches a silent BLOCKED.

    ``verify_phase7_security`` reads mutation verdicts out of the experiment's own output by
    name. A name that is not in ``MUTATIONS`` never appears in that output, so the evidence
    is recorded as missing and the scenario grades BLOCKED -- which reads like a run that
    did not happen, not like a typo in the scenario file.
    """
    scenario_set = load_security_scenarios(SCENARIOS_PATH)
    declared: dict[str, set[str]] = {}
    for scenario in scenario_set.scenarios:
        for item in scenario.evidence:
            if item.kind is EvidenceKind.MUTATION:
                assert item.mutation is not None
                declared.setdefault(item.ref, set()).add(item.mutation)

    assert declared, "no scenario is backed by a mutation at all"
    for script, names in sorted(declared.items()):
        path = REPO_ROOT / script
        assert path.exists(), f"{script} is named by a scenario but is not on disk"
        spec = importlib.util.spec_from_file_location(f"_mutate_{path.stem}", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        actual = {row[0] for row in module.MUTATIONS}
        missing = sorted(names - actual)
        assert not missing, f"{script} has no mutation named {missing}"


def test_every_declared_test_node_id_names_a_test_that_exists() -> None:
    """File on disk, and the function defined in it.

    A node id is checked by reading the file rather than by collecting it: collecting the
    whole suite to validate a scenario list would put a multi-minute import cost on every
    pull request, and the failure this guards against is a typo, which a name match finds.
    """
    scenario_set = load_security_scenarios(SCENARIOS_PATH)
    for node_id in scenario_set.test_node_ids():
        assert "[" not in node_id, (
            f"{node_id}: a scenario names a test, not a parametrisation of it -- the runner "
            "aggregates every parametrisation under the bare node id"
        )
        path_text, _, function = node_id.partition("::")
        assert function, f"{node_id} is not a pytest node id"
        path = REPO_ROOT / path_text
        assert path.exists(), f"{node_id}: {path_text} is not on disk"
        source = path.read_text(encoding="utf-8")
        assert re.search(rf"^(async )?def {re.escape(function)}\(", source, re.MULTILINE), (
            f"{node_id}: no such test function in {path_text}"
        )


def test_every_declared_acceptance_case_exists_in_the_case_list() -> None:
    """An acceptance reference that is not a case would grade BLOCKED for the wrong reason."""
    scenario_set = load_security_scenarios(SCENARIOS_PATH)
    cases = json.loads((REPO_ROOT / "evaluation" / "acceptance" / "cases.v1.json").read_text())
    known = {case["id"] for case in cases["cases"]}
    for scenario in scenario_set.scenarios:
        for item in scenario.evidence:
            if item.kind is EvidenceKind.ACCEPTANCE:
                assert item.ref in known, f"{scenario.id} cites unknown acceptance case {item.ref}"


# --- the reader, and the two ways it went wrong ---------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "tests/servicemind/test_x.py::test_plain PASSED [ 10%]",
        "tests/servicemind/test_x.py::test_plain FAILED [ 10%]",
        "tests/servicemind/test_x.py::test_x[a short digest can never match] PASSED [ 20%]",
        (
            "tests/servicemind/test_x.py::test_x[1000-ééé-non-ASCII makes compare_digest raise] "
            "SKIPPED [ 30%]"
        ),
        "tests/servicemind/test_x.py::test_x[a PASSED b] PASSED [ 40%]",
    ],
)
def test_the_pytest_line_reader_reads_parametrised_ids(line: str) -> None:
    """Pinned because the first version of this regex did not.

    ``\\S+?`` before the status stops at the first space, and a parametrised node id is not
    one token -- several of the shipped refs expand to ids containing spaces. Every one of
    those lines went unmatched, and the tests were recorded as never having run.
    """
    match = _PYTEST_LINE.match(line)
    assert match is not None, line
    assert match["nodeid"].startswith("tests/servicemind/test_x.py::test_")
    assert match["status"] in _PYTEST_OUTCOMES


def test_the_pytest_line_reader_ignores_the_traceback_lines() -> None:
    for line in (
        "tests/servicemind/test_x.py:41: AssertionError",
        "FAILED tests/servicemind/test_x.py::test_y - AssertionError: nope",
        "============================== 5 passed in 0.09s ===============================",
    ):
        assert _PYTEST_LINE.match(line) is None, line


def test_a_parametrised_node_id_is_aggregated_rather_than_lost() -> None:
    """One red parametrisation is enough to deny the behaviour; skips only count alone."""
    passed = [("PASSED", EvidenceOutcome.PASSED)]
    skipped = [("SKIPPED", EvidenceOutcome.UNEXERCISED)]
    failed = [("FAILED", EvidenceOutcome.FAILED)]

    assert _aggregate(passed * 3) is EvidenceOutcome.PASSED
    assert _aggregate(passed + failed) is EvidenceOutcome.FAILED
    assert _aggregate(skipped * 2) is EvidenceOutcome.UNEXERCISED
    # Not promoted to BLOCKED: a single skip-guarded parametrisation would then be able to
    # stop the release gate on its own, and the skip is still visible in the detail line.
    assert _aggregate(passed + skipped) is EvidenceOutcome.PASSED
    assert dict(_tally(passed + skipped)) == {"PASSED": 1, "SKIPPED": 1}


def test_the_mutation_status_vocabulary_is_the_harness_own() -> None:
    """Read back verbatim, so a harness that renames a status must fail here rather than
    silently stop matching and turn every mutation-backed scenario BLOCKED."""
    harness = (REPO_ROOT / "scripts" / "mutation_harness.py").read_text(encoding="utf-8")
    for status in _MUTATION_OUTCOMES:
        assert f'"{status}"' in harness, f"{status!r} is no longer a status mutation_harness prints"
    # And the two answers that mean different things stay mapped to different outcomes.
    assert _MUTATION_OUTCOMES["RED (good)"] is EvidenceOutcome.PASSED
    assert _MUTATION_OUTCOMES["GREEN (BAD - no teeth)"] is EvidenceOutcome.UNDETECTED


# --- the grader -----------------------------------------------------------------------


def test_a_scenario_with_no_observation_is_blocked_and_not_dropped() -> None:
    scenario_set = _set([_scenario("S-1", [ScenarioEvidence(kind=EvidenceKind.TEST, ref="t")])])
    outcome = grade(scenario_set, [], generated_at=NOW)

    assert outcome.verdict is Verdict.BLOCKED
    assert [case.scenario_id for case in outcome.cases] == ["S-1"]
    assert outcome.counts() == {"PASS": 0, "FAIL": 0, "BLOCKED": 1}
    assert "no observation was recorded" in outcome.blockers[0]


def test_green_evidence_passes_and_a_test_only_scenario_is_marked_as_having_no_teeth() -> None:
    scenario_set = _set([_scenario("S-1", [ScenarioEvidence(kind=EvidenceKind.TEST, ref="t")])])
    observation = _observation(scenario_set, "S-1", [_test("t", EvidenceOutcome.PASSED)])
    outcome = grade(scenario_set, [observation], generated_at=NOW)

    assert outcome.verdict is Verdict.PASS
    assert outcome.cases[0].has_teeth is False, "a passing test is not a mutation-backed claim"
    assert outcome.counts()["PASS"] == 1


def test_a_mutation_that_removed_the_behaviour_unnoticed_is_a_fail() -> None:
    """The finding this whole half exists for: the behaviour is gone and the pin is asleep.

    Graded FAIL rather than BLOCKED on purpose. Nothing was left un-run; a control the
    platform is supposed to have can be deleted today without a single test going red, and
    that is a defect in the suite rather than a verification that did not happen.
    """
    evidence = ScenarioEvidence(
        kind=EvidenceKind.MUTATION, ref="scripts/mutate_x.py", mutation="M01 the guard is removed"
    )
    scenario_set = _set([_scenario("S-1", [evidence])])
    observation = _observation(
        scenario_set,
        "S-1",
        [_mutation("scripts/mutate_x.py", "M01 the guard is removed", EvidenceOutcome.UNDETECTED)],
    )
    outcome = grade(scenario_set, [observation], generated_at=NOW)

    assert outcome.verdict is Verdict.FAIL
    assert outcome.cases[0].has_teeth is False
    assert "has no teeth" in outcome.blockers[0]


def test_a_detected_mutation_is_what_earns_a_scenario_its_teeth() -> None:
    evidence = [
        ScenarioEvidence(kind=EvidenceKind.TEST, ref="t"),
        ScenarioEvidence(kind=EvidenceKind.MUTATION, ref="scripts/mutate_x.py", mutation="M01"),
    ]
    scenario_set = _set([_scenario("S-1", evidence)])
    observation = _observation(
        scenario_set,
        "S-1",
        [
            _test("t", EvidenceOutcome.PASSED),
            _mutation("scripts/mutate_x.py", "M01", EvidenceOutcome.PASSED),
        ],
    )
    outcome = grade(scenario_set, [observation], generated_at=NOW)

    assert outcome.verdict is Verdict.PASS
    assert outcome.cases[0].has_teeth is True


def test_evidence_that_was_declared_and_never_run_blocks_the_scenario() -> None:
    """A skipped test is not a pass, and not a scenario that was absent from the table."""
    evidence = [
        ScenarioEvidence(kind=EvidenceKind.TEST, ref="ran"),
        ScenarioEvidence(kind=EvidenceKind.TEST, ref="never-ran"),
    ]
    scenario_set = _set([_scenario("S-1", evidence)])
    observation = _observation(scenario_set, "S-1", [_test("ran", EvidenceOutcome.PASSED)])
    outcome = grade(scenario_set, [observation], generated_at=NOW)

    assert outcome.verdict is Verdict.BLOCKED
    assert any("was not run" in finding for finding in outcome.blockers)


def test_a_skipped_test_is_unexercised_rather_than_passed() -> None:
    scenario_set = _set([_scenario("S-1", [ScenarioEvidence(kind=EvidenceKind.TEST, ref="t")])])
    observation = _observation(scenario_set, "S-1", [_test("t", EvidenceOutcome.UNEXERCISED)])
    outcome = grade(scenario_set, [observation], generated_at=NOW)

    assert outcome.verdict is Verdict.BLOCKED
    assert "ran nothing but skips" in outcome.blockers[0]


def test_evidence_that_was_run_but_not_declared_fails_the_scenario() -> None:
    """The set is the claim; a run that did more than the claim is a set that is out of date."""
    scenario_set = _set([_scenario("S-1", [ScenarioEvidence(kind=EvidenceKind.TEST, ref="t")])])
    observation = _observation(
        scenario_set,
        "S-1",
        [_test("t", EvidenceOutcome.PASSED), _test("undeclared", EvidenceOutcome.PASSED)],
    )
    outcome = grade(scenario_set, [observation], generated_at=NOW)

    assert outcome.verdict is Verdict.FAIL
    assert any("does not declare" in finding for finding in outcome.blockers)


def test_a_missing_script_and_a_failed_script_are_graded_apart() -> None:
    """``missing`` is a run that did not happen; ``failed`` is a script that said no."""
    evidence = ScenarioEvidence(
        kind=EvidenceKind.MUTATION, ref="scripts/mutate_x.py", mutation="M01"
    )
    scenario_set = _set([_scenario("S-1", [evidence])])
    for outcome_, verdict in (
        (EvidenceOutcome.MISSING, Verdict.BLOCKED),
        (EvidenceOutcome.UNRUNNABLE, Verdict.BLOCKED),
        (EvidenceOutcome.FAILED, Verdict.FAIL),
    ):
        observation = _observation(
            scenario_set, "S-1", [_mutation("scripts/mutate_x.py", "M01", outcome_)]
        )
        assert grade(scenario_set, [observation], generated_at=NOW).verdict is verdict


def test_the_report_carries_the_exclusions_next_to_the_green_rows() -> None:
    """A table with the caveats in another file is a table that reads as complete."""
    scenario_set = _set([_scenario("S-1", [ScenarioEvidence(kind=EvidenceKind.TEST, ref="t")])])
    observation = _observation(scenario_set, "S-1", [_test("t", EvidenceOutcome.PASSED)])
    report = render_report(grade(scenario_set, [observation], generated_at=NOW))

    assert "**不**覆盖的内容" in report
    assert scenario_set.not_covered[0] in report
    assert "无变异背书的场景" in report
    assert "S-1" in report


def _two_green_scenarios() -> SecurityScenarioSet:
    return _set(
        [
            _scenario("S-1", [ScenarioEvidence(kind=EvidenceKind.TEST, ref="t1")]),
            _scenario("S-2", [ScenarioEvidence(kind=EvidenceKind.TEST, ref="t2")]),
        ]
    )


def test_a_batch_recorded_across_two_revisions_blocks_instead_of_passing() -> None:
    """The one thing a reader cannot recover from the table.

    Re-running a few scenarios with ``--only`` after an edit leaves the rest stamped with the
    older revision, and both kinds of row look identical in the report. Grading that PASS
    would publish "the platform is sound" about a build no single test ever ran against.
    """
    scenario_set = _two_green_scenarios()
    outcome = grade(
        scenario_set,
        [
            _observation(
                scenario_set, "S-1", [_test("t1", EvidenceOutcome.PASSED)], revision="aaaa"
            ),
            _observation(
                scenario_set, "S-2", [_test("t2", EvidenceOutcome.PASSED)], revision="bbbb"
            ),
        ],
        generated_at=NOW,
    )

    assert outcome.verdict is Verdict.BLOCKED
    assert outcome.deployed_revisions == ("aaaa", "bbbb")
    assert any("more than one revision" in blocker for blocker in outcome.blockers)


def test_one_revision_and_a_missing_scenario_is_still_only_blocked_by_the_gap() -> None:
    """The new rule must not fire on the ordinary single-build batch."""
    scenario_set = _two_green_scenarios()
    outcome = grade(
        scenario_set,
        [_observation(scenario_set, "S-1", [_test("t1", EvidenceOutcome.PASSED)])],
        generated_at=NOW,
    )

    assert outcome.verdict is Verdict.BLOCKED
    assert outcome.deployed_revisions == ("deadbeef",)
    assert not any("more than one revision" in blocker for blocker in outcome.blockers)


def test_a_failure_is_not_downgraded_by_the_revision_rule() -> None:
    """FAIL outranks BLOCKED, and a mixed batch that also failed is still a failure.

    Downgrading here would let an edit-and-rerun turn a red scenario into "not enough
    observation", which is the direction that loses the finding.
    """
    scenario_set = _two_green_scenarios()
    outcome = grade(
        scenario_set,
        [
            _observation(
                scenario_set, "S-1", [_test("t1", EvidenceOutcome.FAILED)], revision="aaaa"
            ),
            _observation(
                scenario_set, "S-2", [_test("t2", EvidenceOutcome.PASSED)], revision="bbbb"
            ),
        ],
        generated_at=NOW,
    )

    assert outcome.verdict is Verdict.FAIL
    assert outcome.deployed_revisions == ("aaaa", "bbbb")


def test_the_report_names_the_build_and_the_window_it_describes() -> None:
    """``generated_at`` is the gate's clock. A report showing only that dates a stale batch today."""
    scenario_set = _two_green_scenarios()
    first = datetime(2026, 1, 1, tzinfo=UTC)
    second = datetime(2026, 1, 2, tzinfo=UTC)
    outcome = grade(
        scenario_set,
        [
            _observation(
                scenario_set, "S-1", [_test("t1", EvidenceOutcome.PASSED)], observed_at=first
            ),
            _observation(
                scenario_set, "S-2", [_test("t2", EvidenceOutcome.PASSED)], observed_at=second
            ),
        ],
        generated_at=NOW,
    )
    report = render_report(outcome)

    assert "deadbeef" in report
    assert first.isoformat() in report
    assert second.isoformat() in report
    assert "生成时间" in report


def test_a_report_with_no_observations_says_so_rather_than_leaving_the_line_out() -> None:
    """An absent "被测版本" line reads as "not applicable", which is not what happened."""
    outcome = grade(_two_green_scenarios(), [], generated_at=NOW)
    report = render_report(outcome)

    assert outcome.deployed_revisions == ()
    assert outcome.observed_from is None
    assert "**未记录**" in report


def test_the_digest_tracks_what_the_list_says() -> None:
    """The gate refuses a replay recorded against a different list, so this has to move."""
    one = _set([_scenario("S-1", [ScenarioEvidence(kind=EvidenceKind.TEST, ref="t")])])
    other = _set([_scenario("S-2", [ScenarioEvidence(kind=EvidenceKind.TEST, ref="t")])])
    assert scenario_set_digest(one) != scenario_set_digest(other)
    assert scenario_set_digest(one) == scenario_set_digest(
        _set([_scenario("S-1", [ScenarioEvidence(kind=EvidenceKind.TEST, ref="t")])])
    )


def test_the_observation_digest_does_not_depend_on_who_is_asking() -> None:
    """The runner and the gate hold the same batch in different orders, and both digest it.

    The runner stamps the summary it prints; the gate stamps the report it writes; a reader
    comparing the two is doing the obvious thing. Digesting the sequence as handed made those
    two numbers disagree over one batch, which reads as a corrupted run and is not one.
    """
    scenario_set = _two_green_scenarios()
    first = _observation(scenario_set, "S-1", [_test("t1", EvidenceOutcome.PASSED)])
    second = _observation(scenario_set, "S-2", [_test("t2", EvidenceOutcome.PASSED)])
    rows = [observation.model_dump(mode="python") for observation in (first, second)]

    assert observation_digest(rows) == observation_digest(list(reversed(rows)))
    assert observation_digest([]) == observation_digest([])
    # The sorted path is keyed on the id, not on the position, so a batch and its shuffle
    # cannot collide with a *different* batch that happens to share a prefix.
    assert observation_digest(rows) != observation_digest(rows[:1])


# --- what the model refuses to hold -----------------------------------------------------


def test_a_scenario_without_evidence_is_rejected_rather_than_reported_as_covered() -> None:
    with pytest.raises(ValidationError):
        SecurityScenario(
            id="S-1",
            category="tenant-isolation",
            title="a scenario",
            defends_against="something",
            teeth="a test would go red",
            evidence=(),
        )


def test_a_mutation_evidence_item_must_name_its_mutation() -> None:
    with pytest.raises(ValidationError):
        ScenarioEvidence(kind=EvidenceKind.MUTATION, ref="scripts/mutate_x.py")
    with pytest.raises(ValidationError):
        ScenarioEvidence(kind=EvidenceKind.TEST, ref="t", mutation="M01")


def test_an_unknown_category_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SecurityScenario(
            id="S-1",
            category="whatever-i-feel-like",
            title="a scenario",
            defends_against="something",
            teeth="a test would go red",
            evidence=(ScenarioEvidence(kind=EvidenceKind.TEST, ref="t"),),
        )


def test_the_shipped_set_rejects_a_duplicate_id_and_a_set_below_the_floor() -> None:
    scenario = _scenario("S-1", [ScenarioEvidence(kind=EvidenceKind.TEST, ref="t")])
    with pytest.raises(ValidationError, match="duplicate scenario ids"):
        SecurityScenarioSet(
            schema_version="phase7-security-v1",
            description="d",
            scenarios=(scenario, scenario),
            not_covered=("nothing",),
        )
    with pytest.raises(ValidationError, match="below the"):
        SecurityScenarioSet(
            schema_version="phase7-security-v1",
            description="d",
            scenarios=(scenario,),
            not_covered=("nothing",),
        )
