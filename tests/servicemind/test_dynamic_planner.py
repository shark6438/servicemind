from datetime import UTC, datetime, timedelta

import pytest

from servicemind.domain.review import ReviewDecision, ReviewResult, RiskLevel
from servicemind.domain.supervisor import (
    PlanProposal,
    PlanRevisionProposal,
    PlanTaskProposal,
)
from servicemind.domain.task import AgentName, Budget, Task, TaskPlan, TaskStatus
from servicemind.orchestration.dynamic_planner import DynamicPlanner


class FakeRunnable:
    def __init__(self, result) -> None:
        self.result = result

    async def ainvoke(self, messages):
        return self.result


def completed_plan() -> TaskPlan:
    due = datetime.now(UTC) + timedelta(hours=1)
    budget = Budget(deadline=due)
    tasks = [
        Task(
            task_id="T1",
            agent=AgentName.DATA,
            task_type="get_ticket",
            status=TaskStatus.SUCCESS,
            output_ref="ev-1",
            deadline=due,
        ),
        Task(
            task_id="T2",
            agent=AgentName.ANALYSIS,
            task_type="analyze_ticket",
            depends_on=["T1"],
            status=TaskStatus.SUCCESS,
            output_ref="analysis-1",
            deadline=due,
        ),
        Task(
            task_id="T3",
            agent=AgentName.REVIEWER,
            task_type="review_analysis",
            depends_on=["T2"],
            status=TaskStatus.SUCCESS,
            output_ref="review-1",
            deadline=due,
        ),
    ]
    return TaskPlan(
        goal="Analyze ticket",
        tasks=tasks,
        max_parallel=2,
        max_steps=budget.max_steps,
        max_replans=budget.max_replans,
        deadline=due,
        budget=budget,
    )


def revision(*, mutate_completed: bool = False) -> PlanRevisionProposal:
    tasks = [
        PlanTaskProposal(
            task_id="T4",
            agent=AgentName.KNOWLEDGE,
            task_type="retrieve_more_knowledge",
            objective="Retrieve the missing runbook",
            depends_on=["T1"],
        ),
        PlanTaskProposal(
            task_id="T5",
            agent=AgentName.ANALYSIS,
            task_type="analyze_ticket",
            objective="Reanalyze with new evidence",
            depends_on=["T1", "T4"],
        ),
        PlanTaskProposal(
            task_id="T6",
            agent=AgentName.REVIEWER,
            task_type="review_analysis",
            objective="Review the revised analysis",
            depends_on=["T5"],
        ),
    ]
    if mutate_completed:
        tasks.append(
            PlanTaskProposal(
                task_id="T1",
                agent=AgentName.KNOWLEDGE,
                task_type="retrieve_knowledge",
                objective="Illegally replace completed task",
            )
        )
    return PlanRevisionProposal(
        rationale_summary="Add missing knowledge then reanalyze and review.",
        preserved_task_ids=["T1", "T2", "T3"],
        tasks=tasks,
        max_parallel=2,
    )


@pytest.mark.asyncio
async def test_replan_preserves_completed_tasks_and_adds_new_dag(monkeypatch) -> None:
    from servicemind.orchestration import dynamic_planner

    monkeypatch.setattr(dynamic_planner, "get_model", lambda _: object())
    monkeypatch.setattr(
        dynamic_planner,
        "structured_output",
        lambda model, schema: FakeRunnable(revision()),
    )
    review = ReviewResult(
        decision=ReviewDecision.RETRIEVE_MORE,
        missing_evidence=["runbook"],
        risk_level=RiskLevel.MEDIUM,
        feedback="Retrieve a runbook.",
    )
    plan = await DynamicPlanner().revise_plan(
        previous=completed_plan(),
        review=review,
        ticket_id=2,
        request_write=False,
    )
    assert [task.status for task in plan.tasks[:3]] == [TaskStatus.SUCCESS] * 3
    assert [task.agent for task in plan.tasks[3:]] == [
        AgentName.KNOWLEDGE,
        AgentName.ANALYSIS,
        AgentName.REVIEWER,
    ]


@pytest.mark.asyncio
async def test_replan_cannot_mutate_completed_task_identity(monkeypatch) -> None:
    from servicemind.orchestration import dynamic_planner

    monkeypatch.setattr(dynamic_planner, "get_model", lambda _: object())
    monkeypatch.setattr(
        dynamic_planner,
        "structured_output",
        lambda model, schema: FakeRunnable(revision(mutate_completed=True)),
    )
    review = ReviewResult(
        decision=ReviewDecision.REPLAN,
        risk_level=RiskLevel.MEDIUM,
        feedback="Replan.",
    )
    with pytest.raises(ValueError, match="identity"):
        await DynamicPlanner().revise_plan(
            previous=completed_plan(),
            review=review,
            ticket_id=2,
            request_write=False,
        )


