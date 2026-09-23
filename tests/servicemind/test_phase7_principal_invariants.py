"""Invariants of the identity that reaches retrieval, and of the one a resume runs as.

Three defects lived on this path and none was visible from either end of it.

The first: ``Phase3State.group_ids`` was declared, read by the dispatch sites and by the
Knowledge node, and written by nobody. ``TenantContext.allowed_glpi_group_ids`` carried
the claim in from the token and stopped there, so every ``RetrievalPrincipal`` was built
with an empty group set -- and ``KnowledgeACL.allows`` requires an *intersection* once a
document declares groups, so a group-restricted document was unretrievable by everyone,
including the group it named.

The second: resuming a run passed the *approver's* ``TenantContext`` into the graph, so
approving somebody else's run handed it the approver's scope.

The third, and the one that survived the fix for the second: the rebuilt principal was
passed only as ``configurable``, which **nothing reads**. The nodes rebuild their
``TenantContext`` from the checkpoint, so every narrowing computed at the resume boundary
was computed and discarded. The tests below therefore assert on what the graph is
*handed*, and one of them pins the LangGraph behaviour that makes handing it over
sufficient.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TypedDict
from uuid import UUID, uuid4

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import SecretStr

from servicemind.domain.knowledge import CorpusScope, KnowledgeACL, RetrievalPrincipal
from servicemind.domain.models import ApprovalDecision
from servicemind.domain.task import AgentName, Budget, Task, TaskPlan, TaskStatus
from servicemind.orchestration.state import Phase3State
from servicemind.persistence.models import AgentRun, RunStatus
from servicemind.security.auth import TenantContext
from servicemind.security.entitlements import (
    AuthorityWithdrawn,
    EntitlementOutcome,
    EntitlementResult,
    KeycloakEntitlementVerifier,
    configure_entitlement_verifier,
    current_entitlement_verifier,
    require_still_held,
)

TENANT = UUID("22222222-2222-4222-8222-222222222222")
OTHER_TENANT = UUID("11111111-1111-4111-8111-111111111111")
REQUESTER = "globex-analyst-g3"
APPROVER = "globex-approver"


def _run(**overrides) -> AgentRun:
    fields = {
        "id": uuid4(),
        "tenant_id": TENANT,
        "user_id": REQUESTER,
        "thread_id": str(uuid4()),
        "ticket_id": 7,
        "goal": "VPN MFA rejected after a phone change",
        "request_write": False,
        # A run that was never persisted has no timestamps, and the endpoint's view
        # model requires them -- so the fixture carries what the row would carry.
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    fields.update(overrides)
    return AgentRun(**fields)


def _requester_checkpoint(**overrides) -> dict:
    """What ``start_run`` writes for the requester: entity 2, group 3, viewer+analyst."""
    values = {
        "tenant_id": str(TENANT),
        "user_id": REQUESTER,
        "username": REQUESTER,
        "roles": ["viewer", "analyst"],
        "allowed_glpi_entity_ids": [2],
        "group_ids": [3],
    }
    values.update(overrides)
    return values


class _Verifier:
    """A verifier whose answer the test chooses, recording how it was asked."""

    def __init__(self, result: EntitlementResult) -> None:
        self.result = result
        self.calls: list[dict] = []

    async def verify(self, tenant_id, user_id, *, fresh=False) -> EntitlementResult:
        self.calls.append({"tenant_id": tenant_id, "user_id": user_id, "fresh": fresh})
        return self.result


def _held(groups=(3,), *, entity_ids=(2,), roles=("viewer", "analyst")) -> EntitlementResult:
    return EntitlementResult(
        EntitlementOutcome.VERIFIED,
        roles=frozenset(roles),
        entity_ids=frozenset(entity_ids),
        group_ids=frozenset(groups),
    )


class _Graph:
    """Stands in for ``supervisor_graph`` and records what it was handed."""

    def __init__(self, values: dict | None = None) -> None:
        self.values = values if values is not None else {}
        self.invocations: list[tuple[object, dict]] = []
        self.state_updates: list[dict] = []

    async def aget_state(self, config):
        return SimpleNamespace(values=self.values)

    async def ainvoke(self, payload, config=None):
        self.invocations.append((payload, config))
        return {"ok": True}

    async def aupdate_state(self, config, values):
        self.state_updates.append(values)
        self.values.update(values)


@pytest.fixture(autouse=True)
def _restore_verifier():
    """Restore whatever ``security.auth`` installed, not ``None``.

    The verifier is a process-wide slot populated at import; a test that clears it and
    does not put it back would leave the suite running against a different wiring than
    the process it is testing.
    """
    original = current_entitlement_verifier()
    yield
    configure_entitlement_verifier(original)


@pytest.fixture
def runtime(monkeypatch):
    from servicemind.orchestration import runtime as module

    audits: list[dict] = []
    withdrawals: list[UUID] = []
    events: list[tuple[UUID, str, dict]] = []

    class _Repository:
        def __init__(self, tenant_id):
            assert tenant_id == TENANT

        async def audit(self, **kwargs):
            audits.append(kwargs)

        async def append_event(self, run_id, event_type, payload):
            events.append((run_id, event_type, payload))

        async def withdraw_action_intent(self, run_id):
            # The real method returns the row as it stands -- ``WITHDRAWN`` when it
            # could take the action back, the untouched row or ``None`` otherwise. The
            # stand-in only has to answer the question the runtime asks of it: "was
            # this action withdrawn by this call?". The row's own lifecycle is covered
            # against PostgreSQL in ``test_phase5_governance``.
            withdrawals.append(run_id)
            return SimpleNamespace(
                id=UUID("00000000-0000-4000-8000-000000000a01"),
                status="withdrawn",
                action_type="append_ticket_followup",
                action_hash="0" * 64,
                target_id=26,
                risk_level="low",
                evidence_refs=["ev-1"],
            )

    monkeypatch.setattr(module, "ServiceMindRepository", _Repository)
    return SimpleNamespace(module=module, audits=audits, withdrawals=withdrawals, events=events)


def _events(audits, event_type):
    return [row for row in audits if row["event_type"] == event_type]


# --------------------------------------------------------------------------------------
# The group carrier


@pytest.mark.asyncio
async def test_start_run_carries_group_scope_into_the_graph(monkeypatch, runtime) -> None:
    """``start_run`` is the only writer of the carrier every retrieval reads."""
    graph = _Graph()
    monkeypatch.setattr(runtime.module, "supervisor_graph", graph)

    await runtime.module.start_run(
        _run(),
        TenantContext(
            tenant_id=TENANT,
            user_id=REQUESTER,
            username=REQUESTER,
            roles={"viewer", "analyst"},
            allowed_glpi_entity_ids={2},
            allowed_glpi_group_ids={3},
        ),
    )

    initial_state = graph.invocations[0][0]
    assert initial_state["group_ids"] == [3]
    assert initial_state["allowed_glpi_entity_ids"] == [2]


def test_the_graph_rebuilds_group_scope_from_state() -> None:
    """A resumed node rebuilds ``TenantContext`` from the checkpoint, not from a token."""
    from servicemind.orchestration.supervisor_workflow import _context

    rebuilt = _context(_requester_checkpoint())
    assert rebuilt.allowed_glpi_group_ids == {3}
    assert rebuilt.allowed_glpi_entity_ids == {2}


def test_a_checkpoint_without_group_scope_reads_as_no_group_scope() -> None:
    """A checkpoint written before the carrier existed must not fail the run, and must
    not be read as "unrestricted" either -- the absent coordinate is the empty one."""
    from servicemind.orchestration.supervisor_workflow import _context

    legacy = _requester_checkpoint()
    del legacy["group_ids"]
    assert _context(legacy).allowed_glpi_group_ids == set()


def test_a_group_restricted_document_is_readable_by_its_group_and_no_other() -> None:
    """The ACL the acceptance fixtures depend on, pinned as a truth table."""
    acl = KnowledgeACL(
        corpus_scope=CorpusScope.TENANT,
        tenant_id=TENANT,
        group_ids=frozenset({3}),
        effective_from=datetime(2026, 1, 1, tzinfo=UTC),
    )

    def principal(groups: set[int]) -> RetrievalPrincipal:
        return RetrievalPrincipal(
            tenant_id=TENANT,
            user_id=REQUESTER,
            entity_ids=frozenset({2}),
            group_ids=frozenset(groups),
        )

    assert principal({3}).allows(acl) is True
    assert principal({3, 4}).allows(acl) is True
    assert principal({4}).allows(acl) is False
    assert principal(set()).allows(acl) is False


def test_an_unrestricted_document_is_readable_by_everyone_in_the_tenant() -> None:
    acl = KnowledgeACL(
        corpus_scope=CorpusScope.TENANT,
        tenant_id=TENANT,
        effective_from=datetime(2026, 1, 1, tzinfo=UTC),
    )
    for groups in ({3}, {4}, set()):
        principal = RetrievalPrincipal(
            tenant_id=TENANT,
            user_id=REQUESTER,
            entity_ids=frozenset({2}),
            group_ids=frozenset(groups),
        )
        assert principal.allows(acl) is True


# --------------------------------------------------------------------------------------
# The resume principal: who it is


@pytest.mark.asyncio
async def test_approving_does_not_lend_the_approver_scope(monkeypatch, runtime) -> None:
    """The escalation this file exists for: an approver with more groups than the
    requester must not raise the resumed run's scope to their own."""
    graph = _Graph(_requester_checkpoint())
    monkeypatch.setattr(runtime.module, "supervisor_graph", graph)
    configure_entitlement_verifier(_Verifier(_held()))

    approver = TenantContext(
        tenant_id=TENANT,
        user_id=APPROVER,
        username=APPROVER,
        roles={"viewer", "analyst", "operator", "approver"},
        allowed_glpi_entity_ids={2},
        allowed_glpi_group_ids={3, 4},
    )
    await runtime.module.resume_run(
        _run(),
        approver,
        ApprovalDecision(decision="approved", decided_by=APPROVER),
    )

    payload, config = graph.invocations[0]
    assert isinstance(payload, Command)
    assert config["configurable"]["user_id"] == REQUESTER
    assert config["metadata"]["tenant_id"] == str(TENANT)
    assert config["configurable"]["user_id"] != APPROVER


