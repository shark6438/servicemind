from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from servicemind.domain.models import ApprovalDecision
from servicemind.orchestration.state import Phase3State
from servicemind.orchestration.supervisor_workflow import supervisor_graph
from servicemind.persistence.models import AgentRun
from servicemind.security.auth import TenantContext


def run_config(run: AgentRun, context: TenantContext) -> RunnableConfig:
    return RunnableConfig(
        configurable={
            "thread_id": run.thread_id,
            "user_id": context.user_id,
            "tenant_id": str(context.tenant_id),
        },
        metadata={"tenant_id": str(context.tenant_id), "run_id": str(run.id)},
    )


async def start_run(run: AgentRun, context: TenantContext) -> Phase3State:
    state = Phase3State(
        run_id=str(run.id),
        tenant_id=str(context.tenant_id),
        user_id=context.user_id,
        username=context.username,
        roles=sorted(context.roles),
        allowed_glpi_entity_ids=sorted(context.allowed_glpi_entity_ids),
        thread_id=run.thread_id,
        ticket_id=run.ticket_id,
        raw_request=run.goal,
        goal=run.goal,
        request_write=run.request_write,
        data_evidence=[],
        knowledge_evidence=[],
        branch_timings=[],
        branch_errors=[],
        task_completions=[],
        trajectory=[],
        control={},
        control_owner="supervisor",
        active_agent="router",
        evidence_dirty=False,
        plan_revision=0,
    )
    return await supervisor_graph.ainvoke(state, config=run_config(run, context))  # type: ignore[return-value]


async def continue_incomplete_run(run: AgentRun, context: TenantContext) -> Phase3State:
    """Resume an existing checkpoint, or start only when no checkpoint exists."""
    config = run_config(run, context)
    snapshot = await supervisor_graph.aget_state(config)
    if snapshot.values:
        return await supervisor_graph.ainvoke(None, config=config)  # type: ignore[return-value]
    return await start_run(run, context)


async def resume_run(
    run: AgentRun, context: TenantContext, decision: ApprovalDecision
) -> Phase3State:
    config = run_config(run, context)
    return await supervisor_graph.ainvoke(  # type: ignore[return-value]
        Command(resume=decision.model_dump()), config=config
    )


async def resume_review_run(
    run: AgentRun, context: TenantContext, resolution: dict[str, str | None]
) -> Phase3State:
    return await supervisor_graph.ainvoke(  # type: ignore[return-value]
        Command(resume=resolution), config=run_config(run, context)
    )


async def has_pending_interrupt(run: AgentRun, context: TenantContext) -> bool:
    snapshot = await supervisor_graph.aget_state(run_config(run, context))
    return any(getattr(task, "interrupts", ()) for task in snapshot.tasks)


async def get_checkpoint_state(run: AgentRun, context: TenantContext) -> dict:
    snapshot = await supervisor_graph.aget_state(run_config(run, context))
    return dict(snapshot.values)


def parse_run_id(value: str) -> UUID:
    return UUID(value)
