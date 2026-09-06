from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from servicemind.domain.task import AgentName, Budget, Task, TaskPlan
from servicemind.orchestration.registry import agent_registry
from servicemind.orchestration.task_dag import DagLimits, DagValidator, PlanValidationError


def deadline() -> datetime:
    return datetime.now(UTC) + timedelta(minutes=10)


def task(
    identifier: str,
    agent: AgentName,
    task_type: str,
    dependencies: list[str] | None = None,
) -> Task:
    return Task(
        task_id=identifier,
        agent=agent,
        task_type=task_type,
        depends_on=dependencies or [],
        deadline=deadline(),
    )


def plan(tasks: list[Task], *, max_parallel: int = 4, max_replans: int = 2) -> TaskPlan:
    due = deadline()
    budget = Budget(max_replans=max_replans, deadline=due)
    return TaskPlan(
        goal="Analyze ticket with evidence",
        tasks=tasks,
        max_parallel=max_parallel,
        max_steps=budget.max_steps,
        max_replans=max_replans,
        deadline=due,
        budget=budget,
    )


def valid_tasks() -> list[Task]:
    return [
        task("T1", AgentName.DATA, "get_ticket"),
        task("T2", AgentName.KNOWLEDGE, "retrieve_knowledge"),
        task("T3", AgentName.ANALYSIS, "analyze_ticket", ["T1", "T2"]),
        task("T4", AgentName.REVIEWER, "review_analysis", ["T3"]),
        task("T5", AgentName.ACTION, "propose_followup", ["T4"]),
    ]


def test_agent_registry_is_exactly_the_five_professional_agents() -> None:
    assert agent_registry.names == set(AgentName)
    for name in AgentName:
        contract = agent_registry.get(name)
        if name is AgentName.DATA:
            assert contract.allowed_tools == (
                "glpi.read.ticket",
                "glpi.read.groups",
                "glpi.read.ticket_followups",
            )
        elif name is AgentName.KNOWLEDGE:
            assert contract.allowed_tools == (
                "knowledge.search.hybrid",
                "knowledge.parent.expand",
            )
        else:
            assert contract.allowed_tools == ()
        assert "glpi.write" in contract.forbidden_tools


def test_valid_dag_has_deterministic_topological_order() -> None:
    assert DagValidator().validate(plan(valid_tasks())) == ["T1", "T2", "T3", "T4", "T5"]


@pytest.mark.parametrize(
    ("tasks", "code"),
    [
        (
            [
                task("T1", AgentName.DATA, "get_ticket"),
                task("T1", AgentName.KNOWLEDGE, "retrieve_knowledge"),
            ],
            "DUPLICATE_TASK_ID",
        ),
        ([task("T1", AgentName.DATA, "get_ticket", ["T9"])], "MISSING_DEPENDENCY"),
        (
            [
                task("T1", AgentName.DATA, "get_ticket", ["T2"]),
                task("T2", AgentName.KNOWLEDGE, "retrieve_knowledge", ["T1"]),
            ],
            "CYCLIC_DAG",
        ),
        (
            [
                task("T1", AgentName.DATA, "get_ticket"),
                task("T2", AgentName.ACTION, "propose_followup", ["T1"]),
            ],
            "ILLEGAL_ACTION_FLOW",
        ),
        (
            [
                task("T1", AgentName.DATA, "get_ticket"),
                task("T2", AgentName.REVIEWER, "review_analysis", ["T1"]),
            ],
            "REVIEWER_BEFORE_ANALYSIS",
        ),
    ],
)
def test_invalid_dags_are_rejected(tasks: list[Task], code: str) -> None:
    with pytest.raises(PlanValidationError) as error:
        DagValidator().validate(plan(tasks))
    assert error.value.code == code


def test_unknown_agent_is_rejected_by_registry_validator() -> None:
    invalid = task("T1", AgentName.DATA, "get_ticket").model_copy(
        update={"agent": "security_agent"}
    )
    with pytest.raises(PlanValidationError, match="UNKNOWN_AGENT"):
        DagValidator().validate(plan([invalid]))


def test_self_dependency_is_rejected_by_contract() -> None:
    with pytest.raises(ValidationError, match="itself"):
        task("T1", AgentName.DATA, "get_ticket", ["T1"])


def test_limits_are_enforced() -> None:
    with pytest.raises(PlanValidationError, match="MAX_PARALLEL"):
        DagValidator().validate(plan(valid_tasks(), max_parallel=5))
    many = [
        task(f"T{index}", AgentName.DATA, "get_ticket")
        for index in range(1, 14)
    ]
    with pytest.raises(PlanValidationError, match="MAX_TASKS"):
        DagValidator(limits=DagLimits(max_tasks=12)).validate(plan(many))