@pytest.mark.asyncio
async def test_a_checkpoint_from_another_tenant_cannot_be_resumed(monkeypatch, runtime) -> None:
    graph = _Graph(_requester_checkpoint(tenant_id=str(OTHER_TENANT)))
    monkeypatch.setattr(runtime.module, "supervisor_graph", graph)
    configure_entitlement_verifier(_Verifier(_held()))

    with pytest.raises(PermissionError):
        await runtime.module.resume_run(
            _run(),
            TenantContext(tenant_id=TENANT, user_id=APPROVER, username=APPROVER),
            ApprovalDecision(decision="approved", decided_by=APPROVER),
        )
    assert graph.invocations == []


@pytest.mark.asyncio
async def test_a_run_with_no_checkpoint_cannot_be_resumed(monkeypatch, runtime) -> None:
    """There is no principal to run as, and falling back to the caller's is exactly
    the escalation this path was rebuilt to remove."""
    graph = _Graph({})
    monkeypatch.setattr(runtime.module, "supervisor_graph", graph)
    configure_entitlement_verifier(_Verifier(_held()))

    with pytest.raises(PermissionError):
        await runtime.module.resume_run(
            _run(),
            TenantContext(tenant_id=TENANT, user_id=APPROVER, username=APPROVER),
            ApprovalDecision(decision="approved", decided_by=APPROVER),
        )
    assert graph.invocations == []


# --------------------------------------------------------------------------------------
# The resume principal: what it is allowed to touch


def test_a_command_update_reaches_the_resumed_node() -> None:
    """The mechanism the enforcement in ``runtime`` rests on, pinned.

    ``Command(resume=..., update=...)`` has to be applied *before* the resumed node
    reads state, or the intersection is computed and then discarded -- which is exactly
    the bug this replaced. Nothing else in the product exercises this, so if a LangGraph
    upgrade changes the ordering, this is where it shows up.
    """

    class _State(TypedDict, total=False):
        groups: list[int]
        seen: list[int]

    def gate(state: _State):
        interrupt({"ask": "approve?"})
        return {"groups": state["groups"]}

    def after(state: _State):
        return {"seen": state["groups"]}

    builder = StateGraph(_State)
    builder.add_node("gate", gate)
    builder.add_node("after", after)
    builder.add_edge(START, "gate")
    builder.add_edge("gate", "after")
    builder.add_edge("after", END)
    app = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": str(uuid4())}}

    async def scenario():
        await app.ainvoke({"groups": [3, 4]}, config=config)
        return await app.ainvoke(Command(resume="approved", update={"groups": [4]}), config=config)

    import asyncio

    final = asyncio.run(scenario())
    assert final["seen"] == [4]


@pytest.mark.asyncio
async def test_a_revoked_group_is_enforced_into_the_checkpoint(monkeypatch, runtime) -> None:
    """The requester held groups 3 and 4; they still hold 4. The resumed run is handed
    4 -- and handed it in the place the nodes actually read."""
    graph = _Graph(_requester_checkpoint(group_ids=[3, 4]))
    monkeypatch.setattr(runtime.module, "supervisor_graph", graph)
    configure_entitlement_verifier(_Verifier(_held(groups=(4,))))

    await runtime.module.resume_run(
        _run(),
        TenantContext(tenant_id=TENANT, user_id=APPROVER, username=APPROVER),
        ApprovalDecision(decision="approved", decided_by=APPROVER),
    )

    payload, config = graph.invocations[0]
    assert payload.update["group_ids"] == [4]
    assert config["configurable"]["user_id"] == REQUESTER

    scope_events = _events(runtime.audits, "run.scope_narrowed")
    assert len(scope_events) == 1
    assert scope_events[0]["payload"]["entitlement_verification"] == "verified"
    assert scope_events[0]["payload"]["recorded"]["group_ids"] == [3, 4]
    assert scope_events[0]["payload"]["effective"]["group_ids"] == [4]


def _plan_mid_run() -> dict:
    """A plan whose evidence tasks and everything downstream of them have succeeded."""
    deadline = datetime.now(UTC) + timedelta(hours=1)
    return TaskPlan(
        goal="Investigate the VPN multi-factor failure on ticket 25",
        deadline=deadline,
        budget=Budget(max_steps=24, max_replans=2, deadline=deadline),
        tasks=[
            Task(
                task_id="T1",
                agent=AgentName.DATA,
                task_type="ticket_retrieval",
                status=TaskStatus.SUCCESS,
                output_ref="ev-1,ev-2",
                deadline=deadline,
            ),
            Task(
                task_id="T2",
                agent=AgentName.KNOWLEDGE,
                task_type="knowledge_retrieval",
                status=TaskStatus.SUCCESS,
                output_ref="ev-k1",
                deadline=deadline,
            ),
            Task(
                task_id="T3",
                agent=AgentName.ANALYSIS,
                task_type="analysis",
                depends_on=["T1", "T2"],
                status=TaskStatus.SUCCESS,
                deadline=deadline,
            ),
            Task(
                task_id="T4",
                agent=AgentName.REVIEWER,
                task_type="review",
                depends_on=["T3"],
                status=TaskStatus.SUCCESS,
                deadline=deadline,
            ),
            Task(
                task_id="T5",
                agent=AgentName.ACTION,
                task_type="propose_followup",
                depends_on=["T4"],
                status=TaskStatus.SUCCESS,
                deadline=deadline,
            ),
        ],
    ).model_dump(mode="json", by_alias=True)


