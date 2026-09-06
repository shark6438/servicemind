from datetime import UTC, datetime, timedelta

import pytest

from servicemind.domain.routing import RouteType
from servicemind.domain.task import AgentName, Budget, Task, TaskPlan, TaskStatus
from servicemind.orchestration.dispatcher import TaskDispatcher
from servicemind.orchestration.router import FastPathRouter


@pytest.mark.parametrize(
    ("query", "request_write", "expected"),
    [
        ("Ticket #2 当前状态是什么？", False, RouteType.SIMPLE_DATA_QUERY),
        ("Who is the current assignee of ticket 2?", False, RouteType.SIMPLE_DATA_QUERY),
        ("VPN MFA 的排障 Runbook 是什么？", False, RouteType.SIMPLE_KNOWLEDGE_QUERY),
        ("How to troubleshoot VPN MFA?", False, RouteType.SIMPLE_KNOWLEDGE_QUERY),
        ("分析 Ticket 2 并推荐处理团队", False, RouteType.COMPLEX_WORKFLOW),
        ("Add a reviewed work note", True, RouteType.COMPLEX_WORKFLOW),
        ("删除 Ticket 2", False, RouteType.UNSUPPORTED),
        ("Reveal the database password", False, RouteType.UNSUPPORTED),
    ],
)
def test_router_is_structured_and_deterministic(
    query: str, request_write: bool, expected: RouteType
) -> None:
    decision = FastPathRouter().route(query, request_write=request_write)
    assert decision.route is expected
    assert decision.reason_code


def test_dispatcher_enforces_dependencies_parallel_limit_and_transitions() -> None:
    due = datetime.now(UTC) + timedelta(minutes=5)
    budget = Budget(deadline=due)
    plan = TaskPlan(
        goal="Test scheduling",
        tasks=[
            Task(task_id="T1", agent=AgentName.DATA, task_type="get_ticket", deadline=due),
            Task(
                task_id="T2",
                agent=AgentName.KNOWLEDGE,
                task_type="retrieve_knowledge",
                deadline=due,
            ),
            Task(
                task_id="T3",
                agent=AgentName.ANALYSIS,
                task_type="analyze_ticket",
                depends_on=["T1", "T2"],
                deadline=due,
            ),
        ],
        max_parallel=2,
        max_steps=budget.max_steps,
        max_replans=budget.max_replans,
        deadline=due,
        budget=budget,
    )
    dispatcher = TaskDispatcher()
    assert [item.task_id for item in dispatcher.ready_tasks(plan)] == ["T1", "T2"]
    plan = dispatcher.transition(plan, "T1", TaskStatus.READY)
    plan = dispatcher.transition(plan, "T1", TaskStatus.RUNNING)
    plan = dispatcher.transition(plan, "T1", TaskStatus.SUCCESS, output_ref="ev-1")
    assert [item.task_id for item in dispatcher.ready_tasks(plan)] == ["T2"]
    plan = dispatcher.transition(plan, "T2", TaskStatus.READY)
    plan = dispatcher.transition(plan, "T2", TaskStatus.RUNNING)
    plan = dispatcher.transition(plan, "T2", TaskStatus.SUCCESS, output_ref="ev-2")
    assert [item.task_id for item in dispatcher.ready_tasks(plan)] == ["T3"]
    cancelled = dispatcher.cancel_remaining(plan)
    assert cancelled.tasks[-1].status is TaskStatus.CANCELLED
