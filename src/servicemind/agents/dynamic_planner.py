import json
from datetime import UTC, datetime, timedelta

from langchain_core.messages import HumanMessage, SystemMessage

from core import get_model, settings
from servicemind.domain.review import ReviewResult
from servicemind.domain.supervisor import (
    PlanProposal,
    PlanRevisionProposal,
    PlanTaskProposal,
)
from servicemind.domain.task import AgentName, Budget, ErrorPolicy, Task, TaskPlan, TaskStatus
from servicemind.orchestration.registry import AgentRegistry, agent_registry
from servicemind.orchestration.task_dag import DagValidator, dag_validator
from servicemind.runtime.structured import structured_output


class DynamicPlanner:
    """LLM planner whose proposals are always compiled through deterministic policy."""

    def __init__(
        self,
        registry: AgentRegistry = agent_registry,
        validator: DagValidator = dag_validator,
    ) -> None:
        self.registry = registry
        self.validator = validator

    def _capability_catalog(self) -> list[dict[str, object]]:
        return [
            {
                "agent": name.value,
                "task_types": list(self.registry.get(name).task_types),
                "scope": self.registry.get(name).read_write_scope,
            }
            for name in sorted(self.registry.names, key=lambda item: item.value)
        ]

    async def create_plan(
        self,
        *,
        goal: str,
        ticket_id: int,
        request_write: bool,
        correction: str | None = None,
    ) -> TaskPlan:
        model = get_model(settings.DEFAULT_MODEL)
        runnable = structured_output(model, PlanProposal)
        result = await runnable.ainvoke(
            [
                SystemMessage(
                    content=self._planner_prompt(
                        request_write, correction, PlanProposal
                    )
                ),
                HumanMessage(
                    content=json.dumps(
                        {
                            "goal": goal,
                            "ticket_id": ticket_id,
                            "request_write": request_write,
                            "capabilities": self._capability_catalog(),
                        },
                        ensure_ascii=False,
                    )
                ),
            ]
        )
        return self.compile_proposal(
            PlanProposal.model_validate(result),
            goal=goal,
            ticket_id=ticket_id,
            request_write=request_write,
        )

    async def revise_plan(
        self,
        *,
        previous: TaskPlan,
        review: ReviewResult,
        ticket_id: int,
        request_write: bool,
        correction: str | None = None,
    ) -> TaskPlan:
        model = get_model(settings.DEFAULT_MODEL)
        runnable = structured_output(model, PlanRevisionProposal)
        completed = [
            task.model_dump(mode="json", by_alias=True)
            for task in previous.tasks
            if task.status is TaskStatus.SUCCESS
        ]
        result = await runnable.ainvoke(
            [
                SystemMessage(
                    content=self._planner_prompt(
                        request_write, correction, PlanRevisionProposal
                    )
                ),
                HumanMessage(
                    content=json.dumps(
                        {
                            "goal": previous.goal,
                            "ticket_id": ticket_id,
                            "request_write": request_write,
                            "review_feedback": review.model_dump(mode="json"),
                            "previous_plan": previous.model_dump(mode="json", by_alias=True),
                            "completed_tasks_must_be_preserved": completed,
                            "capabilities": self._capability_catalog(),
                        },
                        ensure_ascii=False,
                    )
                ),
            ]
        )
        proposal = PlanRevisionProposal.model_validate(result)
        completed_by_id = {
            task.task_id: task
            for task in previous.tasks
            if task.status is TaskStatus.SUCCESS
        }
        if set(proposal.preserved_task_ids) != set(completed_by_id):
            raise ValueError(
                "Replan preserved_task_ids must exactly match completed tasks"
            )
        proposal_by_id = {task.task_id: task for task in proposal.tasks}
        for task_id, old in completed_by_id.items():
            proposed = proposal_by_id.get(task_id)
            if proposed is not None and (
                proposed.agent != old.agent or proposed.task_type != old.task_type
            ):
                raise ValueError("Replan changed the identity of a completed task")
        trusted_completed = [
            PlanTaskProposal(
                task_id=task.task_id,
                agent=task.agent,
                task_type=task.task_type,
                objective=str(task.task_input.get("objective", task.task_type)),
                depends_on=task.depends_on,
            )
            for task in completed_by_id.values()
        ]
        new_tasks = [
            task for task in proposal.tasks if task.task_id not in completed_by_id
        ]
        plan = self.compile_proposal(
            PlanProposal(
                rationale_summary=proposal.rationale_summary,
                tasks=[*trusted_completed, *new_tasks],
                max_parallel=proposal.max_parallel,
            ),
            goal=previous.goal,
            ticket_id=ticket_id,
            request_write=request_write,
        )
        previous_by_id = {task.task_id: task for task in previous.tasks}
        for task in plan.tasks:
            old = previous_by_id.get(task.task_id)
            if old is not None and old.status is TaskStatus.SUCCESS:
                if task.agent != old.agent or task.task_type != old.task_type:
                    raise ValueError("Replan changed the identity of a completed task")
                task.status = TaskStatus.SUCCESS
                task.output_ref = old.output_ref
                task.attempts = old.attempts
        missing = {
            task.task_id
            for task in previous.tasks
            if task.status is TaskStatus.SUCCESS
        } - {task.task_id for task in plan.tasks}
        if missing:
            raise ValueError(f"Replan dropped completed tasks: {sorted(missing)}")
        new_agents = {
            task.agent for task in plan.tasks if task.task_id not in completed_by_id
        }
        required_new = {AgentName.ANALYSIS, AgentName.REVIEWER}
        if not new_agents & {AgentName.DATA, AgentName.KNOWLEDGE}:
            raise ValueError("Replan must add at least one evidence task")
        if not required_new <= new_agents:
            raise ValueError("Replan must add new Analysis and Reviewer tasks")
        if request_write and AgentName.ACTION not in new_agents:
            raise ValueError("Write replan must add a new Action task")
        self.validator.validate(plan)
        return plan

    def compile_proposal(
        self,
        proposal: PlanProposal,
        *,
        goal: str,
        ticket_id: int,
        request_write: bool,
    ) -> TaskPlan:
        due = datetime.now(UTC) + timedelta(
            seconds=settings.SERVICEMIND_RUN_DEADLINE_SECONDS
        )
        budget = Budget(
            max_steps=settings.SERVICEMIND_MAX_STEPS,
            max_replans=settings.SERVICEMIND_MAX_REPLANS,
            max_model_calls=settings.SERVICEMIND_MAX_MODEL_CALLS,
            max_tool_calls=settings.SERVICEMIND_MAX_TOOL_CALLS,
            deadline=due,
        )
        tasks = []
        for proposed in proposal.tasks:
            contract = self.registry.get(proposed.agent)
            tasks.append(
                Task(
                    task_id=proposed.task_id,
                    agent=proposed.agent,
                    task_type=proposed.task_type,
                    input={
                        "objective": proposed.objective,
                        "ticket_id": ticket_id,
                    },
                    depends_on=proposed.depends_on,
                    error_policy=ErrorPolicy(contract.error_policy),
                    deadline=due,
                )
            )
        plan = TaskPlan(
            goal=goal,
            tasks=tasks,
            max_parallel=proposal.max_parallel,
            max_steps=budget.max_steps,
            max_replans=budget.max_replans,
            deadline=due,
            budget=budget,
        )
        self.validator.validate(plan)
        agents = {task.agent for task in plan.tasks}
        if request_write and "action" not in {agent.value for agent in agents}:
            raise ValueError("Write workflow plan must contain an Action task")
        return plan

    def _planner_prompt(
        self,
        request_write: bool,
        correction: str | None,
        schema_type: type[PlanProposal] | type[PlanRevisionProposal],
    ) -> str:
        schema = json.dumps(schema_type.model_json_schema())
        return (
            "You are the structured planner used by ServiceMind Supervisor. Build a minimal "
            "DAG from the supplied capability catalog; never invent agents or task types. "
            "Use Data for GLPI facts and actual support groups. Use Knowledge only when the "
            "goal needs runbooks or when evidence review requests it. Analysis must depend on "
            "all evidence tasks. Reviewer must depend on Analysis. If request_write is true, "
            "Action must depend on Reviewer; otherwise do not add Action. Task IDs must be T1, "
            "T2, ... and dependencies must be acyclic. Return JSON only. Do not include hidden "
            "reasoning. request_write="
            f"{str(request_write).lower()}."
            + (f" Correct this validation failure: {correction}" if correction else "")
            + f"\nRequired JSON Schema:\n{schema}"
        )


dynamic_planner = DynamicPlanner()