def test_the_evidence_channels_treat_none_as_a_reset() -> None:
    """The channel behaviour the narrowing's reset depends on, pinned.

    ``data_evidence`` and ``knowledge_evidence`` accumulate across retrieval rounds, and
    the narrowing writes ``None`` to strip what was gathered under the wider scope. That
    strips anything only because the channel is no longer ``operator.add``: under
    ``operator.add`` the identical update added an empty list, which adds *nothing*, and
    every item stayed exactly where it was. The run then kept -- and could still cite
    into a write -- the documents the narrowing existed to take away.

    Run against the product's own ``Phase3State`` rather than a copy of it, so a channel
    declared back to ``operator.add`` fails here.
    """

    def gather(state: Phase3State) -> dict:
        return {"data_evidence": [{"id": "ev-1"}, {"id": "ev-2"}]}

    def narrow(state: Phase3State) -> dict:
        return {"data_evidence": None, "knowledge_evidence": None}

    builder = StateGraph(Phase3State)
    builder.add_node("gather", gather)
    builder.add_node("narrow", narrow)
    builder.add_edge(START, "gather")
    builder.add_edge("gather", "narrow")
    builder.add_edge("narrow", END)
    app = builder.compile()

    import asyncio

    final = asyncio.run(app.ainvoke({"data_evidence": [], "knowledge_evidence": [{"id": "ev-k1"}]}))
    assert final["data_evidence"] == []
    assert final["knowledge_evidence"] == []


@pytest.mark.asyncio
async def test_narrowing_invalidates_evidence_gathered_under_the_wider_scope(
    monkeypatch, runtime
) -> None:
    """A narrowing must reach backwards as well as forwards.

    Evidence collected before the interrupt was collected under the scope the run has
    just lost. Leaving it in the checkpoint would let the run cite, into a write, a
    document the requester can no longer read -- the narrowing would bind future
    retrieval and be evaded by past retrieval.

    Asserting that the update *says* the right thing is not enough, and that is not a
    hypothetical: this test used to assert ``knowledge_evidence == []`` and passed for
    months while the write cleared nothing at all. So the two halves are separated --
    the update carries the reset signal, and the channel test above pins that the signal
    resets. Clearing the evidence also has to reopen the plan, because the Supervisor
    decides from task status: with every task left at ``SUCCESS``, a run that had just
    lost its evidence, its analysis and its verdict looked complete, and finalized.
    """
    graph = _Graph(
        _requester_checkpoint(
            group_ids=[3, 4], action_intent={"id": "a-1"}, task_plan=_plan_mid_run()
        )
    )
    monkeypatch.setattr(runtime.module, "supervisor_graph", graph)
    configure_entitlement_verifier(_Verifier(_held(groups=(4,))))

    await runtime.module.resume_run(
        _run(),
        TenantContext(tenant_id=TENANT, user_id=APPROVER, username=APPROVER),
        ApprovalDecision(decision="approved", decided_by=APPROVER),
    )

    update = graph.invocations[0][0].update
    assert update["knowledge_evidence"] is None
    assert update["data_evidence"] is None
    # False: there is nothing gathered to join. ``True`` sends the Supervisor to join an
    # empty set, which is a detour past the re-dispatch this whole update is for.
    assert update["evidence_dirty"] is False
    for product in runtime.module._DERIVED_PRODUCTS:
        assert update[product] == {}, f"{product} was not invalidated"
    reopened = TaskPlan.model_validate(update["task_plan"])
    assert [task.status for task in reopened.tasks] == [TaskStatus.PENDING] * 5
    assert [task.output_ref for task in reopened.tasks] == [None] * 5
    assert update["plan_revision"] == 1
    assert _events(runtime.audits, "run.scope_narrowed")[0]["payload"]["evidence_invalidated"]


@pytest.mark.asyncio
async def test_a_pending_action_is_withdrawn_when_the_scope_narrows(monkeypatch, runtime) -> None:
    """Clearing the evidence is not enough, and this is the case that shows why.

    The run is suspended on an approval. The action it is asking about was derived from
    evidence gathered under a scope the requester no longer has, and the human is
    looking at that action. Re-scoping it and letting the approval through would execute
    a decision about documents the run may no longer cite; the action has to be
    withdrawn and re-derived.
    """
    graph = _Graph(_requester_checkpoint(group_ids=[3, 4], action_intent={"id": "a-1"}))
    monkeypatch.setattr(runtime.module, "supervisor_graph", graph)
    configure_entitlement_verifier(_Verifier(_held(groups=(4,))))

    await runtime.module.resume_run(
        _run(),
        TenantContext(tenant_id=TENANT, user_id=APPROVER, username=APPROVER),
        ApprovalDecision(decision="approved", decided_by=APPROVER),
    )

    payload = graph.invocations[0][0]
    assert payload.resume == {"voided": True}, "not an approval: the action no longer exists"
    for product in runtime.module._DERIVED_PRODUCTS:
        assert payload.update[product] == {}, f"{product} was not invalidated"
    assert _events(runtime.audits, "run.scope_narrowed")[0]["payload"]["pending_action_voided"]


@pytest.mark.asyncio
async def test_the_withdrawal_reaches_the_row_and_not_only_the_checkpoint(
    monkeypatch, runtime
) -> None:
    """A voided action the run cannot execute is still an action the approver can see.

    Clearing ``action_intent`` in the checkpoint stops the *graph* from executing it,
    but the row is what ``GET /runs/{id}`` shows a human and what a later approval is
    checked against -- and it is the slot the re-derived action has to be written into,
    since a run may hold exactly one. Withdrawing in state alone left the replacement
    unpourable and the old action looking live.
    """
    run = _run()
    graph = _Graph(_requester_checkpoint(group_ids=[3, 4], action_intent={"id": "a-1"}))
    monkeypatch.setattr(runtime.module, "supervisor_graph", graph)
    configure_entitlement_verifier(_Verifier(_held(groups=(4,))))

    await runtime.module.resume_run(
        run,
        TenantContext(tenant_id=TENANT, user_id=APPROVER, username=APPROVER),
        ApprovalDecision(decision="approved", decided_by=APPROVER),
    )

    assert runtime.withdrawals == [run.id], "the run's pending action was not taken back"
    withdrawn = _events(runtime.audits, "action.withdrawn")
    assert withdrawn, "the withdrawal has to survive the row it describes"
    assert withdrawn[0]["payload"]["action_hash"] == "0" * 64
    assert withdrawn[0]["payload"]["reason"] == "scope_narrowed_before_decision"
    assert [event for _, event, _ in runtime.events if event == "action.withdrawn"], (
        "the timeline has to show the withdrawal, not only the audit table"
    )
    assert graph.invocations[0][0].resume == {"voided": True}


