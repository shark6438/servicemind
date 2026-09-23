import operator
from typing import Annotated, Any, TypedDict


def _reset_or_append(
    current: list[dict[str, Any]] | None, update: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """Accumulate evidence, where ``None`` means "everything gathered so far is void".

    ``operator.add`` cannot say that. Adding an empty list adds nothing, so the
    narrowing path's ``{"data_evidence": []}`` -- written to strip evidence gathered
    under a scope the requester has since lost -- left every item in place, and the
    resumed run was free to cite it into a write. The narrowing was enforced on future
    retrieval and evaded by past retrieval, which is the exact opposite of what the
    update that wrote it says it does.

    ``None`` is not a list any node produces as evidence, so it costs nothing to give
    it this second meaning. It cannot reach the state as a value: on a channel with a
    reducer, ``None`` is a message to the reducer rather than a value to store.
    """
    if update is None:
        return []
    return [*(current or []), *update]


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
    data_evidence: Annotated[list[dict[str, Any]], _reset_or_append]
    knowledge_evidence: Annotated[list[dict[str, Any]], _reset_or_append]
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
