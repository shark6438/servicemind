from copy import deepcopy

from servicemind.domain.task import Task, TaskPlan, TaskStatus

TERMINAL_TASK_STATUSES = {
    TaskStatus.SUCCESS,
    TaskStatus.FAILED,
    TaskStatus.SKIPPED,
    TaskStatus.CANCELLED,
}


class TaskDispatcher:
    """Maintain task state from dependencies; the LLM never decides task completion."""

    def ready_tasks(self, plan: TaskPlan) -> list[Task]:
        successful = {task.task_id for task in plan.tasks if task.status is TaskStatus.SUCCESS}
        running = sum(task.status is TaskStatus.RUNNING for task in plan.tasks)
        capacity = max(plan.max_parallel - running, 0)
        ready = [
            task
            for task in plan.tasks
            if task.status in {TaskStatus.PENDING, TaskStatus.READY}
            and set(task.depends_on) <= successful
        ]
        return sorted(ready, key=lambda task: task.task_id)[:capacity]

    def transition(
        self,
        plan: TaskPlan,
        task_id: str,
        status: TaskStatus,
        *,
        output_ref: str | None = None,
    ) -> TaskPlan:
        updated = deepcopy(plan)
        task = next((item for item in updated.tasks if item.task_id == task_id), None)
        if task is None:
            raise KeyError(task_id)
        allowed = {
            TaskStatus.PENDING: {TaskStatus.READY, TaskStatus.CANCELLED},
            TaskStatus.READY: {TaskStatus.RUNNING, TaskStatus.CANCELLED},
            TaskStatus.RUNNING: TERMINAL_TASK_STATUSES,
        }
        if task.status not in allowed or status not in allowed[task.status]:
            raise ValueError(f"Illegal task transition {task.status.value} -> {status.value}")
        task.status = status
        if status is TaskStatus.RUNNING:
            task.attempts += 1
        if output_ref is not None:
            task.output_ref = output_ref
        return updated

    def cancel_remaining(self, plan: TaskPlan) -> TaskPlan:
        updated = deepcopy(plan)
        for task in updated.tasks:
            if task.status not in TERMINAL_TASK_STATUSES:
                task.status = TaskStatus.CANCELLED
        return updated


task_dispatcher = TaskDispatcher()
