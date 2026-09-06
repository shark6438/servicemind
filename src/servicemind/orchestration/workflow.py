from typing import Literal
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from servicemind.agents.action import action_agent
from servicemind.agents.analysis import analysis_agent
from servicemind.agents.data import data_agent
from servicemind.domain.models import ActionIntent, ApprovalDecision, TicketAnalysis
from servicemind.harness.executor import controlled_executor
from servicemind.observability.tracing import phase_span
from servicemind.orchestration.state import Phase2State
from servicemind.persistence.models import RunStatus
from servicemind.persistence.repository import ServiceMindRepository
from servicemind.security.auth import TenantContext


def _context(state: Phase2State) -> TenantContext:
    return TenantContext(
        tenant_id=UUID(state["tenant_id"]),
        user_id=state["user_id"],
        username=state["username"],
        roles=set(state["roles"]),
        allowed_glpi_entity_ids=set(state["allowed_glpi_entity_ids"]),
    )


async def data_node(state: Phase2State) -> Phase2State:
    run_id = UUID(state["run_id"])
    repository = ServiceMindRepository(UUID(state["tenant_id"]))
    await repository.update_run(run_id, RunStatus.RUNNING)
    with phase_span(
        "servicemind.data_agent",
        **{
            "gen_ai.agent.name": "data-agent",
            "servicemind.tenant.id": state["tenant_id"],
            "servicemind.run.id": state["run_id"],
            "itsm.ticket.id": state["ticket_id"],
        },
    ):
        facts = await data_agent.get_ticket_facts(_context(state), state["ticket_id"])
    await repository.append_event(
        run_id, "data.completed", {"ticket_id": state["ticket_id"], "agent": "data"}
    )
    return {"ticket_facts": facts}


async def analysis_node(state: Phase2State) -> Phase2State:
    run_id = UUID(state["run_id"])
    repository = ServiceMindRepository(UUID(state["tenant_id"]))
    with phase_span(
        "servicemind.analysis_agent",
        **{
            "gen_ai.agent.name": "analysis-agent",
            "servicemind.tenant.id": state["tenant_id"],
            "servicemind.run.id": state["run_id"],
        },
    ):
        analysis = await analysis_agent.analyze(state["ticket_facts"], state["goal"])
    await repository.append_event(
        run_id,
        "analysis.completed",
        {
            "agent": "analysis",
            "category": analysis.category,
            "priority": analysis.recommended_priority,
            "confidence": analysis.confidence,
            "source": analysis.source,
        },
    )
    return {"analysis": analysis.model_dump()}


def after_analysis(state: Phase2State) -> Literal["action", "finalize"]:
    return "action" if state["request_write"] else "finalize"


async def action_node(state: Phase2State) -> Phase2State:
    run_id = UUID(state["run_id"])
    tenant_id = UUID(state["tenant_id"])
    repository = ServiceMindRepository(tenant_id)
    intent = action_agent.propose_followup(
        run_id,
        state["ticket_id"],
        TicketAnalysis.model_validate(state["analysis"]),
    )
    record = await repository.save_action_intent(
        run_id=run_id,
        action_type=intent.action_type,
        target_id=intent.target_id,
        arguments=intent.arguments,
        risk_level=intent.risk_level,
        action_hash=intent.action_hash,
    )
    intent.id = record.id
    await repository.update_run(run_id, RunStatus.WAITING_APPROVAL)
    await repository.append_event(
        run_id,
        "approval.required",
        {
            "agent": "action",
            "action_intent_id": str(record.id),
            "action_type": intent.action_type,
            "action_hash": intent.action_hash,
            "risk_level": intent.risk_level,
        },
    )
    return {"action_intent": intent.model_dump(mode="json")}


def approval_node(state: Phase2State) -> Phase2State:
    decision = interrupt(
        {
            "type": "approval_required",
            "action_intent": state["action_intent"],
        }
    )
    return {"approval": ApprovalDecision.model_validate(decision).model_dump()}


def after_approval(state: Phase2State) -> Literal["execute", "finalize"]:
    return "execute" if state["approval"]["decision"] == "approved" else "finalize"


async def execute_node(state: Phase2State) -> Phase2State:
    intent = ActionIntent.model_validate(state["action_intent"])
    with phase_span(
        "servicemind.tool.execute",
        **{
            "gen_ai.tool.name": "glpi_append_ticket_followup",
            "gen_ai.tool.type": "extension",
            "servicemind.tenant.id": state["tenant_id"],
            "servicemind.run.id": state["run_id"],
            "itsm.ticket.id": intent.target_id,
        },
    ):
        result = await controlled_executor.execute(_context(state), intent)
    repository = ServiceMindRepository(UUID(state["tenant_id"]))
    await repository.append_event(
        UUID(state["run_id"]),
        "execution.verified",
        result.model_dump(mode="json"),
    )
    return {"execution_result": result.model_dump(mode="json")}


async def finalize_node(state: Phase2State) -> Phase2State:
    run_id = UUID(state["run_id"])
    repository = ServiceMindRepository(UUID(state["tenant_id"]))
    if state.get("approval", {}).get("decision") == "rejected":
        status = RunStatus.CANCELLED
    else:
        status = RunStatus.SUCCEEDED
    result = {
        "ticket": state.get("ticket_facts"),
        "analysis": state.get("analysis"),
        "action_intent": state.get("action_intent"),
        "approval": state.get("approval"),
        "execution": state.get("execution_result"),
    }
    await repository.update_run(run_id, status, result=result)
    await repository.append_event(run_id, f"run.{status.value}", {"status": status.value})
    return {"final_result": result}


builder = StateGraph(Phase2State)
builder.add_node("data", data_node)
builder.add_node("analysis", analysis_node)
builder.add_node("action", action_node)
builder.add_node("approval", approval_node)
builder.add_node("execute", execute_node)
builder.add_node("finalize", finalize_node)
builder.add_edge(START, "data")
builder.add_edge("data", "analysis")
builder.add_conditional_edges("analysis", after_analysis)
builder.add_edge("action", "approval")
builder.add_conditional_edges("approval", after_approval)
builder.add_edge("execute", "finalize")
builder.add_edge("finalize", END)

phase2_graph = builder.compile()


def configure_phase2_checkpointer(checkpointer: object) -> None:
    phase2_graph.checkpointer = checkpointer  # type: ignore[assignment]