@pytest.mark.asyncio
async def test_a_pending_action_survives_a_resume_that_narrows_nothing(
    monkeypatch, runtime
) -> None:
    """The withdrawal is tied to the scope moving, not to there being an action."""
    graph = _Graph(_requester_checkpoint(group_ids=[3], action_intent={"id": "a-1"}))
    monkeypatch.setattr(runtime.module, "supervisor_graph", graph)
    configure_entitlement_verifier(_Verifier(_held(groups=(3,))))

    await runtime.module.resume_run(
        _run(),
        TenantContext(tenant_id=TENANT, user_id=APPROVER, username=APPROVER),
        ApprovalDecision(decision="approved", decided_by=APPROVER),
    )

    payload = graph.invocations[0][0]
    assert payload.resume["decision"] == "approved"
    assert payload.update == {}
    assert runtime.withdrawals == [], "nothing moved, so there is nothing to take back"


@pytest.mark.asyncio
async def test_a_narrowed_run_with_no_role_left_for_the_step_pauses(monkeypatch, runtime) -> None:
    """Verified, still enabled, holds something -- and cannot perform the step.

    Nothing was revoked here, so a check that only looks for revocations passes: the
    roles intersection is empty because the recorded roles are empty, and the emptiness
    is read as "nothing to take away". The step needs an ``analyst`` role and an entity
    for the ticket to live in, and having neither is not the same as having no
    restriction.
    """
    graph = _Graph(
        _requester_checkpoint(roles=[], allowed_glpi_entity_ids=[], action_intent={"id": "a-1"})
    )
    monkeypatch.setattr(runtime.module, "supervisor_graph", graph)
    configure_entitlement_verifier(_Verifier(_held(groups=(3,), entity_ids=(), roles=())))

    with pytest.raises(runtime.module.ResumeBlocked) as raised:
        await runtime.module.resume_run(
            _run(),
            TenantContext(tenant_id=TENANT, user_id=APPROVER, username=APPROVER),
            ApprovalDecision(decision="approved", decided_by=APPROVER),
        )

    assert raised.value.reason == "insufficient_authority"
    assert graph.invocations == []
    assert _events(runtime.audits, "run.resume_blocked")[0]["payload"]["reason"] == (
        "insufficient_authority"
    )


@pytest.mark.asyncio
async def test_a_requester_who_never_held_the_step_role_cannot_approve_it(
    monkeypatch, runtime
) -> None:
    """Nothing was revoked, and the write is still refused.

    This is the second boundary the design has to hold, stated as a case: the identity
    provider confirms the subject, confirms they are enabled, and confirms every grant
    they were recorded with -- so re-verification succeeds and the scope does not move.
    What it does not confirm is that those grants are *enough*. Without the check below,
    a verified subject holding ``viewer`` and nothing else would carry an approval
    straight into ``execute_node``; the verification would have answered "who are you"
    and been read as answering "may you".
    """
    graph = _Graph(_requester_checkpoint(roles=["viewer"], action_intent={"id": "a-1"}))
    monkeypatch.setattr(runtime.module, "supervisor_graph", graph)
    configure_entitlement_verifier(_Verifier(_held(roles=("viewer",), groups=(3,))))

    with pytest.raises(runtime.module.ResumeBlocked) as raised:
        await runtime.module.resume_run(
            _run(),
            TenantContext(tenant_id=TENANT, user_id=APPROVER, username=APPROVER),
            ApprovalDecision(decision="approved", decided_by=APPROVER),
        )

    assert raised.value.reason == "insufficient_authority"
    assert "analyst" in (raised.value.detail or "")
    assert graph.invocations == []


@pytest.mark.asyncio
async def test_a_resume_that_narrows_nothing_writes_no_audit(monkeypatch, runtime) -> None:
    """Verification that confirms every grant is not an event. Audit rows that fire on
    the happy path are how a real narrowing gets lost in the noise."""
    graph = _Graph(_requester_checkpoint(group_ids=[3]))
    monkeypatch.setattr(runtime.module, "supervisor_graph", graph)
    configure_entitlement_verifier(_Verifier(_held(groups=(3, 4), entity_ids=(2, 9))))

    await runtime.module.resume_run(
        _run(),
        TenantContext(tenant_id=TENANT, user_id=APPROVER, username=APPROVER),
        ApprovalDecision(decision="approved", decided_by=APPROVER),
    )

    assert runtime.audits == []
    assert graph.invocations[0][0].update == {}


@pytest.mark.asyncio
async def test_the_revocation_window_a_decision_used_is_recorded(monkeypatch, runtime) -> None:
    """A verified answer and a *fresh* answer are different facts, and the difference is
    the amount of time a revocation could have gone unnoticed.

    An operator reading this run's history has to be able to tell that an approval was
    applied on a grant set that was up to thirty seconds old -- otherwise the deployment
    silently carries a revocation lag that no record admits to.
    """
    graph = _Graph(_requester_checkpoint(group_ids=[3, 4]))
    monkeypatch.setattr(runtime.module, "supervisor_graph", graph)
    configure_entitlement_verifier(
        _Verifier(
            EntitlementResult(
                EntitlementOutcome.VERIFIED,
                roles=frozenset({"viewer", "analyst"}),
                entity_ids=frozenset({2}),
                group_ids=frozenset({4}),
                from_cache=True,
                cache_window_seconds=30.0,
            )
        )
    )

    await runtime.module.resume_run(
        _run(request_write=False),
        TenantContext(tenant_id=TENANT, user_id=APPROVER, username=APPROVER),
        ApprovalDecision(decision="approved", decided_by=APPROVER),
    )

    payload = _events(runtime.audits, "run.scope_narrowed")[0]["payload"]
    assert payload["answer_source"] == "cache"
    assert payload["answer_age_bound_seconds"] == 30.0


@pytest.mark.asyncio
async def test_a_fresh_answer_records_no_staleness(monkeypatch, runtime) -> None:
    graph = _Graph(_requester_checkpoint(group_ids=[3, 4]))
    monkeypatch.setattr(runtime.module, "supervisor_graph", graph)
    configure_entitlement_verifier(_Verifier(_held(groups=(4,))))

    await runtime.module.resume_run(
        _run(request_write=True),
        TenantContext(tenant_id=TENANT, user_id=APPROVER, username=APPROVER),
        ApprovalDecision(decision="approved", decided_by=APPROVER),
    )

    payload = _events(runtime.audits, "run.scope_narrowed")[0]["payload"]
    assert payload["answer_source"] == "identity_provider"
    assert payload["answer_age_bound_seconds"] == 0.0


@pytest.mark.asyncio
async def test_a_write_requester_revalidation_bypasses_the_cache(monkeypatch, runtime) -> None:
    """The cache window is time in which a revocation has not been noticed. A read may
    spend it; the write it exists to stop may not."""
    graph = _Graph(_requester_checkpoint())
    monkeypatch.setattr(runtime.module, "supervisor_graph", graph)
    verifier = _Verifier(_held())
    configure_entitlement_verifier(verifier)

    await runtime.module.resume_run(
        _run(request_write=True),
        TenantContext(tenant_id=TENANT, user_id=APPROVER, username=APPROVER),
        ApprovalDecision(decision="approved", decided_by=APPROVER),
    )
    assert [call["fresh"] for call in verifier.calls] == [True]

    verifier.calls.clear()
    await runtime.module.resume_run(
        _run(request_write=False),
        TenantContext(tenant_id=TENANT, user_id=APPROVER, username=APPROVER),
        ApprovalDecision(decision="approved", decided_by=APPROVER),
    )
    assert [call["fresh"] for call in verifier.calls] == [False]


