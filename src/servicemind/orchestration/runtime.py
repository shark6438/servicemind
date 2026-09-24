import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from servicemind.domain.models import ACTION_REQUIRED_ROLES, ApprovalDecision
from servicemind.domain.task import TaskPlan
from servicemind.orchestration.dispatcher import task_dispatcher
from servicemind.orchestration.state import Phase3State
from servicemind.orchestration.supervisor_workflow import supervisor_graph
from servicemind.persistence.models import ActionStatus, AgentRun
from servicemind.persistence.repository import ServiceMindRepository
from servicemind.security.auth import TenantContext
from servicemind.security.entitlements import (
    EntitlementResult,
    intersect,
    revalidate,
    step_shortfall,
)


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
        # The carrier ``Phase3State.group_ids`` was declared, read by every retrieval
        # consumer, and written by nobody: the token's ``glpi_group_ids`` claim reached
        # ``TenantContext`` and stopped there, so ``RetrievalPrincipal.group_ids`` was
        # always empty. That made a group-restricted document *unretrievable by
        # everyone*, including the group it named -- ``KnowledgeACL.allows`` requires an
        # intersection once a document declares groups. The same omission reached memory
        # ACL checks, which read the identical key. Populating it here is the whole fix;
        # the consumers were already correct.
        group_ids=sorted(context.allowed_glpi_group_ids),
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


class ResumeBlocked(RuntimeError):
    """A resume paused because the requester's current authority is unknown.

    Distinct from ``PermissionError``, which this module raises for a checkpoint that
    is internally inconsistent (wrong tenant, no principal at all). Those are
    corruption and the run is finished; this is a question that could not be answered
    *yet*, and the run is left exactly where it is so it can be resumed once it can.
    """

    def __init__(self, reason: str, detail: str | None = None) -> None:
        super().__init__(f"resume blocked: {reason}")
        self.reason = reason
        self.detail = detail


#: Products a run builds *on top of* evidence, in dependency order. Clearing the
#: evidence and leaving these is not a narrowing, it is a narrowing plus a stale
#: conclusion: the analysis already reasoned over documents the requester can no longer
#: read, the reviewer already passed that reasoning, the handoff already digested both,
#: and the action intent already carries the answer into a write. Every one of them is
#: a statement about evidence that is gone, so every one of them goes.
_DERIVED_PRODUCTS = (
    "joined_evidence",
    "analysis_result",
    "review_result",
    "handoff_envelope",
    "action_intent",
    "human_review",
)


@dataclass(frozen=True)
class ResumeScope:
    """The recorded identity, the authority actually being spent, and how to enforce it."""

    recorded: TenantContext
    effective: TenantContext
    #: What must be written into the checkpoint for the nodes to read the intersection
    #: instead of the scope the run started with. Empty when nothing changed.
    update: dict[str, Any]
    outcome: str
    #: The answer the intersection was computed from, kept whole so the audit row can
    #: say whether it came from the realm or from a cache -- see ``from_cache``.
    answer: EntitlementResult
    #: True when the run is suspended on an approval whose action rests on evidence the
    #: requester can no longer reach. The action is then not something to re-scope, it
    #: is something to withdraw: it has to be re-derived from re-retrieved evidence and
    #: approved again, because the human approved a document set, not a ticket number.
    void: bool = False


async def _recorded_principal(run: AgentRun, context: TenantContext) -> TenantContext:
    """The identity a run started with, read back from its own checkpoint.

    The caller contributes exactly one thing -- the tenant it authenticated against,
    which must match. Its roles, entities and groups are deliberately *not* merged in:
    an approver with more groups than the requester must not widen the run, and that is
    a property of this read, not of what the caller happens to pass.
    """
    snapshot = await supervisor_graph.aget_state(run_config(run, context))
    values = dict(snapshot.values or {})
    if not values:
        # There is no recorded principal to run as, and falling back to the caller
        # would reintroduce precisely the escalation this function exists to prevent.
        raise PermissionError("cannot resume a run with no checkpointed principal")
    recorded_tenant = values.get("tenant_id")
    if recorded_tenant is None or UUID(str(recorded_tenant)) != context.tenant_id:
        raise PermissionError("run checkpoint belongs to a different tenant")
    return TenantContext(
        tenant_id=UUID(str(recorded_tenant)),
        user_id=str(values["user_id"]),
        username=str(values.get("username") or values["user_id"]),
        roles=set(values.get("roles") or ()),
        allowed_glpi_entity_ids=set(values.get("allowed_glpi_entity_ids") or ()),
        # Missing reads as empty rather than as an error: a checkpoint written before the
        # group axis existed describes an identity that held no groups, and the narrower
        # reading is the safe one.
        allowed_glpi_group_ids=set(values.get("group_ids") or ()),
    )