# ---------------------------------------------------------------------------
# Orchestration audit regressions (2026-09-08): the planner must fail closed on
# evidence-bearing plans, must not re-arm the run deadline on revision, and must
# honor RETRIEVE_MORE with a Knowledge task.
# ---------------------------------------------------------------------------


def proposal_of(*agents: AgentName) -> PlanProposal:
    task_type = {
        AgentName.DATA: "get_ticket",
        AgentName.KNOWLEDGE: "retrieve_knowledge",
        AgentName.ANALYSIS: "analyze_ticket",
        AgentName.REVIEWER: "review_analysis",
        AgentName.ACTION: "propose_followup",
    }
    tasks = []
    previous: list[str] = []
    for index, agent in enumerate(agents, start=1):
        task_id = f"T{index}"
        depends_on = list(previous) if agent is AgentName.ANALYSIS else []
        if agent is AgentName.REVIEWER:
            depends_on = [f"T{index - 1}"]
        tasks.append(
            PlanTaskProposal(
                task_id=task_id,
                agent=agent,
                task_type=task_type[agent],
                objective=f"Run {agent.value}",
                depends_on=depends_on,
            )
        )
        previous.append(task_id)
    return PlanProposal(
        rationale_summary="Deterministic fixture.",
        tasks=tasks,
        max_parallel=2,
    )


def planner() -> DynamicPlanner:
    return DynamicPlanner()


def test_compile_rejects_evidence_plan_without_reviewer() -> None:
    # DATA + ANALYSIS but no REVIEWER: the run would finalize SUCCEEDED unreviewed.
    with pytest.raises(ValueError, match="Analysis and Reviewer"):
        planner().compile_proposal(
            proposal_of(AgentName.DATA, AgentName.ANALYSIS),
            goal="Analyze ticket",
            ticket_id=2,
            request_write=False,
        )


def test_compile_rejects_data_only_plan() -> None:
    with pytest.raises(ValueError, match="Analysis and Reviewer"):
        planner().compile_proposal(
            proposal_of(AgentName.DATA),
            goal="Analyze ticket",
            ticket_id=2,
            request_write=False,
        )


def test_compile_rejects_knowledge_only_plan() -> None:
    with pytest.raises(ValueError, match="Analysis and Reviewer"):
        planner().compile_proposal(
            proposal_of(AgentName.KNOWLEDGE),
            goal="Find a runbook",
            ticket_id=2,
            request_write=False,
        )


def test_compile_accepts_evidence_plan_with_analysis_and_reviewer() -> None:
    plan = planner().compile_proposal(
        proposal_of(AgentName.DATA, AgentName.ANALYSIS, AgentName.REVIEWER),
        goal="Analyze ticket",
        ticket_id=2,
        request_write=False,
    )
    assert {task.agent for task in plan.tasks} == {
        AgentName.DATA,
        AgentName.ANALYSIS,
        AgentName.REVIEWER,
    }


def test_revision_preserves_original_deadline_and_budget() -> None:
    """Re-planning must inherit the run budget instead of re-arming ``now + N``.

    ``completed_plan()`` sets a deadline an hour out; a re-armed deadline would be
    ``now + SERVICEMIND_RUN_DEADLINE_SECONDS`` (~minutes), so equality is a strong
    regression check for the re-arm bug.
    """
    previous = completed_plan()
    due = previous.deadline
    revised = planner().compile_proposal(
        proposal_of(AgentName.DATA, AgentName.ANALYSIS, AgentName.REVIEWER),
        goal=previous.goal,
        ticket_id=2,
        request_write=False,
        previous=previous,
    )
    assert revised.deadline == due
    assert revised.budget == previous.budget
    assert revised.budget.deadline == due


@pytest.mark.asyncio
async def test_replan_keeps_original_deadline(monkeypatch) -> None:
    import servicemind.orchestration.dynamic_planner as dp

    monkeypatch.setattr(dp, "get_model", lambda _: object())
    monkeypatch.setattr(dp, "structured_output", lambda model, schema: FakeRunnable(revision()))
    previous = completed_plan()
    due = previous.deadline
    review = ReviewResult(
        decision=ReviewDecision.RETRIEVE_MORE,
        missing_evidence=["runbook"],
        risk_level=RiskLevel.MEDIUM,
        feedback="Retrieve a runbook.",
    )
    revised = await DynamicPlanner().revise_plan(
        previous=previous,
        review=review,
        ticket_id=2,
        request_write=False,
    )
    assert revised.deadline == due
    assert revised.budget.deadline == due


