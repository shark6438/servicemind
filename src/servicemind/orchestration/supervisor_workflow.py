import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from core import settings
from servicemind.agents.action import ActionAgent, action_agent
from servicemind.agents.analysis import AnalysisAgent, analysis_agent
from servicemind.agents.data import DataAgent, data_agent
from servicemind.agents.dynamic_planner import DynamicPlanner, dynamic_planner
from servicemind.agents.knowledge import KnowledgeAgent, knowledge_agent
from servicemind.agents.reviewer import ReviewerAgent, reviewer_agent
from servicemind.agents.supervisor import SupervisorAgent, supervisor_agent
from servicemind.context.contracts import ContextAgent
from servicemind.domain.analysis import AnalysisResult
from servicemind.domain.evidence import Evidence, JoinedEvidence, join_evidence
from servicemind.domain.handoff import HandoffEnvelope
from servicemind.domain.models import ActionIntent, ApprovalDecision
from servicemind.domain.review import ReviewDecision, ReviewResult, RiskLevel
from servicemind.domain.routing import RouteDecision, RouteType
from servicemind.domain.supervisor import ControlOwner, SupervisorAction, SupervisorDecision
from servicemind.domain.task import AgentName, RuntimeControl, Task, TaskPlan, TaskStatus
from servicemind.harness.executor import ControlledActionExecutor, controlled_executor
from servicemind.integrations.glpi.client import GlpiAPIError
from servicemind.model_gateway.contracts import ModelCallContext, ModelPurpose, ModelRisk
from servicemind.model_gateway.gateway import model_call_scope
from servicemind.observability.tracing import phase_span
from servicemind.orchestration.budget import BudgetExceeded, budget_controller
from servicemind.orchestration.dispatcher import TaskDispatcher, task_dispatcher
from servicemind.orchestration.phase5_governance import Phase5Governance, phase5_governance
from servicemind.orchestration.registry import agent_registry
from servicemind.orchestration.router import FastPathRouter, fast_path_router
from servicemind.orchestration.state import Phase3State
from servicemind.orchestration.supervisor_policy import (
    SupervisorPolicy,
    SupervisorPolicyError,
    supervisor_policy,
)
from servicemind.persistence.models import RunStatus
from servicemind.persistence.repository import ServiceMindRepository
from servicemind.runtime.contracts import AgentInvocationContext
from servicemind.security.auth import TenantContext


@dataclass(frozen=True)
class SupervisorRuntimeServices:
    router: FastPathRouter = fast_path_router
    supervisor: SupervisorAgent = supervisor_agent
    planner: DynamicPlanner = dynamic_planner
    policy: SupervisorPolicy = supervisor_policy
    dispatcher: TaskDispatcher = task_dispatcher
    data: DataAgent = data_agent
    knowledge: KnowledgeAgent = knowledge_agent
    analysis: AnalysisAgent = analysis_agent
    reviewer: ReviewerAgent = reviewer_agent
    action: ActionAgent = action_agent
    executor: ControlledActionExecutor = controlled_executor
    repository_factory: Callable[[UUID], Any] = ServiceMindRepository
    phase5: Phase5Governance = phase5_governance


def _context(state: Phase3State) -> TenantContext:
    return TenantContext(
        tenant_id=UUID(state["tenant_id"]),
        user_id=state["user_id"],
        username=state["username"],
        roles=set(state["roles"]),
        allowed_glpi_entity_ids=set(state["allowed_glpi_entity_ids"]),
    )


def _control(state: Phase3State) -> RuntimeControl:
    return RuntimeControl.model_validate(state.get("control", {}))


def _plan(state: Phase3State) -> TaskPlan:
    return TaskPlan.model_validate(state["task_plan"])


def _evidence(values: list[dict[str, Any]]) -> list[Evidence]:
    return [Evidence.model_validate(value) for value in values]


def _joined(state: Phase3State) -> JoinedEvidence:
    return JoinedEvidence.model_validate(state["joined_evidence"])


def _analysis(state: Phase3State) -> AnalysisResult:
    return AnalysisResult.model_validate(state["analysis_result"])


def _review(state: Phase3State) -> ReviewResult:
    return ReviewResult.model_validate(state["review_result"])


def _ready_by_agent(plan: TaskPlan, dispatcher: TaskDispatcher, agent: AgentName) -> list[Task]:
    return [task for task in dispatcher.ready_tasks(plan) if task.agent is agent]


def _invocation(state: Phase3State, task: Task) -> AgentInvocationContext:
    contract = agent_registry.get(task.agent)
    control = _control(state)
    remaining_model = max(_plan(state).budget.max_model_calls - control.model_call_count, 0)
    remaining_tools = max(_plan(state).budget.max_tool_calls - control.tool_call_count, 0)
    reserved_model = state.get("invocation_model_budget", remaining_model)
    reserved_tools = state.get("invocation_tool_budget", remaining_tools)
    return AgentInvocationContext(
        run_id=UUID(state["run_id"]),
        tenant_id=UUID(state["tenant_id"]),
        user_id=state["user_id"],
        task_id=task.task_id,
        trace_id=state.get("thread_id") or state["run_id"],
        deadline=min(task.deadline, _plan(state).budget.deadline),
        allowed_capabilities=frozenset(contract.allowed_tools),
        max_model_calls=min(2, remaining_model, reserved_model),
        max_tool_calls=min(6, remaining_tools, reserved_tools),
        policy_version="servicemind-agent-policy-v2",
    )


def _control_model_context(
    state: Phase3State,
    *,
    agent_role: str,
    purpose: ModelPurpose,
    risk: ModelRisk = ModelRisk.MEDIUM,
) -> ModelCallContext:
    return ModelCallContext(
        tenant_id=UUID(state["tenant_id"]),
        run_id=UUID(state["run_id"]),
        agent_role=agent_role,
        purpose=purpose,
        risk=risk,
        policy_version="servicemind-agent-policy-v2",
        prompt_version="2026-09-08",
        max_cost_usd=settings.SERVICEMIND_MODEL_MAX_COST_USD_PER_CALL,
    )


