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

    def _next_task_id(self, tasks: list[Task]) -> str:
        """The next free ``Tn``. Ascending, so a completed-plan revision cannot collide."""
        used = {task.task_id for task in tasks}
        index = len(used) + 1
        while f"T{index}" in used:
            index += 1
        return f"T{index}"

    def _has_ancestor(self, tasks: dict[str, Task], task: Task, agent: AgentName) -> bool:
        """Whether ``task`` reaches ``agent`` through its dependencies.

        The same question ``DagValidator`` asks; asked here over the tasks this method is
        about to complete, which are not a validated plan yet.
        """
        pending = list(task.depends_on)
        seen: set[str] = set()
        while pending:
            identifier = pending.pop()
            if identifier in seen or identifier not in tasks:
                continue
            seen.add(identifier)
            dependency = tasks[identifier]
            if dependency.agent is agent:
                return True
            pending.extend(dependency.depends_on)
        return False

    def _complete_evidence_pipeline(
        self, tasks: list[Task], *, ticket_id: int, due: datetime
    ) -> list[Task]:
        """Give an evidence-bearing proposal the Analysis and Reviewer tasks it omitted.

        Measured on 2026-10-01. For a goal the corpus does not cover -- the quality case
        ``Q-200``, "What is the company policy on accepting gifts from suppliers?" -- the
        planner returned a plan with no Analysis task (``[knowledge]``, and once
        ``[knowledge, data]``) or with a Reviewer that had no Analysis to review
        (``[knowledge, reviewer]``) in six of eight samples. The same prompt returned the
        full pipeline in the other two, so this is the model's variance, not a fixed bug in
        it: an instruction to build a *minimal* DAG and a rule that every evidence plan must
        carry Analysis and Reviewer do not have a common solution the model can find
        reliably, and it resolves the tension differently each time.

        Asking again does not fix that. The planner gets two attempts and the run then dies
        with ``critical_error``, which is what the live probe observed: a user question with
        no matching runbook produced no answer at all, most of the time. The pipeline below
        is policy, not preference -- the fail-closed rule exists so evidence is never joined
        and finalized without a review -- and policy that has exactly one correct
        completion should be *applied*, not requested and then rejected.

        Only the initial plan is completed. A revision is told to add new Analysis and
        Reviewer tasks and is rejected if it does not; that path was not observed failing,
        and completing it would have to reconcile with the completed tasks it must preserve.

        The original proposal is not otherwise reshaped: if it already contains the
        pipeline, or contains no evidence tasks at all, it is returned as it was.
        """
        evidence = [
            task.task_id for task in tasks if task.agent in (AgentName.DATA, AgentName.KNOWLEDGE)
        ]
        if not evidence:
            return tasks

        by_id = {task.task_id: task for task in tasks}
        analysis = [task for task in tasks if task.agent is AgentName.ANALYSIS]
        if analysis:
            analysis_task = analysis[0]
            # The prompt already requires Analysis to depend on every evidence task, and
            # a proposal that omitted one of them is the same omission this method exists
            # to repair: Analysis would be asked to derive from evidence it never joined.
            for task_id in dict.fromkeys(evidence):
                if task_id not in analysis_task.depends_on:
                    analysis_task.depends_on.append(task_id)
        else:
            contract = self.registry.get(AgentName.ANALYSIS)
            analysis_task = Task(
                task_id=self._next_task_id(tasks),
                agent=AgentName.ANALYSIS,
                task_type=contract.task_types[0],
                input={
                    "objective": "Derive the ITSM analysis from the joined evidence.",
                    "ticket_id": ticket_id,
                },
                depends_on=list(dict.fromkeys(evidence)),
                error_policy=ErrorPolicy(contract.error_policy),
                deadline=due,
            )
            tasks.append(analysis_task)
            by_id[analysis_task.task_id] = analysis_task

        reviewers = [task for task in tasks if task.agent is AgentName.REVIEWER]
        if reviewers:
            for reviewer in reviewers:
                by_id[reviewer.task_id] = reviewer
                if analysis_task.task_id not in reviewer.depends_on and not self._has_ancestor(
                    by_id, reviewer, AgentName.ANALYSIS
                ):
                    reviewer.depends_on.append(analysis_task.task_id)
        else:
            contract = self.registry.get(AgentName.REVIEWER)
            reviewer = Task(
                task_id=self._next_task_id(tasks),
                agent=AgentName.REVIEWER,
                task_type=contract.task_types[0],
                input={
                    "objective": "Review the analysis, its evidence and its reachable actions.",
                    "ticket_id": ticket_id,
                },
                depends_on=[analysis_task.task_id],
                error_policy=ErrorPolicy(contract.error_policy),
                deadline=due,
            )
            tasks.append(reviewer)
            by_id[reviewer.task_id] = reviewer

        # ``_validate_control_order`` requires the same thing of Action. An Action task the
        # model proposed against evidence it believed was already reviewed would otherwise
        # be compiled into a plan that claims a review happened before the one added here.
        for action in [task for task in tasks if task.agent is AgentName.ACTION]:
            by_id[action.task_id] = action
            if not self._has_ancestor(by_id, action, AgentName.REVIEWER):
                action.depends_on.append(reviewer.task_id)
        return tasks

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
        if previous is None:
            tasks = self._complete_evidence_pipeline(tasks, ticket_id=ticket_id, due=due)
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
        #
        # ``_complete_evidence_pipeline`` applies the pipeline to an initial proposal, so
        # this is a post-condition of that completion rather than the way the common case is
        # rejected -- which is the point: the rule is enforced by construction, and a
        # proposal it cannot complete is still refused. A revision keeps its own, stricter
        # rules in ``revise_plan``.
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
            # ``minimal`` used to stand alone, and the model read it as licence to drop the
            # review steps for a goal it judged to need little: the shape rules below were
            # the only thing it was contradicting. Smallest-*valid* says both, and the
            # mandatory pipeline is now spelled out instead of left to be inferred from the
            # two dependency rules.
            "You are the structured planner used by ServiceMind Supervisor. Build the "
            "smallest valid DAG from the supplied capability catalog; never invent agents or "
            "task types. Use Data for GLPI facts and actual support groups. Use Knowledge "
            "only when the goal needs runbooks or when evidence review requests it. "
            "**A plan containing a Data or Knowledge task must also contain exactly one "
            "Analysis task and exactly one Reviewer task** -- evidence is never joined and "
            "finalized without review, so there is no smaller plan that reads evidence. "
            "Analysis must depend on all evidence tasks. Reviewer must depend on Analysis. "
            "If request_write is true, Action must depend on Reviewer; otherwise do not add "
            "Action. "
            f"{id_rule}{shape_rule} Return JSON only. Do not include hidden "
            "reasoning. request_write="
            f"{str(request_write).lower()}."
            + (f" Correct this validation failure: {correction}" if correction else "")
            + f"\nRequired JSON Schema:\n{schema}"
        )


dynamic_planner = DynamicPlanner()