@pytest.mark.asyncio
async def test_revise_plan_without_prior_review_keeps_deadline(monkeypatch) -> None:
    """A Supervisor REPLAN can arrive before the first review (evidence gathered,
    not yet joined). The planner must accept ``review=None`` -- the path the
    revision node now uses -- and still carry the original deadline forward."""
    import servicemind.orchestration.dynamic_planner as dp

    monkeypatch.setattr(dp, "get_model", lambda _: object())
    monkeypatch.setattr(dp, "structured_output", lambda model, schema: FakeRunnable(revision()))
    previous = completed_plan()
    due = previous.deadline
    revised = await DynamicPlanner().revise_plan(
        previous=previous,
        review=None,
        ticket_id=2,
        request_write=False,
    )
    assert revised.deadline == due


@pytest.mark.asyncio
async def test_retrieve_more_revision_must_add_knowledge_task(monkeypatch) -> None:
    import servicemind.orchestration.dynamic_planner as dp

    no_knowledge = PlanRevisionProposal(
        rationale_summary="Re-read evidence but add no Knowledge task.",
        preserved_task_ids=["T1", "T2", "T3"],
        tasks=[
            PlanTaskProposal(
                task_id="T4",
                agent=AgentName.DATA,
                task_type="retrieve_more_data",
                objective="Re-read the ticket",
                depends_on=["T1"],
            ),
            PlanTaskProposal(
                task_id="T5",
                agent=AgentName.ANALYSIS,
                task_type="analyze_ticket",
                objective="Reanalyze with new evidence",
                depends_on=["T1", "T4"],
            ),
            PlanTaskProposal(
                task_id="T6",
                agent=AgentName.REVIEWER,
                task_type="review_analysis",
                objective="Review the revised analysis",
                depends_on=["T5"],
            ),
        ],
        max_parallel=2,
    )
    monkeypatch.setattr(dp, "get_model", lambda _: object())
    monkeypatch.setattr(
        dp,
        "structured_output",
        lambda model, schema: FakeRunnable(no_knowledge),
    )
    review = ReviewResult(
        decision=ReviewDecision.RETRIEVE_MORE,
        missing_evidence=["runbook"],
        risk_level=RiskLevel.MEDIUM,
        feedback="Retrieve a runbook.",
    )
    with pytest.raises(ValueError, match="Knowledge"):
        await DynamicPlanner().revise_plan(
            previous=completed_plan(),
            review=review,
            ticket_id=2,
            request_write=False,
        )


# ---------------------------------------------------------------------------
# Live RETRIEVE_MORE regression (2026-09-22): a run died with
# ``termination_code=critical_error`` after two ``replanner.proposal_rejected``
# events -- "Replan must add new Analysis and Reviewer tasks" then "Replan
# preserved_task_ids must exactly match completed tasks".
#
# The revision prompt told the model "Task IDs must be T1, T2, ...", so on retry
# it restarted the numbering: T5/T6 -- the completed Analysis and Reviewer --
# were handed to the new data and knowledge tasks, and the surviving new tasks
# no longer contained Analysis/Reviewer. The contract the validators enforce
# (completed ids survive verbatim, new tasks continue the numbering) was never
# stated to the model, so the Reviewer's retrieval round could not run.
# ---------------------------------------------------------------------------


def _evidence_plan() -> TaskPlan:
    """The live shape: two evidence tasks, two retrievals, Analysis then Reviewer."""
    due = datetime.now(UTC) + timedelta(hours=1)
    budget = Budget(deadline=due)
    layout = [
        ("T1", AgentName.DATA, "get_ticket", []),
        ("T2", AgentName.DATA, "get_ticket", []),
        ("T3", AgentName.KNOWLEDGE, "retrieve_knowledge", []),
        ("T4", AgentName.KNOWLEDGE, "retrieve_knowledge", []),
        ("T5", AgentName.ANALYSIS, "analyze_ticket", ["T1", "T2", "T3", "T4"]),
        ("T6", AgentName.REVIEWER, "review_analysis", ["T5"]),
    ]
    return TaskPlan(
        goal="Analyze ticket",
        tasks=[
            Task(
                task_id=task_id,
                agent=agent,
                task_type=task_type,
                depends_on=depends_on,
                status=TaskStatus.SUCCESS,
                output_ref=f"out:{task_id}",
                deadline=due,
            )
            for task_id, agent, task_type, depends_on in layout
        ],
        max_parallel=2,
        max_steps=budget.max_steps,
        max_replans=budget.max_replans,
        deadline=due,
        budget=budget,
    )


