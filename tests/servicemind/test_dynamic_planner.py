from datetime import UTC, datetime, timedelta

import pytest

from servicemind.agents.dynamic_planner import DynamicPlanner
from servicemind.domain.review import ReviewDecision, ReviewResult, RiskLevel
from servicemind.domain.supervisor import (
    PlanProposal,
    PlanRevisionProposal,
    PlanTaskProposal,
)
from servicemind.domain.task import AgentName, Budget, Task, TaskPlan, TaskStatus


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
    from servicemind.agents import dynamic_planner

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
    from servicemind.agents import dynamic_planner

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
    import servicemind.agents.dynamic_planner as dp

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
    import servicemind.agents.dynamic_planner as dp

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
    import servicemind.agents.dynamic_planner as dp

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