async def decline_run(
    run: AgentRun, context: TenantContext, decision: ApprovalDecision
) -> Phase3State:
    """Apply a refusal, without re-establishing the requester's authority.

    Refusing spends nothing. ``after_approval`` routes a refusal straight to
    ``finalize``, which neither retrieves nor writes, so the question the resume
    boundary normally answers -- "what may this subject read now?" -- has nothing
    hanging on it: no evidence will be read and no action will be taken. Answering it
    anyway made an identity-provider outage able to stop a human from *declining*, which
    protects nothing and leaves the run sitting in ``WAITING_APPROVAL`` until the outage
    clears. A refusal is the one decision that is always safe to record.

    Only a refusal reaches here, and that is enforced rather than documented: the
    approval path goes through ``resolve_resume_scope`` and cannot be reached from this
    function. A resume that *executes* is exactly the case where the revoked grant must
    be noticed, and it must not be able to borrow this shortcut by passing a scope.

    The principal is still the run's recorded one, not the approver's. Nothing on this
    path reads it today -- the nodes rebuild their ``TenantContext`` from the checkpoint
    -- but "nothing reads it" is a fact about the current graph, not a property anybody
    declared, so the correct value is used regardless.
    """
    if decision.decision != "rejected":
        raise ValueError("decline_run applies refusals only")
    recorded = await _recorded_principal(run, context)
    return await supervisor_graph.ainvoke(  # type: ignore[return-value]
        # No ``update``: a refusal ends the run, so there is no checkpoint state left for
        # a narrowed scope to govern, and narrowing one would be a claim that the run
        # continued under it. No ``void`` either -- withdrawing the action is what a
        # *grant* triggers, and the human has already refused it.
        Command(resume=decision.model_dump(), update={}),
        config=run_config(run, recorded),
    )


