"""The four gates refuse a batch of observations that spans more than one revision.

Recorded here as a test rather than trusted to the wiring, because the failure is silent in
the direction that matters. Each gate already read ``deployed_revision`` off every
observation -- the security and load graders even collected the distinct set and printed it
as ``被测版本`` -- and then reached a verdict without ever asking whether the set had one
member. On 2026-09-30 the quality corpus held four, and the gate reported ``PASS`` over it.

The two halves are tested separately: the shared rule in ``evaluation/revisions.py``, and
then each gate's own use of it, because a rule that exists and is not called is the defect
being fixed.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from uuid import UUID

import pytest

from servicemind.evaluation.acceptance import CaseExecution, ObservedEnvironment
from servicemind.evaluation.load_grader import RunObservation
from servicemind.evaluation.quality_grader import CaseObservation
from servicemind.evaluation.revisions import recorded_revisions, revision_problems
from servicemind.evaluation.security_grader import ScenarioObservation

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
TENANT = UUID("22222222-2222-4222-8222-222222222222")
NOW = datetime(2026, 9, 30, tzinfo=UTC)
REVISION = "cf08ac8a7b731054e492ed81ba5f3164dc381863"
OTHER_REVISION = "7a6e6758d2e4966771546e57482e8b0a5d8b19dc+dirty(121 files)"


def load_gate(name: str) -> ModuleType:
    """The gate script as a module, so its check can be called the way ``main`` calls it."""
    spec = importlib.util.spec_from_file_location(
        f"_gate_{name}", SCRIPTS / f"gate_phase7_{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------------------
# The shared rule.
# --------------------------------------------------------------------------------------


def test_one_revision_is_not_a_problem() -> None:
    assert revision_problems([REVISION, REVISION, REVISION]) == []


def test_more_than_one_revision_is_refused_and_both_are_named() -> None:
    problems = revision_problems([REVISION, OTHER_REVISION, REVISION])
    assert len(problems) == 1
    # Both revisions, because "the batch is mixed" is not actionable on its own: the
    # operator has to know which sweeps to re-run, and which commit each belongs to.
    assert REVISION in problems[0]
    assert OTHER_REVISION in problems[0]
    assert "x2" in problems[0]


@pytest.mark.parametrize("absent", [None, ""])
def test_a_revision_nobody_recorded_is_a_problem(absent: str | None) -> None:
    """A missing revision is not a match, exactly as a missing ``cases_digest`` is not one."""
    problems = revision_problems([REVISION, absent])
    assert len(problems) == 1
    assert "do not record the source revision" in problems[0]
    # The empty string is what a caller who recorded ``""`` leaves behind. Sorted in with
    # the real revisions it would read as a revision that was simply never seen before.
    assert recorded_revisions([REVISION, absent]) == (REVISION, "<not recorded>")


def test_an_unrecorded_revision_is_not_reported_as_a_second_one() -> None:
    """Two ways to have no revision is one problem, not a mixed batch and a missing field."""
    problems = revision_problems([None, "", REVISION])
    assert len(problems) == 1
    assert "do not record the source revision" in problems[0]


def test_nothing_observed_is_not_a_provenance_problem() -> None:
    """An empty batch is the gates' own insufficiency rule, and must keep its exit code.

    Returning a problem here would turn "the batch was never run" -- exit 2, nothing was
    learned -- into "the batch is unusable" -- exit 3, the gate is misconfigured. Those
    send an operator to different places.
    """
    assert revision_problems([]) == []


def test_an_expected_revision_is_compared_when_given() -> None:
    assert revision_problems([REVISION], expect=REVISION) == []
    problems = revision_problems([OTHER_REVISION], expect=REVISION)
    assert len(problems) == 1
    assert OTHER_REVISION in problems[0] and REVISION in problems[0]


def test_a_mixed_batch_is_reported_even_when_one_of_them_is_expected() -> None:
    """Currency does not excuse heterogeneity: the mixture is still not a measurement."""
    problems = revision_problems([REVISION, OTHER_REVISION], expect=REVISION)
    assert len(problems) == 2


# --------------------------------------------------------------------------------------
# Each gate's use of it.
# --------------------------------------------------------------------------------------


def quality_observation(revision: str) -> CaseObservation:
    return CaseObservation(
        case_id="Q-001",
        observed_at=NOW,
        deployed_revision=revision,
        cases_digest="0" * 64,
    )


def acceptance_execution(revision: str) -> CaseExecution:
    return CaseExecution(
        case_id="ACC-01",
        environment=ObservedEnvironment(
            entitlement_verifier_configured=False,
            base_url="http://127.0.0.1:18080",
            tenant_id=TENANT,
            recorded_at=NOW,
            deployed_revision=revision,
        ),
    )


def security_observation(revision: str) -> ScenarioObservation:
    return ScenarioObservation(
        scenario_id="SEC-01",
        observed_at=NOW,
        deployed_revision=revision,
        scenarios_digest="0" * 64,
        evidence=(),
    )


def load_observation(revision: str) -> RunObservation:
    return RunObservation(
        tier="tier-1",
        concurrency=1,
        repeat_index=0,
        case_id="Q-001",
        plan_digest="0" * 64,
        workload_digest="0" * 64,
        deployed_revision=revision,
    )


@pytest.mark.parametrize(
    ("gate_name", "build", "check_name", "batch_attribute"),
    [
        ("quality", quality_observation, "check_batch_is_one_revision", "BATCH"),
        ("acceptance", acceptance_execution, "check_replays_are_one_revision", None),
        ("security", security_observation, "check_replays_are_one_revision", None),
        ("load", load_observation, "check_observations_are_one_revision", "BATCH"),
    ],
)
def test_each_gate_refuses_a_batch_from_two_revisions(
    gate_name: str,
    build,
    check_name: str,
    batch_attribute: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = load_gate(gate_name)
    if batch_attribute is not None:
        # The batch file names one revision for the whole sweep. Point it at nothing so the
        # refusal below is about the observations and not about whatever is on disk here.
        monkeypatch.setattr(gate, batch_attribute, tmp_path / "absent.json")
    check = getattr(gate, check_name)

    mixed = [build(REVISION), build(OTHER_REVISION)]
    with pytest.raises(gate.ConfigurationError) as refusal:
        check(mixed, expect=None)
    assert OTHER_REVISION in str(refusal.value)

    # And the same batch, from one revision, is not refused -- otherwise the check above
    # passes for a gate that refuses everything.
    same = [build(REVISION), build(REVISION)]
    assert check(same, expect=REVISION)["deployed_revisions"] == [REVISION]


@pytest.mark.parametrize(
    ("gate_name", "build", "check_name", "batch_attribute"),
    [
        ("quality", quality_observation, "check_batch_is_one_revision", "BATCH"),
        ("acceptance", acceptance_execution, "check_replays_are_one_revision", None),
        ("security", security_observation, "check_replays_are_one_revision", None),
        ("load", load_observation, "check_observations_are_one_revision", "BATCH"),
    ],
)
def test_each_gate_refuses_a_single_but_unexpected_revision(
    gate_name: str,
    build,
    check_name: str,
    batch_attribute: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One stale revision is homogeneous and still not the platform being vouched for."""
    gate = load_gate(gate_name)
    if batch_attribute is not None:
        monkeypatch.setattr(gate, batch_attribute, tmp_path / "absent.json")
    check = getattr(gate, check_name)

    with pytest.raises(gate.ConfigurationError) as refusal:
        check([build(OTHER_REVISION)], expect=REVISION)
    assert REVISION in str(refusal.value)


