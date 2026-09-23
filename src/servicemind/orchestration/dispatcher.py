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
        """Every task whose dependencies are satisfied and that has not started.

        Readiness and capacity are deliberately not the same question. This used to
        return only as many tasks as ``max_parallel`` had free slots, sorted by task
        id -- so a plan with three dependency-ready tasks offered two of them, and
        which two was decided by how the planner happened to number them. Callers
        that then narrow the result to the agents a given action can run (DISPATCH
        may only select data/knowledge tasks) were filtering a set that had already
        been truncated by agents the action cannot run: a reopened plan where an
        analysis task sorted before an evidence task made that evidence task
        unreachable -- neither ready enough to select nor visible as ineligible --
        and the run ended in ``supervisor_policy_failure`` with its review passed and
        its action pending. Capacity bounds how many tasks one transition may start
        (:meth:`available_slots`); it does not decide which tasks exist.
        """
        successful = {task.task_id for task in plan.tasks if task.status is TaskStatus.SUCCESS}
        ready = [
            task
            for task in plan.tasks
            if task.status in {TaskStatus.PENDING, TaskStatus.READY}
            and set(task.depends_on) <= successful
        ]
        return sorted(ready, key=lambda task: task.task_id)

    def available_slots(self, plan: TaskPlan) -> int:
        """How many more tasks may be started in parallel right now."""
        running = sum(task.status is TaskStatus.RUNNING for task in plan.tasks)
        return max(plan.max_parallel - running, 0)

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

    def invalidate(self, plan: TaskPlan) -> TaskPlan:
        """Return the plan with every task back to ``PENDING`` and no output refs.

        Deliberately not a ``transition``. That machine describes work moving forward
        under an authority that still holds; this is the other case. When a run's scope
        narrows, the tasks that already ran were executed under grants the requester no
        longer has, so their results are not results the run may keep -- and for work
        whose output is void there is no honest status other than "not done". Marking
        them anything else leaves the Supervisor looking at a plan with nothing ready,
        which reads as "all work is complete" and finalizes a run that has just lost
        everything its conclusions rested on.

        ``attempts`` is kept: it is a record of what was spent, and rewriting it would
        let a run reset its own budget story by having its grants revoked.
        """
        updated = deepcopy(plan)
        for task in updated.tasks:
            task.status = TaskStatus.PENDING
            task.output_ref = None
        return updated

    def cancel_remaining(self, plan: TaskPlan) -> TaskPlan:
        updated = deepcopy(plan)
        for task in updated.tasks:
            if task.status not in TERMINAL_TASK_STATUSES:
                task.status = TaskStatus.CANCELLED
        return updated


task_dispatcher = TaskDispatcher()