# --------------------------------------------------------------------------------------
# The resume principal: when it cannot be established


@pytest.mark.parametrize(
    "result",
    [
        EntitlementResult(EntitlementOutcome.NOT_CONFIGURED, detail="not wired"),
        EntitlementResult(EntitlementOutcome.UNAVAILABLE, detail="connect error"),
        EntitlementResult(EntitlementOutcome.USER_UNKNOWN, detail="gone"),
        EntitlementResult(EntitlementOutcome.USER_DISABLED, detail="disabled"),
    ],
    ids=["not_configured", "unavailable", "user_unknown", "user_disabled"],
)
@pytest.mark.asyncio
async def test_an_unestablished_authority_pauses_rather_than_narrows(
    monkeypatch, runtime, result
) -> None:
    """Every one of these is a pause, not a narrowing.

    Narrowing and continuing was the earlier behaviour and it was wrong for all four:
    dropping group scope still leaves the run reading every unrestricted document in the
    tenant. A subject nobody can vouch for is not entitled to read the public corpus
    either.
    """
    graph = _Graph(_requester_checkpoint(group_ids=[3, 4]))
    monkeypatch.setattr(runtime.module, "supervisor_graph", graph)
    configure_entitlement_verifier(_Verifier(result))

    with pytest.raises(runtime.module.ResumeBlocked) as raised:
        await runtime.module.resume_run(
            _run(),
            TenantContext(tenant_id=TENANT, user_id=APPROVER, username=APPROVER),
            ApprovalDecision(decision="approved", decided_by=APPROVER),
        )

    assert raised.value.reason == result.outcome.value
    assert graph.invocations == [], "the graph must not be invoked at all"
    blocked = _events(runtime.audits, "run.resume_blocked")
    assert len(blocked) == 1
    assert blocked[0]["payload"]["reason"] == result.outcome.value


@pytest.mark.asyncio
async def test_the_default_configuration_pauses_every_resume(monkeypatch, runtime) -> None:
    """No verifier is not a narrower mode -- it is no mode.

    The distinction matters because "narrower" sounds survivable. It is not: without
    the ability to establish who the requester is now, no resumed run may spend
    authority on their behalf, and the honest state of the feature is *unavailable*.
    """
    graph = _Graph(_requester_checkpoint())
    monkeypatch.setattr(runtime.module, "supervisor_graph", graph)
    configure_entitlement_verifier(None)

    with pytest.raises(runtime.module.ResumeBlocked) as raised:
        await runtime.module.resume_run(
            _run(),
            TenantContext(tenant_id=TENANT, user_id=APPROVER, username=APPROVER),
            ApprovalDecision(decision="approved", decided_by=APPROVER),
        )
    assert raised.value.reason == "not_configured"
    assert graph.invocations == []


@pytest.mark.asyncio
async def test_an_established_but_empty_grant_set_is_not_a_failure(monkeypatch, runtime) -> None:
    """ "We asked and the answer was nothing" is a different fact from "we could not
    ask". It narrows the run to nothing and the run proceeds -- and abstains."""
    graph = _Graph(_requester_checkpoint(group_ids=[3]))
    monkeypatch.setattr(runtime.module, "supervisor_graph", graph)
    configure_entitlement_verifier(
        _Verifier(
            EntitlementResult(
                EntitlementOutcome.VERIFIED,
                roles=frozenset(),
                entity_ids=frozenset(),
                group_ids=frozenset(),
            )
        )
    )

    await runtime.module.resume_run(
        _run(),
        TenantContext(tenant_id=TENANT, user_id=APPROVER, username=APPROVER),
        ApprovalDecision(decision="approved", decided_by=APPROVER),
    )

    update = graph.invocations[0][0].update
    assert update["group_ids"] == []
    assert update["allowed_glpi_entity_ids"] == []
    assert update["roles"] == []


# --------------------------------------------------------------------------------------
# The checkpoint-free path


@pytest.mark.asyncio
async def test_a_run_that_never_reached_the_graph_starts_on_verified_grants(
    monkeypatch, runtime
) -> None:
    """The old code invented ``{viewer, analyst}`` for such a run. Nothing can know the
    scope it would have had, but what the requester holds now is knowable, and that is
    the only defensible starting point."""
    graph = _Graph({})
    monkeypatch.setattr(runtime.module, "supervisor_graph", graph)
    configure_entitlement_verifier(_Verifier(_held(groups=(3,), roles=("viewer",))))

    await runtime.module.continue_incomplete_run(
        _run(), TenantContext(tenant_id=TENANT, user_id=REQUESTER, username=REQUESTER)
    )

    state = graph.invocations[0][0]
    assert state["group_ids"] == [3]
    assert state["roles"] == ["viewer"]
    assert state["allowed_glpi_entity_ids"] == [2]


@pytest.mark.asyncio
async def test_a_mid_run_recovery_enforces_the_intersection_into_the_checkpoint(
    monkeypatch, runtime
) -> None:
    """The checkpoint-free path is not the only one recovery takes.

    A run killed mid-flight has a checkpoint but no pending interrupt, so there is no
    ``Command(resume=...)`` to carry the intersection. Writing it to the state first --
    and only then re-entering with ``None`` -- is what makes the narrowing bind for
    this path too; the alternative is a run that recovery resumed under its old,
    wider scope while the approval path narrowed correctly.
    """
    graph = _Graph(_requester_checkpoint(group_ids=[3, 4], task_plan=_plan_mid_run()))
    monkeypatch.setattr(runtime.module, "supervisor_graph", graph)
    configure_entitlement_verifier(_Verifier(_held(groups=(4,))))

    await runtime.module.continue_incomplete_run(
        _run(), TenantContext(tenant_id=TENANT, user_id=REQUESTER, username=REQUESTER)
    )

    assert len(graph.state_updates) == 1
    written = graph.state_updates[0]
    assert written["roles"] == ["analyst", "viewer"]
    assert written["allowed_glpi_entity_ids"] == [2]
    assert written["group_ids"] == [4]
    assert written["evidence_dirty"] is False
    # ``None`` on the evidence channels is the reset; see the channel test above for why
    # an empty list -- which is what this used to write, and what this case used to
    # assert -- clears nothing at all.
    assert written["knowledge_evidence"] is None
    assert written["data_evidence"] is None
    for product in runtime.module._DERIVED_PRODUCTS:
        assert written[product] == {}, f"{product} was not invalidated"
    assert [task.status for task in TaskPlan.model_validate(written["task_plan"]).tasks] == [
        TaskStatus.PENDING
    ] * 5
    payload, _ = graph.invocations[0]
    assert payload is None, "a mid-run recovery re-enters; it does not resume an interrupt"
    assert _events(runtime.audits, "run.scope_narrowed")[0]["payload"]["effective"][
        "group_ids"
    ] == [4]


@pytest.mark.asyncio
async def test_a_run_that_never_reached_the_graph_pauses_when_unverifiable(
    monkeypatch, runtime
) -> None:
    """Otherwise recovery is a bypass of the whole check: a run could reach the graph
    without its requester's authority ever being established."""
    graph = _Graph({})
    monkeypatch.setattr(runtime.module, "supervisor_graph", graph)
    configure_entitlement_verifier(_Verifier(EntitlementResult(EntitlementOutcome.UNAVAILABLE)))

    with pytest.raises(runtime.module.ResumeBlocked):
        await runtime.module.continue_incomplete_run(
            _run(), TenantContext(tenant_id=TENANT, user_id=REQUESTER, username=REQUESTER)
        )
    assert graph.invocations == []


