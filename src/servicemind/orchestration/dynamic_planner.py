import json
from datetime import UTC, datetime, timedelta

from langchain_core.messages import HumanMessage, SystemMessage

from core import get_model, settings
from servicemind.domain.review import ReviewDecision, ReviewResult
from servicemind.domain.supervisor import (
    PlanProposal,
    PlanRevisionProposal,
    PlanTaskProposal,
)
from servicemind.domain.task import (
    MAX_PLAN_TASKS,
    AgentName,
    Budget,
    ErrorPolicy,
    Task,
    TaskPlan,
    TaskStatus,
)
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
                    content=self._planner_prompt(request_write, correction, PlanProposal)
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
        review: ReviewResult | None = None,
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
                        request_write,
                        correction,
                        PlanRevisionProposal,
                        retrieve_more=(
                            review is not None and review.decision is ReviewDecision.RETRIEVE_MORE
                        ),
                    )
                ),
                HumanMessage(
                    content=json.dumps(
                        {
                            "goal": previous.goal,
                            "ticket_id": ticket_id,
                            "request_write": request_write,
                            "review_feedback": (
                                review.model_dump(mode="json")
                                if review is not None
                                else {
                                    "decision": "replan",
                                    "feedback": correction
                                    or "Supervisor revised the evidence before the first review.",
                                }
                            ),
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
            task.task_id: task for task in previous.tasks if task.status is TaskStatus.SUCCESS
        }
        if set(proposal.preserved_task_ids) != set(completed_by_id):
            raise ValueError("Replan preserved_task_ids must exactly match completed tasks")
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
        new_tasks = [task for task in proposal.tasks if task.task_id not in completed_by_id]
        plan = self.compile_proposal(
            PlanProposal(
                rationale_summary=proposal.rationale_summary,
                tasks=[*trusted_completed, *new_tasks],
                max_parallel=proposal.max_parallel,
            ),
            goal=previous.goal,
            ticket_id=ticket_id,
            request_write=request_write,
            previous=previous,
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
        missing = {task.task_id for task in previous.tasks if task.status is TaskStatus.SUCCESS} - {
            task.task_id for task in plan.tasks
        }
        if missing:
            raise ValueError(f"Replan dropped completed tasks: {sorted(missing)}")
        new_agents = {task.agent for task in plan.tasks if task.task_id not in completed_by_id}
        required_new = {AgentName.ANALYSIS, AgentName.REVIEWER}
        if not new_agents & {AgentName.DATA, AgentName.KNOWLEDGE}:
            raise ValueError("Replan must add at least one evidence task")
        if not required_new <= new_agents:
            raise ValueError("Replan must add new Analysis and Reviewer tasks")
        if request_write and AgentName.ACTION not in new_agents:
            raise ValueError("Write replan must add a new Action task")
        if (
            review is not None
            and review.decision is ReviewDecision.RETRIEVE_MORE
            and AgentName.KNOWLEDGE not in new_agents
        ):
            raise ValueError("RETRIEVE_MORE replan must add a Knowledge task")
        self.validator.validate(plan)
        return plan

    def compile_proposal(
        self,
        proposal: PlanProposal,
        *,
        goal: str,
        ticket_id: int,
        request_write: bool,
        previous: TaskPlan | None = None,
    ) -> TaskPlan:
        if previous is not None:
            # A revision inherits the run's original budget and deadline instead of
            # re-arming the clock: re-planning must not silently extend how long a
            # run is allowed to keep retrying.
            budget = previous.budget
            due = previous.deadline
        else:
            due = datetime.now(UTC) + timedelta(seconds=settings.SERVICEMIND_RUN_DEADLINE_SECONDS)
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
        if request_write and AgentName.ACTION not in agents:
            raise ValueError("Write workflow plan must contain an Action task")
        # Fail closed: any plan that reads evidence must run through Analysis *and*
        # the Reviewer gate. Without this, a data/knowledge-only plan would join
        # evidence and let the Supervisor finalize SUCCEEDED with nothing reviewed.
        if (
            agents & {AgentName.DATA, AgentName.KNOWLEDGE}
            and not {
                AgentName.ANALYSIS,
                AgentName.REVIEWER,
            }
            <= agents
        ):
            raise ValueError("Evidence-bearing plans must contain Analysis and Reviewer tasks")
        return plan

    def _planner_prompt(
        self,
        request_write: bool,
        correction: str | None,
        schema_type: type[PlanProposal] | type[PlanRevisionProposal],
        *,
        retrieve_more: bool = False,
    ) -> str:
        schema = json.dumps(schema_type.model_json_schema())
        shape_rule = ""
        if schema_type is PlanRevisionProposal:
            # A revision must *extend* the running plan, not re-number it. Telling the model
            # "Task IDs must be T1, T2, ..." made it restart the numbering, so the new data
            # and knowledge tasks took over T5/T6 -- the IDs already held by the completed
            # Analysis and Reviewer tasks -- and ``revise_plan`` correctly rejected the
            # proposal. Both retries then failed and the run died instead of performing the
            # retrieval round the Reviewer had asked for.
            id_rule = (
                "List every completed task's exact ID in preserved_task_ids, and re-list each "
                "one in tasks with its original ID, agent and task_type unchanged. New tasks "
                "must continue the numbering after the highest ID already in use -- when T1-T6 "
                "have completed, new tasks are T7, T8, ... -- and must never reuse an ID that a "
                f"completed task already holds. At most {MAX_PLAN_TASKS} tasks in total. "
                "Dependencies must be acyclic."
            )
            # The shape rules were enforced but never stated, so the only way the model
            # learned them was by being rejected for one at a time. A revision has to
            # satisfy all of them at once, and it is told what they are here.
            shape_rule = (
                " A revision must add at least one new Data or Knowledge task, and a new "
                "Analysis task and a new Reviewer task, each with a new ID."
            )
            if request_write:
                shape_rule += " Because request_write is true, it must also add a new Action task."
            if retrieve_more:
                shape_rule += (
                    " The review asked for more evidence, so at least one of the new "
                    "evidence tasks must use the Knowledge agent."
                )
        else:
            id_rule = "Task IDs must be T1, T2, ... and dependencies must be acyclic."
        return (
            "You are the structured planner used by ServiceMind Supervisor. Build a minimal "
            "DAG from the supplied capability catalog; never invent agents or task types. "
            "Use Data for GLPI facts and actual support groups. Use Knowledge only when the "
            "goal needs runbooks or when evidence review requests it. Analysis must depend on "
            "all evidence tasks. Reviewer must depend on Analysis. If request_write is true, "
            "Action must depend on Reviewer; otherwise do not add Action. "
            f"{id_rule}{shape_rule} Return JSON only. Do not include hidden "
            "reasoning. request_write="
            f"{str(request_write).lower()}."
            + (f" Correct this validation failure: {correction}" if correction else "")
            + f"\nRequired JSON Schema:\n{schema}"
        )


dynamic_planner = DynamicPlanner()