async def resolve_resume_scope(run: AgentRun, context: TenantContext) -> ResumeScope:
    """Rebuild the principal a resumed run executes under -- narrowed, never widened.

    Resuming is triggered by whoever holds the approval or review role, but approving
    authorizes *an action*, it does not re-scope *the evidence the action rests on*.
    Passing the caller's ``TenantContext`` into the resumed graph meant that approving
    someone else's run silently handed that run every entity and group the approver
    happened to hold: the wider the approver, the wider the run. The run's own
    checkpoint is the only authority for its identity, and the caller contributes
    exactly one thing -- the tenant it authenticated against, which must match.

    In order of who may decide what:

    * The run keeps the identity it started with, and no caller identity is merged in.
      An approver with more groups than the requester changes nothing here.
    * A missing coordinate reads as empty, so scope can only shrink.
    * What was recorded is then re-verified against the identity provider and
      intersected with it, because a grant revoked while the run sat waiting for
      approval must not be spent when the run wakes up.
    * The intersection is *enforced*, not merely computed -- see ``update`` below.
    * If the authority cannot be established, the resume pauses. Narrowing and
      continuing is not sufficient: dropping groups still leaves the run reading every
      unrestricted document in the tenant on behalf of a subject nobody can vouch for.
    """
    recorded = await _recorded_principal(run, context)
    # Read separately from the principal above because ``void`` is a statement about the
    # action pending at the interrupt, and the two are not derivable from each other.
    snapshot = await supervisor_graph.aget_state(run_config(run, context))
    values = dict(snapshot.values or {})
    # ``fresh`` on a write-requesting run: the cache exists so that a burst of read
    # resumes does not become a burst of realm queries, and its whole window is time in
    # which a revocation has not been noticed. Reads may spend that window; a write may
    # not, because the write is the thing a revocation is supposed to stop.
    result = await revalidate(recorded.tenant_id, recorded.user_id, fresh=run.request_write)
    if not result.established:
        # Audited here rather than by each caller: every path that pauses must leave
        # the same record, and the HTTP caller cannot write it -- it does not know
        # whether the run advanced or not.
        blocked = ResumeBlocked(result.outcome.value, result.detail)
        await _record_block(run, recorded.tenant_id, blocked)
        raise blocked
    effective = TenantContext(
        tenant_id=recorded.tenant_id,
        user_id=recorded.user_id,
        username=recorded.username,
        roles=intersect(recorded.roles, result.roles),
        allowed_glpi_entity_ids=intersect(recorded.allowed_glpi_entity_ids, result.entity_ids),
        allowed_glpi_group_ids=intersect(recorded.allowed_glpi_group_ids, result.group_ids),
    )
    narrowed = effective != recorded
    # A pending action is the run's only irreversible step, and it is the one step whose
    # inputs were fixed before the interrupt: the action hash covers the arguments, the
    # arguments come from the analysis, and the analysis came from the evidence. If the
    # scope moved, every link in that chain was decided under a scope that no longer
    # holds, so the action is not re-scoped -- it is withdrawn. Checking this here, at
    # the boundary, is what keeps the approval from being spent on it.
    void = narrowed and bool(values.get("action_intent"))
    if values.get("action_intent"):
        # Asked whenever the run has an action pending, and deliberately not only when
        # the scope moved. A requester who never held the role this step needs is not a
        # narrowing: nothing was taken away, and a check that only compares recorded
        # against current grants finds no change at all -- because ``recorded`` was empty
        # too. Establishing that the subject is who they say they are does not establish
        # that they may perform the operation, and the second question is the one the
        # write depends on.
        missing = step_shortfall(
            roles=effective.roles,
            entity_ids=effective.allowed_glpi_entity_ids,
            required_roles=ACTION_REQUIRED_ROLES,
        )
        if missing:
            blocked = ResumeBlocked("insufficient_authority", json.dumps(missing, sort_keys=True))
            await _record_block(run, recorded.tenant_id, blocked)
            raise blocked

    update: dict[str, Any] = {}
    if narrowed:
        update = {
            "roles": sorted(effective.roles),
            "allowed_glpi_entity_ids": sorted(effective.allowed_glpi_entity_ids),
            "group_ids": sorted(effective.allowed_glpi_group_ids),
        }
        # Evidence gathered before the interrupt was gathered under the wider scope.
        # Leaving it in place would mean the run cites, into a write, a document the
        # requester could read a moment ago and cannot read now -- the narrowing would
        # be enforced on future retrieval and evaded by past retrieval. ``None`` is the
        # reset: see ``state._reset_or_append``.
        update["knowledge_evidence"] = None
        update["data_evidence"] = None
        # False, not True: ``evidence_dirty`` means "evidence is gathered but not yet
        # joined", and the Supervisor reads it as an instruction to join before
        # anything else. There is nothing here to join -- the plan below is reopened so
        # the evidence is gathered again, under the scope that now holds.
        update["evidence_dirty"] = False
        # ...and everything that was concluded *from* that evidence, for the same
        # reason one step further along. A cleared evidence list with an intact analysis
        # beside it is not a run that lost its evidence; it is a run that still has the
        # answer and has lost the ability to justify it. Cleared to ``{}`` rather than
        # ``None``: that is what the graph's other invalidators write (``revise_node``,
        # ``dispatch_barrier_node``), every reader tests these for truth, and a null
        # reaches ``finalize_node`` as an object it then calls ``.get`` on.
        # Finally the plan itself. Clearing the products without this leaves every task
        # marked SUCCESS, and the Supervisor -- which decides from task status, not from
        # the products -- sees "nothing ready, everything complete" and finalizes a run
        # that has just lost its evidence, its analysis and its reviewer's verdict.
        # Reopening the tasks is what makes the re-derivation happen instead of being
        # merely available: the evidence tasks become ready again, and re-dispatching
        # them is the only way the plan can progress.
        plan_payload = values.get("task_plan")
        if plan_payload:
            update["task_plan"] = task_dispatcher.invalidate(
                TaskPlan.model_validate(plan_payload)
            ).model_dump(mode="json", by_alias=True)
            update["plan_revision"] = int(values.get("plan_revision") or 0) + 1
    return ResumeScope(recorded, effective, update, result.outcome.value, result, void)