# --------------------------------------------------------------------------------------
# The point of use


@pytest.mark.asyncio
async def test_the_write_is_refused_when_the_grants_behind_it_are_gone() -> None:
    configure_entitlement_verifier(_Verifier(_held(groups=(4,))))

    with pytest.raises(AuthorityWithdrawn) as raised:
        await require_still_held(
            tenant_id=TENANT,
            user_id=REQUESTER,
            roles={"viewer", "analyst"},
            entity_ids={2},
            group_ids={3, 4},
        )
    assert raised.value.reason == "revoked"
    assert raised.value.revoked["group_ids"] == [3]
    assert raised.value.revoked["roles"] == []


@pytest.mark.asyncio
async def test_the_write_is_refused_when_the_grants_cannot_be_established() -> None:
    configure_entitlement_verifier(_Verifier(EntitlementResult(EntitlementOutcome.USER_DISABLED)))

    with pytest.raises(AuthorityWithdrawn) as raised:
        await require_still_held(
            tenant_id=TENANT,
            user_id=REQUESTER,
            roles={"viewer"},
            entity_ids={2},
            group_ids={3},
        )
    assert raised.value.reason == "user_disabled"


@pytest.mark.asyncio
async def test_the_write_is_refused_when_the_step_needs_a_role_the_requester_lacks() -> None:
    """Nothing was revoked: the requester lost no grant, they simply never held the role
    this operation needs. Verifying an identity answers "who", not "may they"."""
    configure_entitlement_verifier(_Verifier(_held(roles=("viewer",))))

    with pytest.raises(AuthorityWithdrawn) as raised:
        await require_still_held(
            tenant_id=TENANT,
            user_id=REQUESTER,
            roles={"viewer"},
            entity_ids={2},
            group_ids={3},
            required_roles=frozenset({"analyst"}),
        )
    assert raised.value.reason == "insufficient"
    assert raised.value.missing["roles"] == ["analyst"]
    assert raised.value.revoked == {}


@pytest.mark.asyncio
async def test_the_write_is_refused_when_no_entity_is_in_scope() -> None:
    """The only action this platform has addresses a ticket, and a ticket lives in a
    GLPI entity. A scope with no entity in it cannot address one -- and an empty set
    must read as *no* entity rather than as *any* entity."""
    configure_entitlement_verifier(_Verifier(_held(entity_ids=())))

    with pytest.raises(AuthorityWithdrawn) as raised:
        await require_still_held(
            tenant_id=TENANT,
            user_id=REQUESTER,
            roles={"viewer", "analyst"},
            entity_ids=set(),
            group_ids={3},
            required_roles=frozenset({"analyst"}),
        )
    assert raised.value.reason == "insufficient"
    assert raised.value.missing["entity_ids"] == ["<no GLPI entity in scope>"]


@pytest.mark.asyncio
async def test_the_write_proceeds_when_every_grant_is_still_held() -> None:
    verifier = _Verifier(_held(groups=(3, 4)))
    configure_entitlement_verifier(verifier)

    await require_still_held(
        tenant_id=TENANT,
        user_id=REQUESTER,
        roles={"viewer", "analyst"},
        entity_ids={2},
        group_ids={3},
    )
    assert [call["fresh"] for call in verifier.calls] == [True]


# --------------------------------------------------------------------------------------
# The realm reader


def _realm(handler) -> KeycloakEntitlementVerifier:
    return KeycloakEntitlementVerifier(
        base_url="http://keycloak.test",
        username="servicemind-ops",
        password="unused-in-tests",
        realm="servicemind",
        cache_seconds=30.0,
        transport=httpx.MockTransport(handler),
    )


def _stub_realm(*, user_response, role_names=("viewer", "analyst"), seen: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/protocol/openid-connect/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 60})
        if request.url.path.endswith("/role-mappings/realm"):
            return httpx.Response(200, json=[{"name": name} for name in role_names])
        return user_response

    return handler


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_response", "expected", "cacheable"),
    [
        (
            httpx.Response(
                200,
                json={
                    "enabled": True,
                    "attributes": {"glpi_entity_ids": ["2"], "glpi_group_ids": ["3", "4"]},
                },
            ),
            EntitlementOutcome.VERIFIED,
            True,
        ),
        (httpx.Response(404, json={}), EntitlementOutcome.USER_UNKNOWN, True),
        (
            httpx.Response(200, json={"enabled": False, "attributes": {}}),
            EntitlementOutcome.USER_DISABLED,
            True,
        ),
        (httpx.Response(500, json={}), EntitlementOutcome.UNAVAILABLE, False),
    ],
    ids=["verified", "unknown", "disabled", "server_error"],
)
async def test_the_realm_reader_maps_each_answer_to_its_own_outcome(
    user_response, expected, cacheable
) -> None:
    seen: list[str] = []
    verifier = _realm(_stub_realm(user_response=user_response, seen=seen))

    first = await verifier.verify(TENANT, REQUESTER)
    assert first.outcome is expected
    if expected is EntitlementOutcome.VERIFIED:
        assert first.roles == {"viewer", "analyst"}
        assert first.entity_ids == {2}
        assert first.group_ids == {3, 4}

    # The subject is addressed by the immutable id the token carried, not by a name.
    # A username is reassignable: a renamed account would resolve to whoever holds the
    # old name now, and a recycled one to whoever holds it next.
    assert f"GET /admin/realms/servicemind/users/{REQUESTER}" in seen

    # A failure is not an answer and must not be remembered as one; an answer is.
    # ``from_cache`` is the signal, and it is the one the audit row carries: the answer
    # itself is identical either way, so nothing else distinguishes "we just asked" from
    # "we asked a while ago" -- which is precisely the fact a revocation lag hides in.
    assert first.from_cache is False
    second = await verifier.verify(TENANT, REQUESTER)
    assert second.outcome is expected
    assert second.from_cache is cacheable
    assert (second.roles, second.entity_ids, second.group_ids) == (
        first.roles,
        first.entity_ids,
        first.group_ids,
    )
    if cacheable:
        assert second.cache_window_seconds == 30.0
    third = await verifier.verify(TENANT, REQUESTER, fresh=True)
    assert third.outcome is expected
    assert third.from_cache is False, "a bypassed cache must not report a cached answer"


@pytest.mark.asyncio
async def test_no_glpi_entity_in_scope_is_not_a_tenant_wide_integration(monkeypatch) -> None:
    """The second boundary, at the integration that chooses which GLPI entity is read.

    ``resolve_glpi_config`` returns the tenant's integration *pinned to the entity the
    caller is allowed to work in*, and it refuses when that entity is outside their
    scope. The dangerous reading of an empty grant set is "no restriction", which here
    would mean the integration's own entity is used and a requester entitled to nothing
    reads whatever the tenant's credentials can see. Nothing in this module's tests
    covered this gate before.
    """
    from servicemind.integrations.glpi import resolver

    class _Repository:
        def __init__(self, tenant_id):
            assert tenant_id == TENANT

        async def get_glpi_integration(self):
            return SimpleNamespace(
                entity_id=2,
                base_url="https://glpi.invalid",
                api_version="2.0",
                client_id_encrypted="c",
                client_secret_encrypted="s",
                username_encrypted="u",
                password_encrypted="p",
                profile_id=4,
            )

    class _Cipher:
        def decrypt(self, value):
            return "plaintext"

    monkeypatch.setattr(resolver, "ServiceMindRepository", _Repository)
    monkeypatch.setattr(resolver, "CredentialCipher", _Cipher)

    def context(**overrides) -> TenantContext:
        fields = {
            "tenant_id": TENANT,
            "user_id": REQUESTER,
            "username": REQUESTER,
            "roles": {"viewer", "analyst"},
            "allowed_glpi_entity_ids": {2},
        }
        fields.update(overrides)
        return TenantContext(**fields)

    assert (await resolver.resolve_glpi_config(context())).entity_id == 2
    with pytest.raises(PermissionError):
        await resolver.resolve_glpi_config(context(allowed_glpi_entity_ids=set()))
    with pytest.raises(PermissionError):
        await resolver.resolve_glpi_config(context(allowed_glpi_entity_ids={9}))


