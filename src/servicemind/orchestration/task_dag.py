from collections import Counter, deque
from dataclasses import dataclass
from datetime import UTC, datetime

from core import settings
from servicemind.domain.task import AgentName, Task, TaskPlan
from servicemind.orchestration.registry import AgentRegistry, agent_registry


class PlanValidationError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class DagLimits:
    max_tasks: int = 12
    max_parallel: int = 4
    max_replans: int = 2


class DagValidator:
    def __init__(
        self,
        registry: AgentRegistry = agent_registry,
        limits: DagLimits = DagLimits(),
    ) -> None:
        self.registry = registry
        self.limits = limits

    def validate(self, plan: TaskPlan) -> list[str]:
        if len(plan.tasks) > self.limits.max_tasks:
            raise PlanValidationError("MAX_TASKS_EXCEEDED", "Plan has too many tasks")
        if plan.max_parallel > self.limits.max_parallel:
            raise PlanValidationError(
                "MAX_PARALLEL_EXCEEDED", "Plan max_parallel exceeds runtime policy"
            )
        if plan.max_replans > self.limits.max_replans:
            raise PlanValidationError(
                "MAX_REPLANS_EXCEEDED", "Plan max_replans exceeds runtime policy"
            )
        if plan.deadline <= datetime.now(UTC):
            raise PlanValidationError("DEADLINE_EXCEEDED", "Plan deadline is in the past")

        task_ids = [task.task_id for task in plan.tasks]
        duplicates = [key for key, count in Counter(task_ids).items() if count > 1]
        if duplicates:
            raise PlanValidationError(
                "DUPLICATE_TASK_ID", f"Duplicate task IDs: {sorted(duplicates)}"
            )
        tasks = {task.task_id: task for task in plan.tasks}
        for task in plan.tasks:
            self._validate_task(task, tasks)

        order = self._topological_order(plan.tasks)
        self._validate_control_order(tasks)
        return order

    def _validate_task(self, task: Task, tasks: dict[str, Task]) -> None:
        if not self.registry.contains(task.agent):
            raise PlanValidationError("UNKNOWN_AGENT", str(task.agent))
        contract = self.registry.get(task.agent)
        if task.task_type not in contract.task_types:
            raise PlanValidationError(
                "INVALID_TASK_TYPE",
                f"{task.task_type!r} is not allowed for {task.agent.value}",
            )
        missing = sorted(set(task.depends_on) - tasks.keys())
        if missing:
            raise PlanValidationError(
                "MISSING_DEPENDENCY", f"{task.task_id} references {missing}"
            )
        if task.task_id in task.depends_on:
            raise PlanValidationError("SELF_DEPENDENCY", task.task_id)

    def _topological_order(self, tasks: list[Task]) -> list[str]:
        indegree = {task.task_id: len(set(task.depends_on)) for task in tasks}
        children: dict[str, list[str]] = {task.task_id: [] for task in tasks}
        for task in tasks:
            for dependency in task.depends_on:
                children[dependency].append(task.task_id)
        ready = deque(sorted(key for key, value in indegree.items() if value == 0))
        order: list[str] = []
        while ready:
            task_id = ready.popleft()
            order.append(task_id)
            for child in sorted(children[task_id]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
        if len(order) != len(tasks):
            raise PlanValidationError("CYCLIC_DAG", "Task dependencies contain a cycle")
        return order

    def _validate_control_order(self, tasks: dict[str, Task]) -> None:
        by_agent: dict[AgentName, list[Task]] = {}
        for task in tasks.values():
            by_agent.setdefault(task.agent, []).append(task)
        for reviewer in by_agent.get(AgentName.REVIEWER, []):
            if not self._has_ancestor(reviewer, AgentName.ANALYSIS, tasks):
                raise PlanValidationError(
                    "REVIEWER_BEFORE_ANALYSIS",
                    f"{reviewer.task_id} must depend on analysis",
                )
        for action in by_agent.get(AgentName.ACTION, []):
            if not self._has_ancestor(action, AgentName.REVIEWER, tasks):
                raise PlanValidationError(
                    "ILLEGAL_ACTION_FLOW",
                    f"{action.task_id} must depend on reviewer",
                )

    def _has_ancestor(
        self, task: Task, agent: AgentName, tasks: dict[str, Task]
    ) -> bool:
        pending = list(task.depends_on)
        seen: set[str] = set()
        while pending:
            identifier = pending.pop()
            if identifier in seen:
                continue
            seen.add(identifier)
            dependency = tasks[identifier]
            if dependency.agent is agent:
                return True
            pending.extend(dependency.depends_on)
        return False


dag_validator = DagValidator(
    limits=DagLimits(
        max_tasks=settings.SERVICEMIND_MAX_TASKS,
        max_parallel=settings.SERVICEMIND_MAX_PARALLEL,
        max_replans=settings.SERVICEMIND_MAX_REPLANS,
    )
)
