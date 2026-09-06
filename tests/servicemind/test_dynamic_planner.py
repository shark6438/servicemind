from datetime import UTC, datetime, timedelta

import pytest

from servicemind.agents.dynamic_planner import DynamicPlanner
from servicemind.domain.review import ReviewDecision, ReviewResult, RiskLevel
from servicemind.domain.supervisor import PlanRevisionProposal, PlanTaskProposal
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