def test_a_tool_bound_to_an_entity_is_invisible_without_that_entity() -> None:
    """Same boundary one layer up, where it decides what a run may even ask for."""
    from servicemind.tool_platform.contracts import (
        DataClassification,
        ToolAccess,
        ToolDefinition,
        ToolRisk,
    )
    from servicemind.tool_platform.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="glpi.get_ticket",
            version="1.0.0",
            provider="glpi",
            input_schema={},
            output_schema={},
            read_write_type=ToolAccess.READ,
            risk_level=ToolRisk.LOW,
            allowed_roles=frozenset({"analyst"}),
            allowed_entities=frozenset({2}),
            idempotency_strategy="none",
            verification_strategy="none",
            data_classification=DataClassification.INTERNAL,
        )
    )

    def visible(entity_ids: frozenset[int]):
        return registry.visible(roles=frozenset({"analyst"}), entity_ids=entity_ids)

    assert [item.name for item in visible(frozenset({2}))] == ["glpi.get_ticket"]
    assert visible(frozenset()) == ()
    assert visible(frozenset({9})) == ()


@pytest.mark.asyncio
async def test_an_unreachable_realm_is_unavailable_not_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    result = await _realm(handler).verify(TENANT, REQUESTER)
    assert result.outcome is EntitlementOutcome.UNAVAILABLE
    assert not result.established


# --------------------------------------------------------------------------------------
# The approval endpoint: what a paused resume is allowed to have caused


@pytest.fixture
def approval_endpoint(monkeypatch):
    """Drive ``approve_run`` against a fake store, recording the order of side effects."""
    from fastapi import HTTPException

    from servicemind import api

    run = _run(status=RunStatus.WAITING_APPROVAL.value)
    action = SimpleNamespace(
        id=uuid4(),
        action_type="append_ticket_followup",
        target_id=run.ticket_id,
        arguments={"content": "…"},
        risk_level="low",
        requires_approval=True,
        action_hash="a" * 64,
        status="proposed",
        intent_version="v1",
        policy_version=None,
        review_digest=None,
        evidence_digest=None,
        evidence_refs=[],
        expires_at=None,
        dry_run_preview=None,
    )
    changed: list[str] = []

    class _Repository:
        def __init__(self, tenant_id):
            assert tenant_id == TENANT

        async def get_run(self, run_id):
            return run

        async def get_action_intent(self, run_id):
            return action

        async def record_approval(self, **kwargs):
            changed.append("approval_recorded")
            return SimpleNamespace(id=uuid4()), True

        async def audit(self, **kwargs):
            changed.append(f"audit:{kwargs['event_type']}")

        async def update_run(self, run_id, status, error=None):
            changed.append(f"status:{status.value}")
            return run

    monkeypatch.setattr(api, "ServiceMindRepository", _Repository)
    request = api.ApprovalRequest(
        decision="approved", expected_action_hash=action.action_hash, comment=None
    )
    context = TenantContext(
        tenant_id=TENANT,
        user_id=APPROVER,
        username=APPROVER,
        roles={"approver", "analyst"},
        allowed_glpi_entity_ids={2},
        allowed_glpi_group_ids={3, 4},
    )
    return SimpleNamespace(
        api=api,
        run=run,
        action=action,
        request=request,
        context=context,
        changed=changed,
        HTTPException=HTTPException,
        monkeypatch=monkeypatch,
    )


@pytest.mark.asyncio
async def test_a_paused_resume_records_no_approval(approval_endpoint) -> None:
    """The finding the plan asked to prove: a pause must not spend the decision.

    Recording the approval first persists the decision, flips the action to APPROVED,
    and enqueues its outbox event -- all before the resume that can refuse. The residue
    is an approved action with nothing to execute it, and an approval row that makes the
    retry short-circuit: the run is never resumable again, and a human's decision was
    consumed by an identity-provider hiccup.
    """
    fx = approval_endpoint

    async def blocked(run, context):
        raise fx.api.ResumeBlocked("unavailable", "connect error")

    fx.monkeypatch.setattr(fx.api, "resolve_resume_scope", blocked)

    with pytest.raises(fx.HTTPException) as raised:
        await fx.api.approve_run(fx.run.id, fx.request, fx.context)

    assert raised.value.status_code == 409
    assert fx.changed == [], "nothing may be written for a resume that did not happen"
    assert fx.run.status == RunStatus.WAITING_APPROVAL.value


@pytest.mark.asyncio
async def test_a_voided_action_records_no_approval(approval_endpoint) -> None:
    """A withdrawn action must not carry the approver's 'yes' into the record."""
    fx = approval_endpoint
    resumed: list[str] = []

    async def voiding(run, context):
        return SimpleNamespace(void=True, update={"group_ids": [4]})

    async def fake_resume(run, context, decision, *, scope=None):
        resumed.append("resume")

    fx.monkeypatch.setattr(fx.api, "resolve_resume_scope", voiding)
    fx.monkeypatch.setattr(fx.api, "resume_run", fake_resume)

    with pytest.raises(fx.HTTPException) as raised:
        await fx.api.approve_run(fx.run.id, fx.request, fx.context)

    assert raised.value.status_code == 409
    assert resumed == ["resume"], "the run is put back on the re-derivation path"
    assert fx.changed == [], "the approval is not recorded against a withdrawn action"


@pytest.mark.asyncio
async def test_an_appliable_approval_is_recorded_and_then_applied(approval_endpoint) -> None:
    """The order the other two are measured against: persist, then apply -- and the
    scope that decided it is the scope that is applied, not a second resolution that
    could disagree with the first."""
    fx = approval_endpoint
    scope = SimpleNamespace(void=False, update={})
    applied: list[object] = []

    async def resolving(run, context):
        return scope

    async def fake_resume(run, context, decision, *, scope=None):
        applied.append(scope)

    fx.monkeypatch.setattr(fx.api, "resolve_resume_scope", resolving)
    fx.monkeypatch.setattr(fx.api, "resume_run", fake_resume)

    await fx.api.approve_run(fx.run.id, fx.request, fx.context)

    assert applied == [scope]
    assert fx.changed[0] == "approval_recorded"
    assert "audit:approval.approved" in fx.changed


