"""The driver must not read a legitimate abstention as a permission finding.

``observe-pending`` is the step that establishes the approval boundary every later
approval, revocation and tamper step addresses, and the case list declares it as if the
run were certain to arrive there. It is not certain: a Reviewer that abstains is answering
correctly -- the evidence did not support the claims -- and abstention is terminal by
design, so the run rests having derived no action. Every step after it then has nothing to
approve, nothing to revoke and nothing to tamper with.

Two wrong readings are available and both are worse than saying so. Judging the case on
such a run fails the platform for the Reviewer's refusal. Recording the missing steps as
optional passes reports a permission check that never ran. The driver submits again
instead, and these tests pin that: the boundary is asked for rather than assumed, the
submissions that did not reach it are visible in the replay rather than silently retried,
and a run that *did* derive an action is never second-guessed -- an approval addressed to
an action nobody can see is not a retry, it is a different observation.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "verify_phase7_acceptance_live", ROOT / "scripts/verify_phase7_acceptance_live.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
# Registered before execution because the script decorates with ``@dataclass`` at module
# level, and ``dataclasses`` resolves a class's module through ``sys.modules`` -- a module
# that is still being executed under another name reads back as ``None`` and the decorator
# raises rather than building the class.
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

CASE_SET = MODULE.AcceptanceCaseSet.model_validate(
    json.loads(MODULE.CASES.read_text(encoding="utf-8"))
)
CASE = next(case for case in CASE_SET.cases if case.id == "ACC-21")
STEP = next(step for step in CASE.steps if step.id == "observe-pending")

#: The case runs against a symbolic ticket the bootstrap resolved; the driver only ever
#: uses it to name the ticket in a submission, so a stand-in id is enough here.
TICKETS = {
    reference: 25
    for reference in {CASE.ticket_ref, STEP.ticket_ref, *(step.ticket_ref for step in CASE.steps)}
    if reference is not None
}

#: A derived action, in the shape ``GET /runs/{id}`` carries one.
INTENT: dict[str, Any] = {
    "action_hash": "a" * 64,
    "status": "proposed",
    "action_type": "append_ticket_followup",
    "target_id": 25,
    "arguments": {"content": "…"},
    "evidence_refs": ["ev-1"],
}


def _payload(run_id: UUID, status: str, intent: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"id": str(run_id), "status": status}
    if intent is not None:
        payload["action_intent"] = intent
    return payload


class _Stack:
    """A stack whose runs rest wherever the test says they do, in submission order."""

    timeout_scale = 1.0

    def __init__(self, rests: list[tuple[str, dict[str, Any] | None]]) -> None:
        self._rests = list(rests)
        self.submitted: list[UUID] = []
        #: Who each submission was made as, in the same order as ``submitted``. A run
        #: carries its requester, so the identity is part of what was submitted.
        self.subjects: list[str] = []
        self.settled_as: list[str] = []

    async def create_run(
        self, subject: str, *, ticket_id: int, goal: str, request_write: bool
    ) -> httpx.Response:
        run_id = uuid4()
        self.submitted.append(run_id)
        self.subjects.append(subject)
        return httpx.Response(202, json=_payload(run_id, "pending"))

    async def settle(self, subject: str, run_id: UUID, deadline: float) -> httpx.Response:
        self.settled_as.append(subject)
        status, intent = self._rests.pop(0)
        return httpx.Response(200, json=_payload(run_id, status, intent))


def _observed_run(
    stack: _Stack, status: str, intent: dict[str, Any] | None = None
) -> tuple[MODULE.CaseRun, UUID]:
    """A case run exactly as ``read_run`` leaves it: one run observed, then this is called.

    The status is not decoration. ``seek_approval_boundary`` reads it to decide whether
    there is anything to ask for, so a test that called it on an unobserved run would be
    exercising a state the driver never reaches.
    """
    case_run = MODULE.CaseRun(CASE, stack, TICKETS)
    run_id = uuid4()
    case_run.runs[STEP.id] = run_id
    case_run.run_id = run_id
    case_run.observe_payload(_payload(run_id, status, intent))
    return case_run, run_id


@pytest.mark.asyncio
async def test_a_run_that_derived_no_action_is_submitted_again() -> None:
    stack = _Stack([("waiting_approval", INTENT)])
    case_run, abandoned = _observed_run(stack, "succeeded", None)

    await case_run.seek_approval_boundary(STEP, CASE.subject)

    assert len(stack.submitted) == 1, "the abstained run should have been submitted again"
    assert case_run.run_id == stack.submitted[0]
    assert case_run.run_id != abandoned, "the case must address the run that reached"
    assert case_run.status == "waiting_approval"
    assert case_run.intent is not None
    assert case_run.intent.action_hash == INTENT["action_hash"]
    assert case_run.errors == []
    # The abandoned submission is a step in its own right: a retry that leaves no trace in
    # the replay is indistinguishable from a case that reached the boundary first try.
    assert [observation.id for observation in case_run.steps] == [f"{STEP.id}~attempt-1"]
    assert case_run.boundary_attempts == [str(stack.submitted[0])]


@pytest.mark.asyncio
async def test_a_boundary_retry_is_submitted_as_the_requester_not_the_case_s_approver() -> None:
    """The retry must not quietly hand the run to somebody else.

    A case whose submission names an ``as_subject`` runs as one identity and submits a
    run that belongs to another. Every later step -- revoke this person's role, revoke
    that entity, disable the account -- is about the requester, and the case reaches the
    approval boundary to have *their* action approved. A retry submitted in the approver's
    name still reaches the boundary, because approving runs is what the approver does, and
    the case then runs end to end against a run it never meant to create. On ACC-20
    (2026-09-23) that was not hypothetical: the role was revoked from the requester, the
    approval was applied to the approver's own run, and the ticket was written to.
    """
    stack = _Stack([("waiting_approval", INTENT)])
    case_run, abandoned = _observed_run(stack, "succeeded", None)
    case_run.submitted_as = "globex-analyst-g3"

    await case_run.seek_approval_boundary(STEP, CASE.subject)

    assert stack.subjects == ["globex-analyst-g3"], (
        "the resubmission must be made as the requester of the run it replaces"
    )
    assert stack.settled_as == ["globex-analyst-g3"]
    assert case_run.run_id == stack.submitted[0] != abandoned
    assert case_run.errors == []


@pytest.mark.asyncio
async def test_a_retry_outside_a_submission_still_uses_the_subject_it_was_given() -> None:
    """No submission was recorded, so there is no requester to preserve."""
    stack = _Stack([("waiting_approval", INTENT)])
    case_run, _ = _observed_run(stack, "succeeded", None)

    await case_run.seek_approval_boundary(STEP, CASE.subject)

    assert stack.subjects == [CASE.subject]


@pytest.mark.asyncio
async def test_a_case_that_never_reaches_the_boundary_says_so_instead_of_passing() -> None:
    stack = _Stack([("succeeded", None)] * (MODULE.APPROVAL_BOUNDARY_ATTEMPTS - 1))
    case_run, _ = _observed_run(stack, "succeeded", None)

    await case_run.seek_approval_boundary(STEP, CASE.subject)

    # The original submission plus the retries: the budget counts submissions spent, not
    # retries taken.
    assert len(stack.submitted) == MODULE.APPROVAL_BOUNDARY_ATTEMPTS - 1
    assert case_run.intent is None
    # An unobservable case is reported as unobservable. Recording it as a pass would claim
    # a permission check was refused by a boundary that was never reached.
    assert len(case_run.errors) == 1
    assert "never reached an approval boundary" in case_run.errors[0]
    assert repr("succeeded") in case_run.errors[0]


@pytest.mark.asyncio
async def test_the_boundary_is_sought_from_the_step_that_establishes_it() -> None:
    """The wiring, not just the method: an uncalled fix is an unfixed case.

    ``read_run`` reads the run first and records that reading, then asks for the boundary.
    Both orders matter and only one of them is visible in a test that calls the method
    directly -- an abstained run that is never re-read looks, from the outside, exactly
    like a case that reached the boundary on its first submission.
    """
    stack = _Stack([("succeeded", None), ("waiting_approval", INTENT)])
    case_run = MODULE.CaseRun(CASE, stack, TICKETS)
    case_run.run_id = uuid4()

    await case_run.read_run(STEP, CASE.subject)

    assert len(stack.submitted) == 1
    assert case_run.status == "waiting_approval"
    assert [observation.id for observation in case_run.steps] == [
        STEP.id,
        f"{STEP.id}~attempt-1",
    ]


@pytest.mark.asyncio
async def test_a_run_that_derived_an_action_is_not_submitted_again() -> None:
    stack = _Stack([])
    case_run, _ = _observed_run(stack, "waiting_review", INTENT)

    await case_run.seek_approval_boundary(STEP, CASE.subject)

    # There is a real action in flight. Whatever the case makes of a run resting there,
    # it is a finding about *that* run, and starting a second one would answer a question
    # the case did not ask.
    assert stack.submitted == []
    assert case_run.errors == []
    assert case_run.intent is not None


def test_a_platform_that_moved_mid_sweep_is_reported() -> None:
    """Both halves of the identity, because either can move on its own.

    A restart onto the same tree changes the process but not the revision; a source edit
    without a restart changes the revision but not the process. Whichever moved, the
    replays on the two sides describe different platforms, and nothing in a replay says so.
    """
    same = ("Fri 2026-10-02 23:07:26 CST", "b385df7c+patch(47de4c33ae95)")
    assert MODULE.mid_sweep_change(same, same) is None

    restarted = ("Sat 2026-10-03 00:46:24 CST", same[1])
    changed_tree = (same[0], "b385df7c+patch(b066c2f1a5aa)")
    for after in (restarted, changed_tree):
        message = MODULE.mid_sweep_change(same, after)
        assert message is not None
        assert same[0] in message and after[0] in message


def test_a_restart_the_sweep_itself_performed_is_not_drift() -> None:
    """The exemption is an exact match against what the sweep saw at its own restart.

    One case restarts the unit on purpose -- it points the verifier at a dead port and
    restarts to make that take effect, then restarts again to undo it. Aborting on that
    restart makes the corpus unrunnable, which is what happened on 2026-10-03, when the
    guard stopped the sweep at the case right after it. But "a restart happened somewhere
    in this sweep's past" is not the claim being made, and accepting anything once the
    sweep has restarted once would re-open the hole it was built to close. The claim is
    narrower: the identity observed now is the one this sweep saw at its own last restart.
    """
    before = ("Sat 2026-10-03 00:46:24 CST", "b385df7c+patch(502cd954496f)")
    ours = ("Sat 2026-10-03 13:11:48 CST", before[1])
    afterwards = ("Sat 2026-10-03 13:20:00 CST", before[1])
    changed_tree = (ours[0], "b385df7c+patch(deadbeef1234)")

    assert MODULE.rebaselined_after_restart(before, ours, declared=ours) is True
    assert MODULE.rebaselined_after_restart(before, ours, declared=None) is False, (
        "nothing in this sweep restarted the unit, so the change is not ours to explain"
    )
    assert MODULE.rebaselined_after_restart(before, afterwards, declared=ours) is False, (
        "our restart happened, but the unit moved again after it -- that second move is drift"
    )
    assert MODULE.rebaselined_after_restart(before, changed_tree, declared=ours) is False, (
        "the tree is not something a restart explains, so it is never absorbed"
    )
    assert MODULE.rebaselined_after_restart(before, before, declared=before) is False, (
        "nothing moved, so there is nothing to re-baseline"
    )


def test_the_exemption_is_asked_for_rather_than_assumed() -> None:
    """The loop must consult the identity captured at its own restart.

    Structural, because the wrong version of this line is the permissive one: a loop that
    re-baselines on any change once a restart has happened anywhere in its past reads
    identically at the call site and passes every behavioural test that only ever produces
    a change the sweep caused.
    """
    source = (
        Path(__file__).resolve().parents[2] / "scripts" / "verify_phase7_acceptance_live.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    def exemption_calls(node: ast.AST) -> list[ast.Call]:
        return [
            child
            for child in ast.walk(node)
            if isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "rebaselined_after_restart"
        ]

    loops = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.For) and "run_case" in ast.dump(node)
    ]
    assert len(loops) == 1
    guarded = exemption_calls(loops[0])
    assert guarded, "the per-case loop must decide whether the change was its own"
    # The value, not merely the argument name: ``declared=None`` is the same keyword
    # spelled so that the loop never re-baselines on anything, and a check of the name
    # alone passes it. This is asserted as source text rather than as a node shape,
    # because the shape has more ways to be right than it has ways to be wrong.
    given = [
        ast.unparse(keyword.value)
        for call in guarded
        for keyword in call.keywords
        if keyword.arg == "declared"
    ]
    assert given, "the decision must be given an identity to compare against"
    assert all(value == "stack.identity_at_last_restart" for value in given), (
        "the decision must be given the identity captured at the sweep's own restart; "
        f"found {given}"
    )


def test_the_sweep_asks_that_question_around_every_case() -> None:
    """The question is asked per case and once more at the end, asserted structurally.

    Reading the source rather than the call, because the defect this guards is an absent
    call: ``refuse_stale_deployment`` checks the same pairing once, before the first case,
    and a sweep that only ever asks once is exactly the sweep that cannot notice a restart
    four cases in -- which is what happened on 2026-10-03.
    """
    source = (
        Path(__file__).resolve().parents[2] / "scripts" / "verify_phase7_acceptance_live.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    def calls(node: ast.AST) -> bool:
        return any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "mid_sweep_change"
            for child in ast.walk(node)
        )

    case_loops = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.For) and calls(node) and "run_case" in ast.dump(node)
    ]
    assert len(case_loops) == 1, (
        "the per-case loop must ask whether the platform moved before running the case; "
        "a sweep that asks only once cannot notice a restart part-way through"
    )

    finals = [
        node.finalbody for node in ast.walk(tree) if isinstance(node, ast.Try) and node.finalbody
    ]
    assert any(any(calls(statement) for statement in block) for block in finals), (
        "the last case can straddle the change as easily as any other, and a batch that "
        "ended just after a restart has no next case to notice it"
    )