# --------------------------------------------------------------------------------------
# The wiring: a gate's ``main`` refuses, rather than a function that main might not call.
# --------------------------------------------------------------------------------------

#: Where each gate reads its evidence, and the batch file that names one revision for the
#: whole sweep -- absent for the two gates that keep no batch.
EVIDENCE_PATHS = {
    "quality": ("evaluation/quality/replays", "BATCH"),
    "acceptance": ("evaluation/acceptance/replays", None),
    "security": ("evaluation/security/replays", None),
    "load": ("evaluation/load/replays", "BATCH"),
}

COPIED_REVISION = "deadbeef+copied-by-the-test"


@pytest.mark.parametrize("gate_name", sorted(EVIDENCE_PATHS))
def test_a_gate_main_exits_three_on_a_replay_from_another_revision(
    gate_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The end of the wire, on replays with the shape the real ones have.

    The checks are tested above by calling them; this calls the gate the way CI does, over
    two real replays copied into a directory of their own -- one left as recorded and one
    stamped with a revision no deployment served. A gate that grew the check but never
    reached it would pass the tests above and fail this one. Two, not one, because a single
    foreign revision is *homogeneous* and only ``--expect-revision`` can refuse it.
    """
    relative, batch_attribute = EVIDENCE_PATHS[gate_name]
    # ``_``-prefixed files are the batch header, not an observation, and every gate's loader
    # skips them. Left in, this would copy the header as though it were a replay and stamp a
    # revision onto a file no gate reads -- which is exactly what happened for ``load``,
    # whose lowercase ``tier-`` names sort *after* ``_batch.json``. The quality gate escaped
    # it only because ``Q`` sorts before ``_``.
    originals = [
        path
        for path in sorted((REPO_ROOT / relative).glob("*.json"))
        if not path.name.startswith("_")
    ]
    if len(originals) < 2:
        pytest.skip(f"{relative} holds fewer than two replays to copy")

    replays = tmp_path / "replays"
    replays.mkdir()

    def where(payload: dict) -> dict:
        # The acceptance replay records the deployment under ``environment``; the other three
        # record the revision at the top level. Followed rather than assumed, because writing
        # to the wrong one would leave a replay the gate does not read and a test that passes
        # by not testing anything.
        return payload["environment"] if "environment" in payload else payload

    def write(source: Path, revision: str | None) -> None:
        # ``None`` leaves the replay's own revision in place, so the batch below is one real
        # revision beside one that no deployment ever served.
        payload = json.loads(source.read_text(encoding="utf-8"))
        if revision is not None:
            where(payload)["deployed_revision"] = revision
        (replays / source.name).write_text(json.dumps(payload), encoding="utf-8")

    gate = load_gate(gate_name)
    monkeypatch.setattr(gate, "REPLAYS", replays)
    if batch_attribute is not None:
        # The batch file names one revision for the whole sweep. Point it at a name no run
        # ever wrote, so the refusal below is about the observations and not about the batch
        # left on disk. It stays inside the repository because the gates name it by its
        # path relative to the root when they report it missing.
        monkeypatch.setattr(
            gate, batch_attribute, getattr(gate, batch_attribute).with_name("_absent-by-test.json")
        )

    recorded = where(json.loads(originals[0].read_text(encoding="utf-8")))["deployed_revision"]
    write(originals[0], None)
    write(originals[1], COPIED_REVISION)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            f"gate_phase7_{gate_name}.py",
            "--check",
            "--replay-only",
            "--format",
            "json",
            "--report",
            str(tmp_path / "report.md"),
        ],
    )

    assert gate.main() == gate.EXIT_CONFIGURATION
    refusal = capsys.readouterr().err
    assert COPIED_REVISION in refusal and recorded in refusal

    # The same replays from one revision get past provenance and into the grading rules,
    # which over two observations is not enough observed (2) rather than a configuration
    # error. Without this half, a gate that refused everything would pass.
    #
    # Stamped with ``recorded`` rather than left as recorded: leaving it asked the real
    # corpus to have its two lowest-sorting replays on one revision, which is a property of
    # the corpus and not of the gate. On 2026-10-01 a live probe re-recorded twelve quality
    # cases against the current build, ``Q-001`` became a different revision from ``Q-002``
    # and this half failed -- correctly reporting the corpus, while claiming to test the
    # gate. Homogeneity is what the second half needs, so it is constructed here.
    write(originals[1], recorded)
    monkeypatch.setattr(gate, "REPLAYS", replays)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            f"gate_phase7_{gate_name}.py",
            "--check",
            "--replay-only",
            "--format",
            "json",
            "--report",
            str(tmp_path / "report.md"),
            "--expect-revision",
            recorded,
        ],
    )
    capsys.readouterr()
    assert gate.main() != gate.EXIT_CONFIGURATION