async def _audit_scope_change(run: AgentRun, scope: ResumeScope) -> None:
    """Written here rather than by the caller: this is the only place that knows both
    the recorded and the effective scope, and an operator has to be able to find out
    afterwards that an approval ran narrower than the requester's grant."""
    repository = ServiceMindRepository(scope.recorded.tenant_id)
    await repository.audit(
        actor_id="servicemind-resume",
        event_type="run.scope_narrowed",
        resource_type="AgentRun",
        resource_id=str(run.id),
        run_id=run.id,
        payload={
            "entitlement_verification": scope.outcome,
            # A verified answer is not by itself a fresh one. When it came from the
            # cache, the grants below were correct as of up to ``cache_window_seconds``
            # ago, and any revocation inside that window is exactly what this run did
            # not see -- so the window is recorded with the decision rather than left
            # to be inferred from the deployment's configuration.
            "answer_source": "cache" if scope.answer.from_cache else "identity_provider",
            "answer_age_bound_seconds": (
                scope.answer.cache_window_seconds if scope.answer.from_cache else 0.0
            ),
            "recorded": {
                "roles": sorted(scope.recorded.roles),
                "entity_ids": sorted(scope.recorded.allowed_glpi_entity_ids),
                "group_ids": sorted(scope.recorded.allowed_glpi_group_ids),
            },
            "effective": {
                "roles": sorted(scope.effective.roles),
                "entity_ids": sorted(scope.effective.allowed_glpi_entity_ids),
                "group_ids": sorted(scope.effective.allowed_glpi_group_ids),
            },
            "evidence_invalidated": bool(scope.update),
            # A narrowing at the approval boundary does not re-scope the pending action,
            # it withdraws it. Recorded here because the approval that never got written
            # is otherwise indistinguishable from an approval that was never requested.
            "pending_action_voided": scope.void,
        },
    )


async def _withdraw_pending_action(run: AgentRun, scope: ResumeScope) -> None:
    """Persist the withdrawal the resume boundary has already decided on.

    ``void`` is computed before anything is written -- that is what keeps a narrowing
    from spending an approval -- but until this runs it exists only in the graph
    invocation that has not happened yet. Two things depend on it being a record:

    * the action the approver was shown must stop being an action anyone can approve,
      which is a property of the row, not of the run's control flow; and
    * the re-derived action needs the slot back, because ``action_intents`` holds one
      action per run.

    The withdrawn action's identity is written here rather than left to the row,
    because the slot is reused and that row goes away. ``audit_events`` is append-only:
    the event is what lets an auditor say afterwards which action was taken back, and
    find no approval of it anywhere.
    """
    repository = ServiceMindRepository(scope.recorded.tenant_id)
    action = await repository.withdraw_action_intent(run.id)
    if action is None or action.status != ActionStatus.WITHDRAWN.value:
        return
    detail = {
        "action_intent_id": str(action.id),
        "action_type": action.action_type,
        "action_hash": action.action_hash,
        "target_id": action.target_id,
        "risk_level": action.risk_level,
        "evidence_refs": list(action.evidence_refs or ()),
        "reason": "scope_narrowed_before_decision",
    }
    await repository.audit(
        actor_id="servicemind-resume",
        event_type="action.withdrawn",
        resource_type="ActionIntent",
        resource_id=str(action.id),
        run_id=run.id,
        payload=detail,
    )
    await repository.append_event(run.id, "action.withdrawn", detail)


async def _record_block(run: AgentRun, tenant_id: UUID, exc: ResumeBlocked) -> None:
    repository = ServiceMindRepository(tenant_id)
    await repository.audit(
        actor_id="servicemind-resume",
        event_type="run.resume_blocked",
        resource_type="AgentRun",
        resource_id=str(run.id),
        run_id=run.id,
        payload={"reason": exc.reason, "detail": exc.detail},
    )