def _renumbered_revision() -> PlanRevisionProposal:
    """The captured shape: completed T5/T6 overwritten by new evidence tasks."""
    return PlanRevisionProposal(
        rationale_summary="Replan adds new Analysis and Reviewer tasks.",
        preserved_task_ids=["T1", "T2", "T3", "T4"],
        tasks=[
            PlanTaskProposal(
                task_id="T4",
                agent=AgentName.DATA,
                task_type="retrieve_more_data",
                objective="Gather additional GLPI facts",
                depends_on=["T1"],
            ),
            PlanTaskProposal(
                task_id="T5",
                agent=AgentName.KNOWLEDGE,
                task_type="retrieve_more_knowledge",
                objective="Retrieve the missing runbook",
                depends_on=["T1"],
            ),
            PlanTaskProposal(
                task_id="T6",
                agent=AgentName.ANALYSIS,
                task_type="analyze_ticket",
                objective="Reanalyze with new evidence",
                depends_on=["T1", "T4", "T5"],
            ),
            PlanTaskProposal(
                task_id="T7",
                agent=AgentName.REVIEWER,
                task_type="review_analysis",
                objective="Review the revised analysis",
                depends_on=["T6"],
            ),
        ],
        max_parallel=2,
    )


@pytest.mark.asyncio
async def test_renumbering_a_completed_task_id_is_rejected(monkeypatch) -> None:
    """The validator that fired live must keep firing: re-numbering is not a revision."""
    import servicemind.orchestration.dynamic_planner as dp

    monkeypatch.setattr(dp, "get_model", lambda _: object())
    monkeypatch.setattr(
        dp, "structured_output", lambda model, schema: FakeRunnable(_renumbered_revision())
    )
    review = ReviewResult(
        decision=ReviewDecision.RETRIEVE_MORE,
        missing_evidence=["runbook"],
        risk_level=RiskLevel.MEDIUM,
        feedback="Retrieve a runbook.",
    )
    with pytest.raises(ValueError, match="preserved_task_ids"):
        await DynamicPlanner().revise_plan(
            previous=_evidence_plan(),
            review=review,
            ticket_id=2,
            request_write=False,
        )


def test_revision_prompt_states_the_id_contract_the_model_must_follow() -> None:
    """The prompt is the only place the numbering contract reaches the model."""
    planner = DynamicPlanner()
    revision_prompt = planner._planner_prompt(False, None, PlanRevisionProposal)
    initial_prompt = planner._planner_prompt(False, None, PlanProposal)

    # The renumbering instruction is gone from the revision prompt, and replaced
    # by the contract the validators actually enforce.
    assert "Task IDs must be T1, T2, ..." not in revision_prompt
    assert "continue the numbering after the highest ID already in use" in revision_prompt
    assert "preserved_task_ids" in revision_prompt
    # The initial plan keeps first-plan numbering.
    assert "Task IDs must be T1, T2, ..." in initial_prompt


def test_revision_prompt_states_the_shape_contract_the_compiler_enforces() -> None:
    """The shape rules were enforced but never stated, and one rejection at a time.

    Live regression, 2026-09-23, ACC-03, run 16a6ba03. ``compile_proposal`` raises on the
    first shape rule it finds, so a revision that clears one is told nothing about the
    next. Of the two proposals the replanner produced, the first was rejected for adding
    no evidence task; the second, having added a Data one, was rejected for not adding a
    Knowledge one -- a rule that had never been shown to it, and the run died with the
    analysis and the review erased. The ``id_rule`` above was added for the same reason
    and in the same place; these are the rest of the contract.
    """
    planner = DynamicPlanner()
    plain = planner._planner_prompt(False, None, PlanRevisionProposal)
    retrieve = planner._planner_prompt(False, None, PlanRevisionProposal, retrieve_more=True)
    writing = planner._planner_prompt(True, None, PlanRevisionProposal)
    initial = planner._planner_prompt(False, None, PlanProposal)

    assert "add at least one new Data or Knowledge task" in plain
    assert "a new Analysis task and a new Reviewer task" in plain
    # Each conditional rule is stated exactly when the compiler will enforce it.
    assert "Knowledge agent" in retrieve
    assert "Knowledge agent" not in plain
    assert "new Action task" in writing
    assert "new Action task" not in plain
    # A first plan is not a revision, and is not bound by the revision shape rules.
    assert "add at least one new Data or Knowledge task" not in initial


