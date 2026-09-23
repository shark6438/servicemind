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


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        # Asking *for* a procedure is a knowledge lookup, and the Chinese vocabulary has
        # to cover the words people actually use for one. The table above already fixes
        # the intent with its English row ("VPN MFA 的排障 Runbook 是什么？"); these are
        # the same question, and 「手册」/「流程」 are the words a Chinese operator uses
        # where that row says "runbook".
        (
            "更换手机后 VPN 的多因素认证持续失败。当前有效的处置手册是什么？",
            RouteType.SIMPLE_KNOWLEDGE_QUERY,
        ),
        ("Network Team 的 VPN 多因素认证紧急兜底流程是什么？", RouteType.SIMPLE_KNOWLEDGE_QUERY),
        ("VPN 多因素认证的官方处置流程是什么？", RouteType.SIMPLE_KNOWLEDGE_QUERY),
    ],
)
def test_asking_for_a_documented_procedure_is_a_knowledge_lookup(
    query: str, expected: RouteType
) -> None:
    assert FastPathRouter().route(query, request_write=False).route is expected


@pytest.mark.parametrize(
    "query",
    [
        # Names a manual, but the thing being asked for is a conclusion. The artifact
        # noun alone must not steer the route -- if it did, this would be answered by
        # returning whichever document matched, which is not the question.
        "更换手机后 VPN 多因素认证失败，官方手册给出了明确处置。请给出结论。",
        "请穷尽列举与该工单相关的全部知识条目、全部历史 followup 与全部技能要求。",
    ],
)
def test_mentioning_a_document_is_not_the_same_as_asking_for_one(query: str) -> None:
    assert FastPathRouter().route(query, request_write=False).route is RouteType.COMPLEX_WORKFLOW


@pytest.mark.parametrize(
    "query",
    [
        # The data fast path answers with the ticket's own fields, so it may only be
        # selected when the question is about one of those fields. A bare interrogative
        # is not a field: "…是什么" ends questions of every kind, and matching on it sent
        # knowledge questions to a route whose answer is ``ticket_facts`` -- the run then
        # reported SUCCEEDED having answered nothing that was asked.
        "更换手机后 VPN 的多因素认证持续失败。当前有效的处置手册是什么？",
        "VPN 多因素认证的官方处置流程是什么？",
        "请按 VPN/MFA 技能的要求组织证据，并说明还缺什么证据。",
    ],
)
def test_a_bare_interrogative_never_selects_the_data_fast_path(query: str) -> None:
    route = FastPathRouter().route(query, request_write=False).route
    assert route is not RouteType.SIMPLE_DATA_QUERY


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
