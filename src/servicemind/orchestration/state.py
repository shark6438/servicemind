import operator
from typing import Annotated, Any, TypedDict


class Phase2State(TypedDict, total=False):
    run_id: str
    tenant_id: str
    user_id: str
    username: str
    roles: list[str]
    allowed_glpi_entity_ids: list[int]
    ticket_id: int
    goal: str
    request_write: bool
    ticket_facts: dict[str, Any]
    analysis: dict[str, Any]
    action_intent: dict[str, Any]
    approval: dict[str, Any]
    execution_result: dict[str, Any]
    final_result: dict[str, Any]


class Phase3State(TypedDict, total=False):
    run_id: str
    tenant_id: str
    user_id: str
    username: str
    roles: list[str]
    allowed_glpi_entity_ids: list[int]
    # Optional GLPI group/profile ACL carriers. They mirror the retrieval principal so
    # dispatch can forward the full identity to the Knowledge DAG node; absent for
    # callers that only scope on entity. Consumers read via ``state.get`` so runs that
    # never set them are unaffected.
    group_ids: list[int]
    profile_ids: list[int]
    thread_id: str
    ticket_id: int
    raw_request: str
    goal: str
    knowledge_query: str
    request_write: bool
    route: dict[str, Any]
    task_plan: dict[str, Any]
    supervisor_decision: dict[str, Any]
    control_owner: str
    active_agent: str
    evidence_dirty: bool
    dispatch_batch_id: str
    dispatch_task: dict[str, Any]
    invocation_model_budget: int
    invocation_tool_budget: int
    task_completions: Annotated[list[dict[str, Any]], operator.add]
    plan_revision: int
    data_evidence: Annotated[list[dict[str, Any]], operator.add]
    knowledge_evidence: Annotated[list[dict[str, Any]], operator.add]
    joined_evidence: dict[str, Any]
    analysis_result: dict[str, Any]
    review_result: dict[str, Any]
    handoff_envelope: dict[str, Any]
    action_intent: dict[str, Any]
    approval: dict[str, Any]
    human_review: dict[str, Any]
    execution_result: dict[str, Any]
    control: dict[str, Any]
    budget: dict[str, Any]
    termination_code: str
    branch_timings: Annotated[list[dict[str, Any]], operator.add]
    branch_errors: Annotated[list[dict[str, Any]], operator.add]
    agent_invocations: Annotated[list[dict[str, Any]], operator.add]
    trajectory: Annotated[list[str], operator.add]
    final_result: dict[str, Any]