async def continue_incomplete_run(run: AgentRun, context: TenantContext) -> Phase3State:
    """Resume an existing checkpoint, or start a run that never reached the graph.

    Both branches need the requester's authority established *now*, and neither gets
    it from ``context``: for the interruption-free branch the caller is the recovery
    process, which holds no token at all, and for the never-started branch the scope
    the run would have had was never recorded anywhere -- so it is taken from what the
    requester is verified to hold at this moment rather than invented.
    """
    config = run_config(run, context)
    snapshot = await supervisor_graph.aget_state(config)
    if not snapshot.values:
        result = await revalidate(context.tenant_id, run.user_id, fresh=True)
        if not result.established:
            blocked = ResumeBlocked(result.outcome.value, result.detail)
            await _record_block(run, context.tenant_id, blocked)
            raise blocked
        return await start_run(
            run,
            TenantContext(
                tenant_id=context.tenant_id,
                user_id=run.user_id,
                username=run.user_id,
                roles=set(result.roles),
                allowed_glpi_entity_ids=set(result.entity_ids),
                allowed_glpi_group_ids=set(result.group_ids),
            ),
        )
    scope = await resolve_resume_scope(run, context)
    if scope.update:
        await _audit_scope_change(run, scope)
        # The nodes rebuild ``TenantContext`` from the checkpoint, so the intersection
        # has to be in the checkpoint. Passing it only as ``configurable`` -- which is
        # what this used to do -- writes it where nothing reads it.
        await supervisor_graph.aupdate_state(run_config(run, scope.effective), scope.update)
    return await supervisor_graph.ainvoke(  # type: ignore[return-value]
        None, config=run_config(run, scope.effective)
    )


async def resume_run(
    run: AgentRun,
    context: TenantContext,
    decision: ApprovalDecision,
    *,
    scope: ResumeScope | None = None,
) -> Phase3State:
    """Apply an approval. ``scope`` may be resolved by the caller first.

    A caller that has to persist something as a consequence of the decision -- an
    approval row, a status change -- must resolve the scope *before* it does, because
    resolving is the step that can refuse. Taking the scope as a parameter is how the
    HTTP boundary gets to check before it commits rather than after.
    """
    scope = scope or await resolve_resume_scope(run, context)
    if scope.update:
        await _audit_scope_change(run, scope)
    if scope.void:
        # Before the graph runs, not inside it: the re-derivation's ``action`` node
        # writes the replacement intent, and the run holds exactly one slot, so the
        # withdrawal has to be in place while that node is looking at the table.
        await _withdraw_pending_action(run, scope)
    return await supervisor_graph.ainvoke(  # type: ignore[return-value]
        # ``update`` travels with the resume: LangGraph applies it before the resumed
        # node reads state, which is what makes the intersection binding rather than
        # advisory.
        Command(resume=_resume_payload(decision, scope.void), update=scope.update),
        config=run_config(run, scope.effective),
    )


def _resume_payload(decision: ApprovalDecision, void: bool) -> dict[str, Any]:
    """What the interrupted ``approval`` node receives.

    A voided action is not an approval and not a rejection: the human never ruled on
    it, because by the time they did, the thing they were shown no longer described
    what the run was entitled to do. The node sends that back to the supervisor to be
    re-derived rather than resolving it either way.
    """
    return {"voided": True} if void else decision.model_dump()


async def resume_review_run(
    run: AgentRun,
    context: TenantContext,
    resolution: dict[str, str | None],
    *,
    scope: ResumeScope | None = None,
) -> Phase3State:
    scope = scope or await resolve_resume_scope(run, context)
    if scope.update:
        await _audit_scope_change(run, scope)
    if scope.void:
        # Same ordering as ``resume_run``: a review resolution can carry a narrowed
        # scope too, and a withdrawn action must be withdrawn in the table before the
        # node that would re-derive it looks for the slot.
        await _withdraw_pending_action(run, scope)
    return await supervisor_graph.ainvoke(  # type: ignore[return-value]
        Command(resume=resolution, update=scope.update),
        config=run_config(run, scope.effective),
    )


async def has_pending_interrupt(run: AgentRun, context: TenantContext) -> bool:
    snapshot = await supervisor_graph.aget_state(run_config(run, context))
    return any(getattr(task, "interrupts", ()) for task in snapshot.tasks)


async def get_checkpoint_state(run: AgentRun, context: TenantContext) -> dict:
    snapshot = await supervisor_graph.aget_state(run_config(run, context))
    return dict(snapshot.values)


def parse_run_id(value: str) -> UUID:
    return UUID(value)