@pytest.mark.asyncio
async def test_a_refusal_does_not_need_the_requester_reestablished(approval_endpoint) -> None:
    """Declining is the one decision an identity-provider outage must not be able to stop.

    The refusal path and the approval path share an endpoint, and they shared its gate:
    ``resolve_resume_scope`` ran before the decision was looked at, so a deployment whose
    verifier was unavailable -- or, as shipped by default, simply not configured --
    returned 409 for a rejection too. Nothing is protected by that: a refusal reaches
    ``finalize``, which neither retrieves nor writes, so there is no evidence to scope and
    no authority to spend. What it costs is the ability to say no during an outage, with
    the run left in ``WAITING_APPROVAL`` until the identity provider comes back.

    The assertion is that the verifier is never asked, so this holds for an unreachable
    verifier exactly as it holds for an absent one.
    """
    fx = approval_endpoint
    fx.request = fx.api.ApprovalRequest(
        decision="rejected", expected_action_hash=fx.action.action_hash, comment="not this change"
    )
    declined: list[object] = []

    async def scope_must_not_be_resolved(run, context):
        raise AssertionError("a refusal must not require the requester's authority")

    async def fake_decline(run, context, decision):
        declined.append(decision)

    fx.monkeypatch.setattr(fx.api, "resolve_resume_scope", scope_must_not_be_resolved)
    fx.monkeypatch.setattr(fx.api, "decline_run", fake_decline)

    await fx.api.approve_run(fx.run.id, fx.request, fx.context)

    assert [item.decision for item in declined] == ["rejected"]
    assert fx.changed == ["approval_recorded", "audit:approval.rejected"]


@pytest.mark.asyncio
async def test_the_refusal_path_cannot_be_borrowed_to_approve(monkeypatch, runtime) -> None:
    """The shortcut is not a parameter, so an executing resume cannot reach it.

    ``decline_run`` is safe precisely because refusing spends nothing. An approval does
    execute, and it is the case where a revoked grant has to be noticed -- so it must not
    be able to take this path by passing a decision that was not a refusal.
    """
    graph = _Graph(_requester_checkpoint())
    monkeypatch.setattr(runtime.module, "supervisor_graph", graph)

    with pytest.raises(ValueError):
        await runtime.module.decline_run(
            _run(status=RunStatus.WAITING_APPROVAL.value),
            TenantContext(
                tenant_id=TENANT,
                user_id=APPROVER,
                username=APPROVER,
                roles={"approver"},
                allowed_glpi_entity_ids={2},
                allowed_glpi_group_ids={3, 4},
            ),
            ApprovalDecision(decision="approved", decided_by=APPROVER),
        )

    assert graph.invocations == [], "the graph is not resumed for a decision it cannot take"


@pytest.mark.asyncio
async def test_a_refusal_resumes_as_the_requester_without_narrowing(monkeypatch, runtime) -> None:
    """Two things the refusal resume must not quietly do.

    It must not run as the approver: the approver's grant is not the run's, and the
    endpoint receiving their token is not a reason to hand it over. And it must not
    write a narrowed scope into the checkpoint: a refusal ends the run, so an ``update``
    would be a claim that the run continued under the intersection, and a ``void`` would
    put the approver's words into a disposition they never gave.
    """
    graph = _Graph(_requester_checkpoint())
    monkeypatch.setattr(runtime.module, "supervisor_graph", graph)

    await runtime.module.decline_run(
        _run(status=RunStatus.WAITING_APPROVAL.value),
        TenantContext(
            tenant_id=TENANT,
            user_id=APPROVER,
            username=APPROVER,
            roles={"approver"},
            allowed_glpi_entity_ids={2},
            allowed_glpi_group_ids={3, 4},
        ),
        ApprovalDecision(decision="rejected", decided_by=APPROVER),
    )

    payload, config = graph.invocations[0]
    assert payload.resume == {"decision": "rejected", "decided_by": APPROVER, "comment": None}
    assert payload.update == {}, "a terminal refusal has no scope to enforce"
    assert config["configurable"]["user_id"] == REQUESTER, "not the approver's identity"
    assert graph.state_updates == []


@pytest.fixture
def review_endpoint(monkeypatch):
    """The same question asked of the other resume boundary, which has the same shape:
    the escalation answer is a decision too, and a decision that cannot be applied must
    not be spent."""
    from fastapi import HTTPException

    from servicemind import api

    run = _run(status=RunStatus.WAITING_REVIEW.value)
    changed: list[str] = []

    class _Repository:
        def __init__(self, tenant_id):
            assert tenant_id == TENANT

        async def get_run(self, run_id):
            return run

        async def get_action_intent(self, run_id):
            return None

        async def audit(self, **kwargs):
            changed.append(f"audit:{kwargs['event_type']}")

        async def update_run(self, run_id, status, error=None):
            changed.append(f"status:{status.value}")
            return run

    monkeypatch.setattr(api, "ServiceMindRepository", _Repository)
    return SimpleNamespace(
        api=api,
        run=run,
        request=api.ReviewResolutionRequest(decision="continue", comment=None),
        context=TenantContext(
            tenant_id=TENANT,
            user_id=APPROVER,
            username=APPROVER,
            roles={"approver", "analyst"},
            allowed_glpi_entity_ids={2},
            allowed_glpi_group_ids={3, 4},
        ),
        changed=changed,
        HTTPException=HTTPException,
        monkeypatch=monkeypatch,
    )


@pytest.mark.asyncio
async def test_a_paused_review_resolution_writes_nothing(review_endpoint) -> None:
    """A human's escalation answer is theirs until the resume that applies it happens.

    Auditing the answer and flipping the run to ``running`` before the scope is resolved
    leaves a run that says it is running with no graph behind it, and an audit trail
    that records a decision nobody applied.
    """
    fx = review_endpoint

    async def blocked(run, context):
        raise fx.api.ResumeBlocked("unavailable", "connect error")

    fx.monkeypatch.setattr(fx.api, "resolve_resume_scope", blocked)

    with pytest.raises(fx.HTTPException) as raised:
        await fx.api.resolve_review_escalation(fx.run.id, fx.request, fx.context)

    assert raised.value.status_code == 409
    assert fx.changed == []
    assert fx.run.status == RunStatus.WAITING_REVIEW.value


def test_the_default_configuration_installs_no_verifier(monkeypatch) -> None:
    """The serving path is not given realm-admin credentials by default.

    The default under test is the one in the code, so the settings are cleared and the
    wiring re-run rather than the ambient process being read. A deployment that has
    deliberately configured a verifier holds credentials in its ``.env``, and a test that
    read the environment would fail on exactly the deployments that took the trouble to
    configure one -- which is the opposite of what this property is for.
    """
    from servicemind.security import auth

    for name in (
        "SERVICEMIND_KEYCLOAK_ADMIN_URL",
        "SERVICEMIND_KEYCLOAK_ADMIN_USERNAME",
        "SERVICEMIND_KEYCLOAK_ADMIN_PASSWORD",
    ):
        monkeypatch.setattr(auth.settings, name, None)

    assert auth.install_entitlement_verifier() is None
    assert current_entitlement_verifier() is None


def test_a_configured_deployment_installs_a_keycloak_verifier(monkeypatch) -> None:
    """The wiring that makes the re-verification above reachable in a real deployment.

    Without it every resume pauses on ``not_configured`` -- correct, but with no signal
    that the precise path is dead code and the approval queue is closed.
    """
    from servicemind.security import auth

    monkeypatch.setattr(auth.settings, "SERVICEMIND_KEYCLOAK_ADMIN_URL", "http://127.0.0.1:8090")
    monkeypatch.setattr(auth.settings, "SERVICEMIND_KEYCLOAK_ADMIN_USERNAME", "servicemind-ops")
    monkeypatch.setattr(auth.settings, "SERVICEMIND_KEYCLOAK_ADMIN_PASSWORD", SecretStr("s3cret"))

    installed = auth.install_entitlement_verifier()

    assert isinstance(installed, KeycloakEntitlementVerifier)
    assert current_entitlement_verifier() is installed

    monkeypatch.setattr(auth.settings, "SERVICEMIND_KEYCLOAK_ADMIN_URL", None)
    assert auth.install_entitlement_verifier() is None
    assert current_entitlement_verifier() is None