def build_supervisor_graph(services: SupervisorRuntimeServices | None = None):
    svc = services or SupervisorRuntimeServices()

    def repository(state: Phase3State):
        return svc.repository_factory(UUID(state["tenant_id"]))

    async def record_agent_result(state: Phase3State, envelope) -> None:
        repo = repository(state)
        await repo.append_event(
            UUID(state["run_id"]),
            "agent.completed",
            envelope.model_dump(mode="json", exclude={"output", "tool_invocations"}),
        )
        if hasattr(repo, "record_tool_invocation"):
            for record in envelope.tool_invocations:
                await repo.record_tool_invocation(
                    run_id=UUID(state["run_id"]),
                    tool_name=record.tool_name,
                    input_hash=record.input_hash,
                    status=record.status,
                    result_ref=record.result_ref,
                )

    async def route_node(state: Phase3State) -> dict[str, Any]:
        await repository(state).update_run(UUID(state["run_id"]), RunStatus.RUNNING)
        decision = svc.router.route(state["raw_request"], request_write=state["request_write"])
        control = _control(state)
        control.current_stage = "route"
        control.total_steps += 1
        await repository(state).append_event(
            UUID(state["run_id"]),
            "route.completed",
            {
                "route": decision.route.value,
                "reason_code": decision.reason_code,
            },
        )
        return {
            "route": decision.model_dump(mode="json"),
            "control": control.model_dump(mode="json"),
            "control_owner": ControlOwner.SUPERVISOR.value,
            "active_agent": "router",
            "trajectory": ["router"],
        }

    def after_route(
        state: Phase3State,
    ) -> Literal["fast_data", "fast_knowledge", "supervisor", "unsupported"]:
        route = RouteDecision.model_validate(state["route"]).route
        if route is RouteType.SIMPLE_DATA_QUERY:
            return "fast_data"
        if route is RouteType.SIMPLE_KNOWLEDGE_QUERY:
            return "fast_knowledge"
        if route is RouteType.COMPLEX_WORKFLOW:
            return "supervisor"
        return "unsupported"

    async def fast_data_node(state: Phase3State) -> dict[str, Any]:
        async with asyncio.timeout(20):
            evidence = await svc.data.get_ticket_evidence(_context(state), state["ticket_id"])
        result = {
            "route": state["route"],
            "evidence": [item.model_dump(mode="json") for item in evidence],
            "answer": evidence[0].metadata.get("ticket_facts", {}),
            "trajectory": ["router", "data"],
        }
        await repository(state).update_run(
            UUID(state["run_id"]), RunStatus.SUCCEEDED, result=result
        )
        await repository(state).append_event(
            UUID(state["run_id"]), "run.succeeded", {"route": "simple_data_query"}
        )
        return {
            "data_evidence": [item.model_dump(mode="json") for item in evidence],
            "final_result": result,
            "trajectory": ["data"],
        }

    async def fast_knowledge_node(state: Phase3State) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"tenant_id": UUID(state["tenant_id"]), "query": state["goal"]}
        if isinstance(svc.knowledge, KnowledgeAgent):
            kwargs.update(
                user_id=state["user_id"],
                entity_ids=set(state["allowed_glpi_entity_ids"]),
                group_ids=set(state.get("group_ids") or ()),
                profile_ids=set(state.get("profile_ids") or ()),
            )
            context_envelope = await svc.phase5.build_fast_knowledge_context(
                state=cast(dict[str, Any], state)
            )
            if context_envelope is not None:
                kwargs["model_query"] = json.dumps(
                    context_envelope.model_payload(), ensure_ascii=False
                )
        with model_call_scope(
            _control_model_context(
                state,
                agent_role="knowledge",
                purpose=ModelPurpose.RETRIEVAL_REWRITE,
                risk=ModelRisk.LOW,
            )
        ):
            evidence = await svc.knowledge.retrieve(**kwargs)
        result = {
            "route": state["route"],
            "evidence": [item.model_dump(mode="json") for item in evidence],
            "answer": [item.content for item in evidence],
            "trajectory": ["router", "knowledge"],
        }
        await repository(state).update_run(
            UUID(state["run_id"]), RunStatus.SUCCEEDED, result=result
        )
        await repository(state).append_event(
            UUID(state["run_id"]), "run.succeeded", {"route": "simple_knowledge_query"}
        )
        return {
            "knowledge_evidence": [item.model_dump(mode="json") for item in evidence],
            "final_result": result,
            "trajectory": ["knowledge"],
        }

    async def unsupported_node(state: Phase3State) -> dict[str, Any]:
        result = {
            "route": state["route"],
            "status": "rejected",
            "reason": "The requested operation is outside the safety policy.",
            "trajectory": ["router", "rejected"],
        }
        await repository(state).update_run(
            UUID(state["run_id"]), RunStatus.CANCELLED, result=result
        )
        await repository(state).append_event(
            UUID(state["run_id"]),
            "run.rejected",
            {"route": state["route"], "reason": result["reason"]},
        )
        return {
            "termination_code": "unsupported",
            "final_result": result,
            "trajectory": ["rejected"],
        }

    def supervisor_view(state: Phase3State) -> dict[str, Any]:
        plan = _plan(state) if state.get("task_plan") else None
        legal = svc.policy.legal_actions(cast(dict, state))
        ready = svc.dispatcher.ready_tasks(plan) if plan else []
        review = _review(state) if state.get("review_result") else None
        return {
            "goal": state["goal"],
            "request_write": state["request_write"],
            "control_owner": state.get("control_owner"),
            "legal_actions": sorted(item.value for item in legal),
            "ready_tasks": [
                {
                    "task_id": task.task_id,
                    "agent": task.agent.value,
                    "task_type": task.task_type,
                    "objective": task.task_input.get("objective"),
                }
                for task in ready
            ],
            "plan": (
                [
                    {
                        "task_id": task.task_id,
                        "agent": task.agent.value,
                        "task_type": task.task_type,
                        "status": task.status.value,
                        "depends_on": task.depends_on,
                    }
                    for task in plan.tasks
                ]
                if plan
                else None
            ),
            "max_parallel": plan.max_parallel if plan else 0,
            "evidence_dirty": state.get("evidence_dirty", False),
            "evidence_count": len(state.get("data_evidence", []))
            + len(state.get("knowledge_evidence", [])),
            "analysis": state.get("analysis_result"),
            "approval": state.get("approval"),
            "human_review": state.get("human_review"),
            "review": (
                {
                    "review_id": str(review.review_id),
                    "decision": review.decision.value,
                    "risk_level": review.risk_level.value,
                    "feedback": review.feedback,
                    "reason_codes": [item.reason_code for item in review.findings],
                    "policy_version": review.policy_version,
                    "degraded": review.degraded,
                }
                if review
                else None
            ),
            "control": state.get("control", {}),
            "termination_code": state.get("termination_code"),
        }

    async def supervisor_node(state: Phase3State) -> Command:
        control = _control(state)
        if state.get("task_plan"):
            try:
                budget_controller.check(_plan(state).budget, control)
            except BudgetExceeded as exc:
                return Command(
                    update={
                        "termination_code": exc.code.value,
                        "trajectory": ["supervisor", f"decision:{SupervisorAction.FINALIZE.value}"],
                    },
                    goto="finalize",
                )
        feedback = None
        decision = None
        for _ in range(2):
            control.model_call_count += 1
            with model_call_scope(
                _control_model_context(
                    state,
                    agent_role="supervisor",
                    purpose=ModelPurpose.CONTROL,
                )
            ):
                candidate = await svc.supervisor.decide(
                    supervisor_view(state), policy_feedback=feedback
                )
            try:
                svc.policy.validate(candidate, cast(dict, state))
            except SupervisorPolicyError as exc:
                feedback = str(exc)
                await repository(state).append_event(
                    UUID(state["run_id"]),
                    "supervisor.policy_rejected",
                    {
                        "action": candidate.action.value,
                        "selected_task_ids": candidate.selected_task_ids,
                        "reason": feedback,
                    },
                )
                continue
            decision = candidate
            break
        if decision is None:
            control.errors.append({"node": "supervisor", "error_type": "SupervisorPolicyError"})
            return Command(
                update={
                    "control": control.model_dump(mode="json"),
                    "termination_code": "supervisor_policy_failure",
                    "trajectory": ["supervisor", "policy_rejected"],
                },
                goto="finalize",
            )
        control.current_stage = f"supervisor:{decision.action.value}"
        control.total_steps += 1
        target = {
            SupervisorAction.PLAN: "plan",
            SupervisorAction.DISPATCH: "dispatch",
            SupervisorAction.JOIN_EVIDENCE: "join_evidence",
            SupervisorAction.ANALYZE: "analysis",
            SupervisorAction.REVIEW: "reviewer",
            SupervisorAction.RETRIEVE_MORE: "retrieve_more",
            SupervisorAction.REPLAN: "replan",
            SupervisorAction.HANDOFF_ACTION: "handoff",
            SupervisorAction.ESCALATE: "escalate",
            SupervisorAction.FINALIZE: "finalize",
        }[decision.action]
        await repository(state).append_event(
            UUID(state["run_id"]),
            "supervisor.decision",
            {
                "action": decision.action.value,
                "selected_task_ids": decision.selected_task_ids,
                "confidence": decision.confidence,
                "rationale_summary": decision.rationale_summary,
            },
        )
        return Command(
            update={
                "supervisor_decision": decision.model_dump(mode="json"),
                "control": control.model_dump(mode="json"),
                "control_owner": ControlOwner.SUPERVISOR.value,
                "active_agent": "supervisor",
                "trajectory": ["supervisor", f"decision:{decision.action.value}"],
            },
            goto=target,
        )

    async def plan_node(state: Phase3State) -> dict[str, Any]:
        correction = None
        task_plan = None
        attempts_used = 0
        for attempt in range(2):
            attempts_used += 1
            try:
                with model_call_scope(
                    _control_model_context(
                        state,
                        agent_role="planner",
                        purpose=ModelPurpose.PLANNING,
                    )
                ):
                    task_plan = await svc.planner.create_plan(
                        goal=state["goal"],
                        ticket_id=state["ticket_id"],
                        request_write=state["request_write"],
                        correction=correction,
                    )
                break
            except Exception as exc:
                correction = f"{type(exc).__name__}: {str(exc)[:1000]}"
                await repository(state).append_event(
                    UUID(state["run_id"]),
                    "planner.proposal_rejected",
                    {
                        "attempt": attempt + 1,
                        "error_type": type(exc).__name__,
                        "reason": str(exc)[:1000],
                    },
                )
        if task_plan is None:
            return Command(
                update={
                    "termination_code": "critical_error",
                    "control": _control(state).model_dump(mode="json"),
                    "trajectory": [
                        "planner",
                        f"decision:{SupervisorAction.FINALIZE.value}",
                    ],
                },
                goto="finalize",
            )
        await repository(state).append_event(
            UUID(state["run_id"]),
            "plan.validated",
            {
                "plan_id": str(task_plan.plan_id),
                "tasks": [
                    {"id": task.task_id, "agent": task.agent.value} for task in task_plan.tasks
                ],
            },
        )
        control = _control(state)
        control.model_call_count += attempts_used
        control.total_steps += 1
        return {
            "task_plan": task_plan.model_dump(mode="json", by_alias=True),
            "budget": task_plan.budget.model_dump(mode="json"),
            "plan_revision": 0,
            "evidence_dirty": False,
            "control": control.model_dump(mode="json"),
            "active_agent": "planner",
            "trajectory": ["planner", "dag_validator"],
        }

    async def dispatch_node(state: Phase3State) -> Command:
        plan = _plan(state)
        decision = SupervisorDecision.model_validate(state["supervisor_decision"])
        batch_id = str(uuid4())
        sends: list[Send] = []
        control = _control(state)
        remaining_model = max(plan.budget.max_model_calls - control.model_call_count, 0)
        remaining_tools = max(plan.budget.max_tool_calls - control.tool_call_count, 0)
        selected_count = len(decision.selected_task_ids)
        for index, task_id in enumerate(decision.selected_task_ids):
            slots_left = selected_count - index
            model_budget = min(2, (remaining_model + slots_left - 1) // slots_left)
            tool_budget = min(6, remaining_tools // slots_left)
            remaining_model -= model_budget
            remaining_tools -= tool_budget
            task = next(task for task in plan.tasks if task.task_id == task_id)
            plan = svc.dispatcher.transition(plan, task_id, TaskStatus.READY)
            plan = svc.dispatcher.transition(plan, task_id, TaskStatus.RUNNING)
            payload = {
                "run_id": state["run_id"],
                "tenant_id": state["tenant_id"],
                "user_id": state["user_id"],
                "username": state["username"],
                "roles": state["roles"],
                "allowed_glpi_entity_ids": state["allowed_glpi_entity_ids"],
                "group_ids": state.get("group_ids", []),
                "profile_ids": state.get("profile_ids", []),
                "ticket_id": state["ticket_id"],
                "goal": state["goal"],
                "thread_id": state.get("thread_id", state["run_id"]),
                "task_plan": plan.model_dump(mode="json", by_alias=True),
                "dispatch_batch_id": batch_id,
                "dispatch_task": task.model_dump(mode="json", by_alias=True),
                "invocation_model_budget": model_budget,
                "invocation_tool_budget": tool_budget,
                "joined_evidence": state.get("joined_evidence", {}),
                "review_result": state.get("review_result", {}),
                "control": state.get("control", {}),
            }
            node = "data_task" if task.agent is AgentName.DATA else "knowledge_task"
            sends.append(Send(node, payload))
        return Command(
            update={
                "task_plan": plan.model_dump(mode="json", by_alias=True),
                "dispatch_batch_id": batch_id,
                "active_agent": "dispatcher",
                "trajectory": ["dispatcher"],
            },
            goto=sends,
        )

    async def data_task_node(state: Phase3State) -> dict[str, Any]:
        task = Task.model_validate(state["dispatch_task"])
        started = time.perf_counter()
        evidence = []
        error = None
        envelope = None
        attempts_used = 0
        tool_budget = max(state.get("invocation_tool_budget", 2), 0)
        attempts_allowed = (
            0 if tool_budget == 0 else 1 if hasattr(svc.data, "run") else min(2, tool_budget)
        )
        if attempts_allowed == 0:
            error = RuntimeError("Data task has no reserved tool budget")
        for attempt in range(attempts_allowed):
            attempts_used += 1
            try:
                async with asyncio.timeout(20):
                    if hasattr(svc.data, "run"):
                        invocation = _invocation(state, task)
                        context_envelope = await svc.phase5.build_context(
                            state=cast(dict[str, Any], state),
                            task=task,
                            invocation=invocation,
                            agent=ContextAgent.DATA,
                        )
                        run_kwargs: dict[str, Any] = {
                            "invocation": invocation,
                            "tenant_context": _context(state),
                            "objective": str(task.task_input.get("objective") or state["goal"]),
                            "ticket_id": state["ticket_id"],
                        }
                        if isinstance(svc.data, DataAgent):
                            run_kwargs["context_envelope"] = context_envelope
                        with model_call_scope(
                            ModelCallContext.from_invocation(
                                invocation,
                                agent_role="data",
                                purpose=ModelPurpose.DATA_PLANNING,
                            )
                        ):
                            envelope = await svc.data.run(**run_kwargs)
                        evidence = envelope.output
                        if (
                            envelope.metrics.model_calls > state.get("invocation_model_budget", 0)
                            or envelope.metrics.tool_calls > tool_budget
                        ):
                            evidence = []
                            error = RuntimeError("Data Agent exceeded its reserved budget")
                            break
                    else:
                        evidence = await svc.data.get_ticket_evidence(
                            _context(state), state["ticket_id"]
                        )
                error = None
                break
            except (GlpiAPIError, TimeoutError) as exc:
                error = exc
                if attempt + 1 < attempts_allowed:
                    await asyncio.sleep(0.25)
            except Exception as exc:
                error = exc
                break
        if envelope is not None:
            await record_agent_result(state, envelope)
        completion = {
            "batch_id": state["dispatch_batch_id"],
            "task_id": task.task_id,
            "status": "failed" if error else "success",
            "error_type": type(error).__name__ if error else None,
            "output_refs": [item.evidence_id for item in evidence],
            "model_calls": envelope.metrics.model_calls if envelope else 0,
            "tool_calls": envelope.metrics.tool_calls if envelope else attempts_used,
            "attempts": envelope.metrics.attempts if envelope else attempts_used,
            "agent_status": (
                envelope.status.value if envelope else "degraded" if error else "succeeded"
            ),
        }
        return {
            "data_evidence": [item.model_dump(mode="json") for item in evidence],
            "task_completions": [completion],
            "branch_errors": ([completion] if error else []),
            "branch_timings": [
                {
                    "agent": "data",
                    "task_id": task.task_id,
                    "batch_id": state["dispatch_batch_id"],
                    "started": started,
                    "finished": time.perf_counter(),
                }
            ],
            "agent_invocations": (
                [envelope.model_dump(mode="json", exclude={"output"})] if envelope else []
            ),
            "trajectory": [f"data:{task.task_id}"],
        }

    async def knowledge_task_node(state: Phase3State) -> dict[str, Any]:
        """One knowledge DAG task: bounded retrieval with retry and observability.

        Mirrors ``data_task_node`` so a knowledge fault cannot hang the graph: the
        call runs inside ``asyncio.timeout`` with one retry, and every outcome is
        recorded as a completion (and, when a genuine subagent envelope exists, as an
        ``agent.completed`` event). The retrieval principal is forwarded in full --
        user, GLPI entity, and the optional group/profile ACL -- so RLS scoping of
        candidate chunks is identical to the requesting analyst's context.
        """
        task = Task.model_validate(state["dispatch_task"])
        started = time.perf_counter()
        query = f"{state['goal']} {task.task_input.get('objective', '')}"
        if state.get("joined_evidence"):
            query += " " + " ".join(
                item.content
                for item in JoinedEvidence.model_validate(state["joined_evidence"]).items
            )
        if state.get("review_result"):
            query += " " + _review(state).feedback
        kwargs: dict[str, Any] = {
            "tenant_id": UUID(state["tenant_id"]),
            "query": query,
            "retrieval_round": _control(state).retrieval_round,
        }
        if isinstance(svc.knowledge, KnowledgeAgent):
            kwargs.update(
                user_id=state["user_id"],
                entity_ids=set(state["allowed_glpi_entity_ids"]),
                group_ids=set(state.get("group_ids") or ()),
                profile_ids=set(state.get("profile_ids") or ()),
                use_query_model=state.get("invocation_model_budget", 0) > 0,
            )
        invocation = _invocation(state, task)
        context_envelope = await svc.phase5.build_context(
            state=cast(dict[str, Any], state),
            task=task,
            invocation=invocation,
            agent=ContextAgent.KNOWLEDGE,
        )
        if context_envelope is not None and isinstance(svc.knowledge, KnowledgeAgent):
            kwargs["model_query"] = json.dumps(context_envelope.model_payload(), ensure_ascii=False)
        evidence: list[Evidence] = []
        use_query_model = bool(kwargs.get("use_query_model", False))
        attempts_used = 0
        tool_budget = max(state.get("invocation_tool_budget", 2), 0)
        model_budget = max(state.get("invocation_model_budget", 0), 0)
        attempts_allowed = min(2, tool_budget, model_budget if use_query_model else tool_budget)
        error: BaseException | None = (
            RuntimeError("Knowledge task has no reserved tool budget")
            if attempts_allowed == 0
            else None
        )
        for attempt in range(attempts_allowed):
            attempts_used += 1
            try:
                async with asyncio.timeout(20):
                    with model_call_scope(
                        ModelCallContext.from_invocation(
                            invocation,
                            agent_role="knowledge",
                            purpose=ModelPurpose.RETRIEVAL_REWRITE,
                            risk=ModelRisk.LOW,
                        )
                    ):
                        evidence = await svc.knowledge.retrieve(**kwargs)
                error = None
                break
            except TimeoutError as exc:
                error = exc
                if attempt + 1 < attempts_allowed:
                    await asyncio.sleep(0.25)
            except Exception as exc:
                error = exc
                break
        model_calls = attempts_used if use_query_model else 0
        completion = {
            "batch_id": state["dispatch_batch_id"],
            "task_id": task.task_id,
            "status": "failed" if error else "success",
            "error_type": type(error).__name__ if error else None,
            "output_refs": [item.evidence_id for item in evidence],
            "model_calls": model_calls,
            "tool_calls": attempts_used,
            "attempts": attempts_used,
            "agent_status": "degraded" if error else "succeeded",
        }
        await repository(state).append_event(
            UUID(state["run_id"]),
            "agent.completed",
            {
                "agent_name": "knowledge",
                "task_id": task.task_id,
                "status": "degraded" if error else "succeeded",
                "evidence_refs": completion["output_refs"],
                "metrics": {
                    "model_calls": model_calls,
                    "tool_calls": attempts_used,
                    "attempts": attempts_used,
                    "latency_ms": (time.perf_counter() - started) * 1000,
                },
                "policy_version": "servicemind-agent-policy-v2",
            },
        )
        return {
            "knowledge_evidence": [item.model_dump(mode="json") for item in evidence],
            "task_completions": [completion],
            "branch_errors": ([completion] if error else []),
            "branch_timings": [
                {
                    "agent": "knowledge",
                    "task_id": task.task_id,
                    "batch_id": state["dispatch_batch_id"],
                    "started": started,
                    "finished": time.perf_counter(),
                }
            ],
            "trajectory": [f"knowledge:{task.task_id}"],
        }

    async def dispatch_barrier_node(state: Phase3State) -> dict[str, Any]:
        plan = _plan(state)
        batch = state["dispatch_batch_id"]
        completions = [
            item for item in state.get("task_completions", []) if item["batch_id"] == batch
        ]
        for completion in completions:
            status = TaskStatus.SUCCESS if completion["status"] == "success" else TaskStatus.FAILED
            plan = svc.dispatcher.transition(
                plan,
                completion["task_id"],
                status,
                output_ref=",".join(completion["output_refs"]) or "none",
            )
        failures = [item for item in completions if item["status"] == "failed"]
        control = _control(state)
        control.total_steps += len(completions)
        control.tool_call_count += sum(item.get("tool_calls", 1) for item in completions)
        control.model_call_count += sum(item.get("model_calls", 0) for item in completions)
        if failures:
            control.consecutive_failures += len(failures)
            control.errors.extend(
                {
                    "node": item["task_id"],
                    "error_type": item["error_type"],
                }
                for item in failures
            )
            review = ReviewResult(
                decision=ReviewDecision.REPLAN,
                conflicts=[f"Task {item['task_id']} failed" for item in failures],
                risk_level=RiskLevel.MEDIUM,
                feedback="One or more evidence tasks failed and require a revised plan.",
            )
        else:
            review = None
            control.consecutive_failures = 0
        return {
            "task_plan": plan.model_dump(mode="json", by_alias=True),
            "evidence_dirty": not failures,
            "review_result": review.model_dump(mode="json") if review else {},
            "control": control.model_dump(mode="json"),
            "control_owner": ControlOwner.SUPERVISOR.value,
            "active_agent": "supervisor",
            "trajectory": ["dispatch_barrier"],
        }

    async def join_node(state: Phase3State) -> dict[str, Any]:
        values = _evidence(state.get("data_evidence", [])) + _evidence(
            state.get("knowledge_evidence", [])
        )
        joined = join_evidence(UUID(state["tenant_id"]), values)
        control = _control(state)
        control.total_steps += 1
        await repository(state).append_event(
            UUID(state["run_id"]),
            "evidence.joined",
            {"count": len(joined.items), "evidence_refs": joined.evidence_refs},
        )
        return {
            "joined_evidence": joined.model_dump(mode="json"),
            "evidence_dirty": False,
            "control": control.model_dump(mode="json"),
            "control_owner": ControlOwner.SUPERVISOR.value,
            "active_agent": "supervisor",
            "trajectory": ["evidence_join"],
        }

    async def analysis_node(state: Phase3State) -> dict[str, Any]:
        plan = _plan(state)
        tasks = _ready_by_agent(plan, svc.dispatcher, AgentName.ANALYSIS)
        if not tasks:
            raise RuntimeError("Supervisor selected ANALYZE without a ready task")
        selected_id = SupervisorDecision.model_validate(
            state["supervisor_decision"]
        ).selected_task_ids[0]
        task = next(item for item in tasks if item.task_id == selected_id)
        plan = svc.dispatcher.transition(plan, task.task_id, TaskStatus.READY)
        plan = svc.dispatcher.transition(plan, task.task_id, TaskStatus.RUNNING)
        envelope = None
        invocation = _invocation(state, task)
        context_envelope = await svc.phase5.build_context(
            state=cast(dict[str, Any], state),
            task=task,
            invocation=invocation,
            agent=ContextAgent.ANALYSIS,
        )
        with phase_span(
            "servicemind.supervisor.analysis", **{"gen_ai.agent.name": "analysis-agent"}
        ):
            if hasattr(svc.analysis, "run"):
                run_kwargs: dict[str, Any] = {
                    "invocation": invocation,
                    "evidence": _joined(state),
                    "goal": state["goal"],
                    "request_write": state["request_write"],
                    "ticket_id": state["ticket_id"],
                }
                if isinstance(svc.analysis, AnalysisAgent):
                    run_kwargs["context_envelope"] = context_envelope
                with model_call_scope(
                    ModelCallContext.from_invocation(
                        invocation,
                        agent_role="analysis",
                        purpose=ModelPurpose.ANALYSIS,
                    )
                ):
                    envelope = await svc.analysis.run(**run_kwargs)
                result = envelope.output
            else:
                result = await svc.analysis.analyze_evidence(
                    _joined(state),
                    state["goal"],
                    request_write=state["request_write"],
                    ticket_id=state["ticket_id"],
                )
        if envelope is not None:
            await record_agent_result(state, envelope)
        plan = svc.dispatcher.transition(
            plan, task.task_id, TaskStatus.SUCCESS, output_ref="analysis_result"
        )
        control = _control(state)
        control.model_call_count += envelope.metrics.model_calls if envelope else 1
        control.total_steps += 1
        return {
            "task_plan": plan.model_dump(mode="json", by_alias=True),
            "analysis_result": result.model_dump(mode="json"),
            "review_result": {},
            "control": control.model_dump(mode="json"),
            "control_owner": ControlOwner.SUPERVISOR.value,
            "active_agent": "analysis",
            "agent_invocations": (
                [envelope.model_dump(mode="json", exclude={"output"})] if envelope else []
            ),
            "trajectory": [f"analysis:{task.task_id}"],
        }

    async def reviewer_node(state: Phase3State) -> dict[str, Any]:
        plan = _plan(state)
        tasks = _ready_by_agent(plan, svc.dispatcher, AgentName.REVIEWER)
        if not tasks:
            raise RuntimeError("Supervisor selected REVIEW without a ready task")
        selected_id = SupervisorDecision.model_validate(
            state["supervisor_decision"]
        ).selected_task_ids[0]
        task = next(item for item in tasks if item.task_id == selected_id)
        plan = svc.dispatcher.transition(plan, task.task_id, TaskStatus.READY)
        plan = svc.dispatcher.transition(plan, task.task_id, TaskStatus.RUNNING)
        control = _control(state)
        envelope = None
        invocation = _invocation(state, task)
        context_envelope = await svc.phase5.build_context(
            state=cast(dict[str, Any], state),
            task=task,
            invocation=invocation,
            agent=ContextAgent.REVIEWER,
        )
        if hasattr(svc.reviewer, "run"):
            run_kwargs: dict[str, Any] = {
                "invocation": invocation,
                "analysis": _analysis(state),
                "evidence": _joined(state),
                "request_write": state["request_write"],
                "retrieval_round": control.retrieval_round,
                "replan_count": control.replan_count,
                "max_replans": plan.max_replans,
            }
            if isinstance(svc.reviewer, ReviewerAgent):
                run_kwargs["context_envelope"] = context_envelope
            with model_call_scope(
                ModelCallContext.from_invocation(
                    invocation,
                    agent_role="reviewer",
                    purpose=ModelPurpose.REVIEW,
                    risk=ModelRisk.HIGH,
                )
            ):
                envelope = await svc.reviewer.run(**run_kwargs)
            result = envelope.output
        else:
            result = await svc.reviewer.review(
                analysis=_analysis(state),
                evidence=_joined(state),
                request_write=state["request_write"],
                retrieval_round=control.retrieval_round,
                replan_count=control.replan_count,
                max_replans=plan.max_replans,
            )
        if envelope is not None:
            await record_agent_result(state, envelope)
        plan = svc.dispatcher.transition(
            plan, task.task_id, TaskStatus.SUCCESS, output_ref="review_result"
        )
        await repository(state).append_event(
            UUID(state["run_id"]),
            "review.completed",
            {
                "decision": result.decision.value,
                "feedback": result.feedback,
                "review_id": str(result.review_id),
                "policy_version": result.policy_version,
                "reason_codes": [finding.reason_code for finding in result.findings],
            },
        )
        control.model_call_count += envelope.metrics.model_calls if envelope else 0
        control.total_steps += 1
        return {
            "task_plan": plan.model_dump(mode="json", by_alias=True),
            "review_result": result.model_dump(mode="json"),
            "control": control.model_dump(mode="json"),
            "control_owner": ControlOwner.SUPERVISOR.value,
            "active_agent": "reviewer",
            "agent_invocations": (
                [envelope.model_dump(mode="json", exclude={"output"})] if envelope else []
            ),
            "trajectory": [f"reviewer:{task.task_id}", f"review:{result.decision.value}"],
        }

    async def revise_node(state: Phase3State, *, retrieve_more: bool) -> dict[str, Any]:
        control = _control(state)
        if retrieve_more:
            control.retrieval_round += 1
        else:
            control.replan_count += 1
        try:
            budget_controller.check(_plan(state).budget, control)
        except BudgetExceeded as exc:
            # Same terminal handling as the Supervisor node: an exhausted replan /
            # retrieval budget ends the run with a persisted termination code rather
            # than letting the exception escape the sub-node and error the graph.
            return Command(
                update={
                    "termination_code": exc.code.value,
                    "control": control.model_dump(mode="json"),
                    "trajectory": [
                        "retrieve_more" if retrieve_more else "replan",
                        f"decision:{SupervisorAction.FINALIZE.value}",
                    ],
                },
                goto="finalize",
            )
        # A Supervisor-triggered REPLAN may legally arrive before any review has run
        # (e.g. evidence was gathered but is still un-joined). Tolerate that instead
        # of crashing on state["review_result"] == {}.
        review_payload = state.get("review_result") or {}
        review = ReviewResult.model_validate(review_payload) if review_payload else None
        correction = None
        revised = None
        attempts_used = 0
        for attempt in range(2):
            attempts_used += 1
            try:
                with model_call_scope(
                    _control_model_context(
                        state,
                        agent_role="planner",
                        purpose=ModelPurpose.PLANNING,
                    )
                ):
                    revised = await svc.planner.revise_plan(
                        previous=_plan(state),
                        review=review,
                        ticket_id=state["ticket_id"],
                        request_write=state["request_write"],
                        correction=correction,
                    )
                break
            except Exception as exc:
                correction = f"{type(exc).__name__}: {str(exc)[:1000]}"
                await repository(state).append_event(
                    UUID(state["run_id"]),
                    "replanner.proposal_rejected",
                    {
                        "attempt": attempt + 1,
                        "error_type": type(exc).__name__,
                        "reason": str(exc)[:1000],
                    },
                )
        if revised is None:
            # Double replan validation failure is a run-level policy failure: persist
            # it through the normal finalizer instead of raising a raw RuntimeError.
            return Command(
                update={
                    "termination_code": "critical_error",
                    "analysis_result": {},
                    "review_result": {},
                    "control": control.model_dump(mode="json"),
                    "trajectory": [
                        "retrieve_more" if retrieve_more else "replan",
                        f"decision:{SupervisorAction.FINALIZE.value}",
                    ],
                },
                goto="finalize",
            )
        control.model_call_count += attempts_used
        control.total_steps += 1
        return {
            "task_plan": revised.model_dump(mode="json", by_alias=True),
            "plan_revision": state.get("plan_revision", 0) + 1,
            "analysis_result": {},
            "review_result": {},
            "evidence_dirty": False,
            "control": control.model_dump(mode="json"),
            "control_owner": ControlOwner.SUPERVISOR.value,
            "active_agent": "replanner",
            "trajectory": ["retrieve_more" if retrieve_more else "replan", "dag_validator"],
        }

    async def retrieve_more_node(state: Phase3State) -> dict[str, Any]:
        return await revise_node(state, retrieve_more=True)

    async def replan_node(state: Phase3State) -> dict[str, Any]:
        return await revise_node(state, retrieve_more=False)

    async def handoff_node(state: Phase3State) -> Command:
        if _review(state).decision is not ReviewDecision.PASSED:
            raise PermissionError("Supervisor cannot hand off without PASSED review")
        plan = _plan(state)
        if not _ready_by_agent(plan, svc.dispatcher, AgentName.ACTION):
            raise RuntimeError("No ready Action task exists")
        envelope = HandoffEnvelope(
            run_id=UUID(state["run_id"]),
            tenant_id=UUID(state["tenant_id"]),
            user_id=state["user_id"],
            evidence_refs=_joined(state).evidence_refs,
            review_result=_review(state),
            allowed_operations=["append_ticket_followup"],
            risk_level=_review(state).risk_level,
            remaining_budget=budget_controller.snapshot(plan.budget, _control(state)),
            idempotency_context={"run_id": state["run_id"]},
            handoff_reason=SupervisorDecision.model_validate(
                state["supervisor_decision"]
            ).rationale_summary,
        )
        await repository(state).append_event(
            UUID(state["run_id"]),
            "control.handoff",
            {
                "from": ControlOwner.SUPERVISOR.value,
                "to": ControlOwner.ACTION.value,
                "allowed_operations": envelope.allowed_operations,
                "review_digest": envelope.review_digest,
                "evidence_digest": envelope.evidence_digest,
                "expires_at": envelope.expires_at.isoformat() if envelope.expires_at else None,
            },
        )
        return Command(
            update={
                "handoff_envelope": envelope.model_dump(mode="json"),
                "control_owner": ControlOwner.ACTION.value,
                "active_agent": "action",
                "trajectory": ["handoff:supervisor->action"],
            },
            goto="action",
        )

    async def action_node(state: Phase3State) -> dict[str, Any]:
        if state.get("control_owner") != ControlOwner.ACTION.value:
            raise PermissionError("Action Agent does not own control")
        plan = _plan(state)
        task = _ready_by_agent(plan, svc.dispatcher, AgentName.ACTION)[0]
        plan = svc.dispatcher.transition(plan, task.task_id, TaskStatus.READY)
        plan = svc.dispatcher.transition(plan, task.task_id, TaskStatus.RUNNING)
        await svc.phase5.build_context(
            state=cast(dict[str, Any], state),
            task=task,
            invocation=_invocation(state, task),
            agent=ContextAgent.ACTION,
        )
        intent = svc.action.propose_from_handoff(
            HandoffEnvelope.model_validate(state["handoff_envelope"]),
            _analysis(state),
            ticket_id=state["ticket_id"],
        )
        record = await repository(state).save_action_intent(
            run_id=UUID(state["run_id"]),
            action_type=intent.action_type,
            target_id=intent.target_id,
            arguments=intent.arguments,
            risk_level=intent.risk_level,
            action_hash=intent.action_hash,
            intent_version=intent.intent_version,
            policy_version=intent.policy_version,
            review_digest=intent.review_digest,
            evidence_digest=intent.evidence_digest,
            evidence_refs=intent.evidence_refs,
            idempotency_context=intent.idempotency_context,
            requested_by=intent.requested_by,
            expires_at=intent.expires_at,
            dry_run_preview=intent.dry_run_preview,
        )
        intent.id = record.id
        plan = svc.dispatcher.transition(
            plan, task.task_id, TaskStatus.SUCCESS, output_ref=str(record.id)
        )
        await repository(state).update_run(UUID(state["run_id"]), RunStatus.WAITING_APPROVAL)
        await repository(state).append_event(
            UUID(state["run_id"]),
            "approval.required",
            {
                "agent": "action",
                "action_intent_id": str(record.id),
                "action_type": intent.action_type,
                "action_hash": intent.action_hash,
                "risk_level": intent.risk_level,
            },
        )
        return {
            "task_plan": plan.model_dump(mode="json", by_alias=True),
            "action_intent": intent.model_dump(mode="json"),
            "control_owner": ControlOwner.HUMAN.value,
            "active_agent": "approval",
            "agent_invocations": [
                {
                    "agent_name": "action",
                    "task_id": task.task_id,
                    "status": "succeeded",
                    "evidence_refs": intent.evidence_refs,
                    "metrics": {
                        "model_calls": 0,
                        "tool_calls": 0,
                        "attempts": 1,
                        "latency_ms": 0,
                    },
                    "model_name": None,
                    "prompt_version": "deterministic-compiler-v2",
                    "policy_version": intent.policy_version,
                    "failure_code": None,
                    "failure_detail": None,
                }
            ],
            "trajectory": [f"action:{task.task_id}"],
        }

    def approval_node(state: Phase3State) -> dict[str, Any]:
        decision = interrupt({"type": "approval_required", "action_intent": state["action_intent"]})
        return {
            "approval": ApprovalDecision.model_validate(decision).model_dump(),
            "control_owner": ControlOwner.HARNESS.value,
            "active_agent": "harness",
            "trajectory": ["approval"],
        }

    def after_approval(state: Phase3State) -> Literal["execute", "finalize"]:
        return "execute" if state["approval"]["decision"] == "approved" else "finalize"

    async def execute_node(state: Phase3State) -> dict[str, Any]:
        if state.get("control_owner") != ControlOwner.HARNESS.value:
            raise PermissionError("Harness does not own control")
        result = await svc.executor.execute(
            _context(state), ActionIntent.model_validate(state["action_intent"])
        )
        await repository(state).append_event(
            UUID(state["run_id"]), "execution.verified", result.model_dump(mode="json")
        )
        return {
            "execution_result": result.model_dump(mode="json"),
            "control_owner": ControlOwner.NONE.value,
            "active_agent": "finalize",
            "trajectory": ["phase2_harness", "execute", "read_back_verify"],
        }

    async def escalate_node(state: Phase3State) -> dict[str, Any]:
        await repository(state).update_run(UUID(state["run_id"]), RunStatus.WAITING_REVIEW)
        resolution = interrupt(
            {
                "type": "review_escalation",
                "review_result": state.get("review_result"),
                "allowed_decisions": ["continue", "stop"],
            }
        )
        return {
            "human_review": cast(dict[str, Any], resolution),
            "control_owner": ControlOwner.SUPERVISOR.value,
            "active_agent": "supervisor",
            "trajectory": ["human_escalation"],
        }

    def after_escalation(state: Phase3State) -> Literal["supervisor", "finalize"]:
        return "supervisor" if state["human_review"].get("decision") == "continue" else "finalize"

    async def finalize_node(state: Phase3State) -> dict[str, Any]:
        failed = state.get("termination_code") in {
            "critical_error",
            "supervisor_policy_failure",
        }
        cancelled = (
            state.get("termination_code") is not None
            or state.get("approval", {}).get("decision") == "rejected"
            or state.get("human_review", {}).get("decision") == "stop"
            or state.get("review_result", {}).get("decision") == "reject"
        )
        # Evidence-insufficiency abstention is a *successful* terminal: the platform
        # fulfilled its duty by refusing to fabricate, so the run is SUCCEEDED but the
        # persisted review carries decision="abstain" and the event below is distinct
        # (run.abstained) so metrics can count abstention-correctness as first-class.
        abstained = state.get("review_result", {}).get("decision") == "abstain"
        # A human who answered "continue" on an escalation accepts the reviewer's
        # blocked outcome as the run's result. The run SUCCEEDS without executing any
        # action, and is recorded under a distinct event so metrics can count
        # human-resolved escalations -- it is not a pass and not a silent cancel.
        escalation_accepted = state.get("human_review", {}).get("decision") == "continue"
        status = (
            RunStatus.FAILED
            if failed
            else RunStatus.CANCELLED
            if cancelled
            else RunStatus.SUCCEEDED
        )
        plan = _plan(state) if state.get("task_plan") else None
        if (cancelled or abstained or escalation_accepted) and plan:
            plan = svc.dispatcher.cancel_remaining(plan)
        result = {
            "route": state.get("route"),
            "task_plan": plan.model_dump(mode="json", by_alias=True) if plan else None,
            "evidence": state.get("joined_evidence"),
            "analysis": state.get("analysis_result"),
            "review": state.get("review_result"),
            "supervisor_decision": state.get("supervisor_decision"),
            "plan_revision": state.get("plan_revision", 0),
            "handoff": state.get("handoff_envelope"),
            "control_owner": state.get("control_owner"),
            "action_intent": state.get("action_intent"),
            "approval": state.get("approval"),
            "human_review": state.get("human_review"),
            "execution": state.get("execution_result"),
            "control": state.get("control"),
            "termination_code": state.get("termination_code"),
            "branch_timings": state.get("branch_timings", []),
            "agent_invocations": state.get("agent_invocations", []),
            "final_state_verified": status is RunStatus.SUCCEEDED,
            "trajectory": state.get("trajectory", []) + ["finalize"],
        }
        await repository(state).update_run(UUID(state["run_id"]), status, result=result)
        await repository(state).append_event(
            UUID(state["run_id"]),
            "run.abstained"
            if abstained
            else "run.escalation_accepted"
            if escalation_accepted
            else f"run.{status.value}",
            {
                "status": status.value,
                "decision": (
                    "abstain" if abstained else "human_continue" if escalation_accepted else None
                ),
            },
        )
        try:
            memory_count = await svc.phase5.post_run(
                state=cast(dict[str, Any], state),
                result=result,
                status=status.value,
            )
            if memory_count:
                await repository(state).append_event(
                    UUID(state["run_id"]),
                    "memory.post_run_completed",
                    {"record_count": memory_count},
                )
        except Exception as exc:
            await repository(state).append_event(
                UUID(state["run_id"]),
                "memory.post_run_failed",
                {"error_type": type(exc).__name__},
            )
        return {"final_result": result, "trajectory": ["finalize"]}

    graph = StateGraph(Phase3State)
    graph.add_node("route", route_node)
    graph.add_node("fast_data", fast_data_node)
    graph.add_node("fast_knowledge", fast_knowledge_node)
    graph.add_node("unsupported", unsupported_node)
    graph.add_node(
        "supervisor",
        supervisor_node,
        destinations=(
            "plan",
            "dispatch",
            "join_evidence",
            "analysis",
            "reviewer",
            "retrieve_more",
            "replan",
            "handoff",
            "escalate",
            "finalize",
        ),
    )
    graph.add_node("plan", plan_node)
    graph.add_node("dispatch", dispatch_node, destinations=("data_task", "knowledge_task"))
    graph.add_node("data_task", data_task_node)
    graph.add_node("knowledge_task", knowledge_task_node)
    graph.add_node("dispatch_barrier", dispatch_barrier_node)
    graph.add_node("join_evidence", join_node)
    graph.add_node("analysis", analysis_node)
    graph.add_node("reviewer", reviewer_node)
    graph.add_node("retrieve_more", retrieve_more_node)
    graph.add_node("replan", replan_node)
    graph.add_node("handoff", handoff_node, destinations=("action",))
    graph.add_node("action", action_node)
    graph.add_node("approval", approval_node)
    graph.add_node("execute", execute_node)
    graph.add_node("escalate", escalate_node)
    graph.add_node("finalize", finalize_node)
    graph.add_edge(START, "route")
    graph.add_conditional_edges("route", after_route)
    graph.add_edge("fast_data", END)
    graph.add_edge("fast_knowledge", END)
    graph.add_edge("unsupported", END)
    graph.add_edge("plan", "supervisor")
    graph.add_edge("data_task", "dispatch_barrier")
    graph.add_edge("knowledge_task", "dispatch_barrier")
    graph.add_edge("dispatch_barrier", "supervisor")
    graph.add_edge("join_evidence", "supervisor")
    graph.add_edge("analysis", "supervisor")
    graph.add_edge("reviewer", "supervisor")
    graph.add_edge("retrieve_more", "supervisor")
    graph.add_edge("replan", "supervisor")
    graph.add_edge("action", "approval")
    graph.add_conditional_edges("approval", after_approval)
    graph.add_edge("execute", "finalize")
    graph.add_conditional_edges("escalate", after_escalation)
    graph.add_edge("finalize", END)
    return graph.compile()


supervisor_graph = build_supervisor_graph()


def configure_supervisor_checkpointer(checkpointer: object) -> None:
    supervisor_graph.checkpointer = checkpointer  # type: ignore[assignment]
