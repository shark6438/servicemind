from dataclasses import dataclass

from servicemind.domain.review import ReviewDecision, ReviewResult
from servicemind.domain.supervisor import SupervisorAction, SupervisorDecision
from servicemind.domain.task import AgentName, RuntimeControl, TaskPlan, TaskStatus
from servicemind.orchestration.dispatcher import TaskDispatcher, task_dispatcher


class SupervisorPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class SupervisorPolicy:
    dispatcher: TaskDispatcher = task_dispatcher

    def legal_actions(self, state: dict) -> set[SupervisorAction]:
        if state.get("termination_code"):
            return {SupervisorAction.FINALIZE}
        # A human already resolved an escalation with "continue". The Supervisor
        # re-enters after the interrupt; offering ESCALATE again would re-interrupt
        # the same person in a loop. The accepted outcome is terminal: finalize.
        if (state.get("human_review") or {}).get("decision") == "continue":
            return {SupervisorAction.FINALIZE}
        plan_payload = state.get("task_plan")
        if not plan_payload:
            return {SupervisorAction.PLAN, SupervisorAction.ESCALATE}

        plan = TaskPlan.model_validate(plan_payload)
        review_payload = state.get("review_result")
        if review_payload:
            review = ReviewResult.model_validate(review_payload)
            # Schema evolution is fail-closed: a degraded review cannot authorize handoff,
            # even if an old or malformed producer labels its decision as passed.
            if review.degraded:
                return {SupervisorAction.ESCALATE, SupervisorAction.FINALIZE}
            decision = review.decision
            if decision is ReviewDecision.PASSED:
                if state.get("request_write"):
                    # Handoff is only legal once a ready Action task actually exists;
                    # otherwise the graph would reach handoff_node and raise. A replan
                    # is always available to rebuild the missing Action task.
                    ready = self.dispatcher.ready_tasks(plan)
                    legal = {SupervisorAction.REPLAN}
                    if any(task.agent is AgentName.ACTION for task in ready):
                        legal.add(SupervisorAction.HANDOFF_ACTION)
                    return legal
                return {SupervisorAction.FINALIZE, SupervisorAction.REPLAN}
            if decision is ReviewDecision.RETRIEVE_MORE:
                return {
                    SupervisorAction.RETRIEVE_MORE,
                    SupervisorAction.REPLAN,
                    SupervisorAction.ESCALATE,
                }
            if decision is ReviewDecision.REPLAN:
                return {SupervisorAction.REPLAN, SupervisorAction.ESCALATE}
            if decision is ReviewDecision.ESCALATE:
                return {SupervisorAction.ESCALATE, SupervisorAction.FINALIZE}
            if decision is ReviewDecision.ABSTAIN:
                # Evidence-insufficiency abstention is a terminal, user-facing outcome:
                # retrieval already ran its extra round and the reviewer will not
                # fabricate. No replan loop and no human escalation -- just finalize.
                return {SupervisorAction.FINALIZE}
            return {SupervisorAction.FINALIZE}

        if state.get("evidence_dirty"):
            return {SupervisorAction.JOIN_EVIDENCE, SupervisorAction.REPLAN}

        ready = self.dispatcher.ready_tasks(plan)
        ready_agents = {task.agent for task in ready}
        legal: set[SupervisorAction] = set()
        if ready_agents & {AgentName.DATA, AgentName.KNOWLEDGE}:
            legal.add(SupervisorAction.DISPATCH)
        if AgentName.ANALYSIS in ready_agents:
            legal.add(SupervisorAction.ANALYZE)
        if AgentName.REVIEWER in ready_agents:
            legal.add(SupervisorAction.REVIEW)
        if AgentName.ACTION in ready_agents:
            legal.add(SupervisorAction.ESCALATE)
        if not ready and all(
            task.status in {TaskStatus.SUCCESS, TaskStatus.SKIPPED, TaskStatus.CANCELLED}
            for task in plan.tasks
        ):
            legal.add(SupervisorAction.FINALIZE)
        legal.update({SupervisorAction.REPLAN, SupervisorAction.ESCALATE})
        return legal

    def validate(self, decision: SupervisorDecision, state: dict) -> None:
        legal = self.legal_actions(state)
        if decision.action not in legal:
            raise SupervisorPolicyError(
                f"Action {decision.action.value!r} is illegal; legal actions are "
                f"{sorted(item.value for item in legal)}"
            )
        if decision.action is SupervisorAction.DISPATCH:
            plan = TaskPlan.model_validate(state["task_plan"])
            ready = {
                task.task_id: task
                for task in self.dispatcher.ready_tasks(plan)
                if task.agent in {AgentName.DATA, AgentName.KNOWLEDGE}
            }
            selected = decision.selected_task_ids
            if not selected:
                raise SupervisorPolicyError("DISPATCH requires selected_task_ids")
            if len(selected) != len(set(selected)):
                raise SupervisorPolicyError("DISPATCH task IDs must be unique")
            if len(selected) > plan.max_parallel:
                raise SupervisorPolicyError("DISPATCH exceeds max_parallel")
            control = RuntimeControl.model_validate(state.get("control", {}))
            remaining_tools = max(
                plan.budget.max_tool_calls - control.tool_call_count,
                0,
            )
            if len(selected) > remaining_tools:
                raise SupervisorPolicyError(
                    "DISPATCH has fewer remaining tool calls than selected tasks"
                )
            unknown = set(selected) - ready.keys()
            if unknown:
                raise SupervisorPolicyError(
                    f"DISPATCH selected tasks that are not ready: {sorted(unknown)}"
                )
        elif decision.action in {SupervisorAction.ANALYZE, SupervisorAction.REVIEW}:
            plan = TaskPlan.model_validate(state["task_plan"])
            expected_agent = (
                AgentName.ANALYSIS
                if decision.action is SupervisorAction.ANALYZE
                else AgentName.REVIEWER
            )
            ready_ids = {
                task.task_id
                for task in self.dispatcher.ready_tasks(plan)
                if task.agent is expected_agent
            }
            if len(decision.selected_task_ids) != 1:
                raise SupervisorPolicyError(
                    f"{decision.action.value} requires exactly one selected task"
                )
            if decision.selected_task_ids[0] not in ready_ids:
                raise SupervisorPolicyError(
                    f"{decision.action.value} selected a task that is not ready"
                )
        elif decision.selected_task_ids:
            raise SupervisorPolicyError(
                f"{decision.action.value} must not select dispatch task IDs"
            )


supervisor_policy = SupervisorPolicy()