@pytest.mark.asyncio
async def test_second_revision_can_express_every_completed_task(monkeypatch) -> None:
    """The live second RETRIEVE_MORE died here.

    After one successful revision the plan holds T1-T10. A further revision must
    re-list all ten alongside its new tasks, but ``PlanRevisionProposal.tasks``
    capped the list at 12, so the proposal the model produced needed 14 items and
    failed schema validation before any policy check could run::

        tasks: List should have at most 12 items after validation, not 14

    Both retries failed the same way and the run ended ``critical_error``. The
    cap has to let a cumulative proposal reach the compiled plan's own ceiling.
    """
    import servicemind.orchestration.dynamic_planner as dp

    preserved = [
        ("T1", AgentName.DATA, "get_ticket", []),
        ("T2", AgentName.DATA, "get_ticket", []),
        ("T3", AgentName.KNOWLEDGE, "retrieve_knowledge", []),
        ("T4", AgentName.KNOWLEDGE, "retrieve_knowledge", []),
        ("T5", AgentName.ANALYSIS, "analyze_ticket", ["T1", "T2", "T3", "T4"]),
        ("T6", AgentName.REVIEWER, "review_analysis", ["T5"]),
        ("T7", AgentName.DATA, "retrieve_more_data", []),
        ("T8", AgentName.KNOWLEDGE, "retrieve_more_knowledge", []),
        ("T9", AgentName.ANALYSIS, "analyze_ticket", ["T1", "T2", "T3", "T4", "T7", "T8"]),
        ("T10", AgentName.REVIEWER, "review_analysis", ["T9"]),
    ]
    due = datetime.now(UTC) + timedelta(hours=1)
    budget = Budget(deadline=due)
    previous = TaskPlan(
        goal="Analyze ticket",
        tasks=[
            Task(
                task_id=task_id,
                agent=agent,
                task_type=task_type,
                depends_on=depends_on,
                status=TaskStatus.SUCCESS,
                output_ref=f"out:{task_id}",
                deadline=due,
            )
            for task_id, agent, task_type, depends_on in preserved
        ],
        max_parallel=2,
        max_steps=budget.max_steps,
        max_replans=budget.max_replans,
        deadline=due,
        budget=budget,
    )
    proposal = PlanRevisionProposal(
        rationale_summary="Retrieve ownership evidence, then reanalyze and review.",
        preserved_task_ids=[task_id for task_id, *_ in preserved],
        tasks=[
            PlanTaskProposal(
                task_id=task_id,
                agent=agent,
                task_type=task_type,
                objective=f"Re-list completed {task_id}",
                depends_on=depends_on,
            )
            for task_id, agent, task_type, depends_on in preserved
        ]
        + [
            PlanTaskProposal(
                task_id="T11",
                agent=AgentName.DATA,
                task_type="retrieve_more_data",
                objective="Retrieve GLPI ownership and support-group evidence",
                depends_on=[],
            ),
            PlanTaskProposal(
                task_id="T12",
                agent=AgentName.KNOWLEDGE,
                task_type="retrieve_more_knowledge",
                objective="Retrieve escalation-path runbook evidence",
                depends_on=[],
            ),
            PlanTaskProposal(
                task_id="T13",
                agent=AgentName.ANALYSIS,
                task_type="analyze_ticket",
                objective="Reanalyze using all preserved and new evidence",
                depends_on=["T1", "T2", "T3", "T4", "T7", "T8", "T11", "T12"],
            ),
            PlanTaskProposal(
                task_id="T14",
                agent=AgentName.REVIEWER,
                task_type="review_analysis",
                objective="Review the reanalysis",
                depends_on=["T13"],
            ),
        ],
        max_parallel=2,
    )
    assert len(proposal.tasks) > 12, "the captured shape must actually exceed the old cap"

    monkeypatch.setattr(dp, "get_model", lambda _: object())
    monkeypatch.setattr(dp, "structured_output", lambda model, schema: FakeRunnable(proposal))
    review = ReviewResult(
        decision=ReviewDecision.RETRIEVE_MORE,
        missing_evidence=["ownership"],
        risk_level=RiskLevel.MEDIUM,
        feedback="Retrieve ownership evidence.",
    )

    revised = await DynamicPlanner().revise_plan(
        previous=previous,
        review=review,
        ticket_id=2,
        request_write=False,
    )

    assert [task.task_id for task in revised.tasks] == [f"T{index}" for index in range(1, 15)]
    assert [task.status for task in revised.tasks[:10]] == [TaskStatus.SUCCESS] * 10
    assert [task.status for task in revised.tasks[10:]] == [TaskStatus.PENDING] * 4
