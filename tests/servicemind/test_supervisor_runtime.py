from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from servicemind.agents.action import ActionAgent
from servicemind.context.contracts import ContextAgent, ContextAssemblyError
from servicemind.domain.analysis import AnalysisResult, ProposedAction
from servicemind.domain.evidence import Evidence, EvidenceSourceType
from servicemind.domain.models import ExecutionResult
from servicemind.domain.review import ReviewDecision, ReviewResult, RiskLevel
from servicemind.domain.supervisor import SupervisorAction, SupervisorDecision
from servicemind.domain.task import (
    TASK_OUTPUT_REF_MAX_LENGTH,
    AgentName,
    Budget,
    ErrorPolicy,
    Task,
    TaskPlan,
    TaskStatus,
)
from servicemind.orchestration.dispatcher import TaskDispatcher
from servicemind.orchestration.registry import supervisor_contract
from servicemind.orchestration.router import FastPathRouter
from servicemind.orchestration.supervisor_policy import (
    SupervisorPolicy,
    SupervisorPolicyError,
)
from servicemind.orchestration.supervisor_workflow import (
    SupervisorRuntimeServices,
    _bounded_output_ref,
    build_supervisor_graph,
)
from servicemind.security.entitlements import (
    configure_entitlement_verifier,
    current_entitlement_verifier,
)

TENANT = UUID("11111111-1111-4111-8111-111111111111")


class _ConfirmingVerifier:
    """Confirms whatever this file's ``initial()`` state records, and nothing more."""

    async def verify(self, tenant_id, user_id, *, fresh=False):
        from servicemind.security.entitlements import EntitlementOutcome, EntitlementResult

        assert tenant_id == TENANT
        assert fresh is True, "the pre-write check must not be served from a cache"
        return EntitlementResult(
            EntitlementOutcome.VERIFIED,
            roles=frozenset({"viewer", "analyst"}),
            entity_ids=frozenset({1}),
            group_ids=frozenset(),
        )


class FakeRepository:
    events: list[tuple[str, dict]] = []
    results: dict[UUID, dict] = {}

    def __init__(self, tenant_id: UUID) -> None:
        assert tenant_id == TENANT

    @classmethod
    def reset(cls) -> None:
        cls.events = []
        cls.results = {}

    async def update_run(self, run_id, status, *, result=None, error=None):
        if result is not None:
            self.results[run_id] = result
        return SimpleNamespace(id=run_id, status=status.value, result=result, error=error)

    async def append_event(self, run_id, event_type, payload):
        self.events.append((event_type, payload))
        return SimpleNamespace()

    async def audit(self, *, event_type, payload, **kwargs):
        self.events.append((event_type, payload))
        return SimpleNamespace()

    async def save_action_intent(self, **kwargs):
        return SimpleNamespace(id=uuid4(), **kwargs)


def evidence(source: EvidenceSourceType, resource_type: str, content: str) -> Evidence:
    return Evidence.create(
        tenant_id=TENANT,
        source_type=source,
        source_ref=f"{source.value}://fixture/{resource_type}",
        resource_type=resource_type,
        resource_id="2",
        content=content,
        provider="supervisor-test",
        retrieval_method="fixture",
        metadata=(
            {
                "ticket_facts": {
                    "id": 2,
                    "name": "VPN MFA outage",
                    "impact": 3,
                    "urgency": 4,
                    "priority": 4,
                }
            }
            if resource_type == "ticket"
            else {}
        ),
    )


class FakeData:
    calls = 0

    async def get_ticket_evidence(self, context, ticket_id):
        type(self).calls += 1
        return [
            evidence(EvidenceSourceType.GLPI, "ticket", "VPN MFA incident facts"),
            evidence(
                EvidenceSourceType.GLPI,
                "support_group",
                "GLPI support group: Network Team",
            ),
        ]


class FakeKnowledge:
    calls = 0

    async def retrieve(self, *, tenant_id, query, retrieval_round=0):
        type(self).calls += 1
        return [
            evidence(
                EvidenceSourceType.KNOWLEDGE,
                "runbook",
                "Network Team owns VPN faults",
            )
        ]


class FakeAnalysis:
    async def analyze_evidence(self, joined, goal, *, request_write, ticket_id):
        actions = []
        if request_write:
            actions = [
                ProposedAction(
                    operation="append_ticket_followup",
                    resource_type="ticket",
                    resource_id=str(ticket_id),
                    evidence_refs=joined.evidence_refs,
                    risk_level=RiskLevel.LOW,
                )
            ]
        return AnalysisResult(
            classification="network/vpn",
            impact=3,
            urgency=4,
            priority=4,
            recommended_group="Network Team",
            recurring_incident=False,
            problem_recommendation="Collect recurrence evidence.",
            change_recommendation="No change supported.",
            proposed_actions=actions,
            reasoning_summary="Evidence supports Network Team.",
            evidence_refs=joined.evidence_refs,
            confidence=0.8,
            source="supervisor-test",
        )


class FakeReviewer:
    async def review(self, *, analysis, evidence, **kwargs):
        return ReviewResult(
            decision=ReviewDecision.PASSED,
            risk_level=RiskLevel.LOW,
            feedback="Evidence and policy checks passed.",
            reviewed_evidence_refs=evidence.evidence_refs,
        )


class FakePlanner:
    def __init__(self) -> None:
        self.goals: list[str] = []

    async def create_plan(self, *, goal, ticket_id, request_write, correction=None):
        self.goals.append(goal)
        due = datetime.now(UTC) + timedelta(minutes=5)
        budget = Budget(deadline=due)
        tasks = [
            Task(
                task_id="T1",
                agent=AgentName.DATA,
                task_type="get_ticket",
                input={"objective": "Read ticket", "ticket_id": ticket_id},
                error_policy=ErrorPolicy.RETRY,
                deadline=due,
            )
        ]
        evidence_ids = ["T1"]
        if "runbook" in goal.casefold():
            tasks.append(
                Task(
                    task_id="T2",
                    agent=AgentName.KNOWLEDGE,
                    task_type="retrieve_knowledge",
                    input={"objective": "Read runbook", "ticket_id": ticket_id},
                    error_policy=ErrorPolicy.RETRY,
                    deadline=due,
                )
            )
            evidence_ids.append("T2")
        next_id = len(tasks) + 1
        analysis_id = f"T{next_id}"
        reviewer_id = f"T{next_id + 1}"
        tasks.extend(
            [
                Task(
                    task_id=analysis_id,
                    agent=AgentName.ANALYSIS,
                    task_type="analyze_ticket",
                    depends_on=evidence_ids,
                    deadline=due,
                ),
                Task(
                    task_id=reviewer_id,
                    agent=AgentName.REVIEWER,
                    task_type="review_analysis",
                    depends_on=[analysis_id],
                    deadline=due,
                ),
            ]
        )
        if request_write:
            tasks.append(
                Task(
                    task_id=f"T{next_id + 2}",
                    agent=AgentName.ACTION,
                    task_type="propose_followup",
                    depends_on=[reviewer_id],
                    deadline=due,
                )
            )
        return TaskPlan(
            goal=goal,
            tasks=tasks,
            max_parallel=2,
            max_steps=budget.max_steps,
            max_replans=budget.max_replans,
            deadline=due,
            budget=budget,
        )

    async def revise_plan(self, **kwargs):
        return kwargs["previous"]


class StateDrivenSupervisor:
    def __init__(
        self,
        invalid_first: int | bool = 0,
        override: dict[int, SupervisorAction] | None = None,
    ) -> None:
        self.calls = 0
        self.feedback: list[str | None] = []
        # An int rather than a flag so a test can say how many decisions in a row come
        # back illegal -- the bound on that run of rejections is what is under test.
        self.invalid_first = int(invalid_first)
        self.override = override or {}

    async def decide(self, state_view, *, policy_feedback=None):
        self.calls += 1
        self.feedback.append(policy_feedback)
        if self.calls <= self.invalid_first:
            return SupervisorDecision(
                action=SupervisorAction.HANDOFF_ACTION,
                rationale_summary="Intentionally invalid decision.",
                confidence=1,
            )
        legal = set(state_view["legal_actions"])
        forced = self.override.get(self.calls)
        if forced is not None:
            if forced.value not in legal:
                raise AssertionError(f"{forced.value} is not legal: {sorted(legal)}")
            return SupervisorDecision(
                action=forced,
                selected_task_ids=[],
                rationale_summary=f"Forced {forced.value} decision.",
                confidence=1,
            )
        for action in (
            SupervisorAction.PLAN,
            SupervisorAction.DISPATCH,
            SupervisorAction.JOIN_EVIDENCE,
            SupervisorAction.ANALYZE,
            SupervisorAction.REVIEW,
            SupervisorAction.HANDOFF_ACTION,
            SupervisorAction.FINALIZE,
            SupervisorAction.RETRIEVE_MORE,
            SupervisorAction.REPLAN,
            SupervisorAction.ESCALATE,
        ):
            if action.value in legal:
                return SupervisorDecision(
                    action=action,
                    selected_task_ids=(
                        [item["task_id"] for item in state_view["ready_tasks"]]
                        if action is SupervisorAction.DISPATCH
                        else [
                            item["task_id"]
                            for item in state_view["ready_tasks"]
                            if item["agent"]
                            == ("analysis" if action is SupervisorAction.ANALYZE else "reviewer")
                        ][:1]
                        if action in {SupervisorAction.ANALYZE, SupervisorAction.REVIEW}
                        else []
                    ),
                    rationale_summary=f"Progress workflow with {action.value}.",
                    confidence=1,
                )
        raise AssertionError(legal)


class CountingAction:
    def __init__(self) -> None:
        self.calls = 0
        self.delegate = ActionAgent()

    def propose_from_handoff(self, handoff, analysis, *, ticket_id):
        self.calls += 1
        return self.delegate.propose_from_handoff(handoff, analysis, ticket_id=ticket_id)


class FakeExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, context, intent):
        self.calls += 1
        return ExecutionResult(
            tool_name="glpi_append_ticket_followup",
            followup_id=200,
            ticket_id=intent.target_id,
            verified=True,
        )


def initial(goal: str, *, write: bool = False) -> dict:
    return {
        "run_id": str(uuid4()),
        "tenant_id": str(TENANT),
        "user_id": "user-1",
        "username": "analyst",
        "roles": ["viewer", "analyst"],
        "allowed_glpi_entity_ids": [1],
        "thread_id": str(uuid4()),
        "ticket_id": 2,
        "raw_request": goal,
        "goal": goal,
        "request_write": write,
        "data_evidence": [],
        "knowledge_evidence": [],
        "branch_timings": [],
        "branch_errors": [],
        "task_completions": [],
        "trajectory": [],
        "control": {},
        "control_owner": "supervisor",
        "active_agent": "router",
        "evidence_dirty": False,
        "plan_revision": 0,
    }


def services(supervisor=None, planner=None, action=None, executor=None, phase5=None, reviewer=None):
    return SupervisorRuntimeServices(
        router=FastPathRouter(),
        supervisor=supervisor or StateDrivenSupervisor(),
        planner=planner or FakePlanner(),
        policy=SupervisorPolicy(),
        dispatcher=TaskDispatcher(),
        data=FakeData(),
        knowledge=FakeKnowledge(),
        analysis=FakeAnalysis(),
        reviewer=reviewer or FakeReviewer(),
        action=action or CountingAction(),
        executor=executor or FakeExecutor(),
        repository_factory=FakeRepository,
        **({"phase5": phase5} if phase5 is not None else {}),
    )


@pytest.fixture(autouse=True)
def reset_fakes():
    FakeRepository.reset()
    FakeData.calls = 0
    FakeKnowledge.calls = 0


@pytest.fixture(autouse=True)
def authority_still_held():
    """The identity provider confirms exactly the grants ``initial()`` records.

    ``execute_node`` re-verifies the requester's authority before it writes, because a
    grant can be revoked between the approval and the moment the write is spent. These
    tests drive the graph by hand rather than through ``runtime.resume_run`` -- which is
    the only path that reaches the node in production, and which establishes a verifier
    before it gets here -- so the confirmation they would have arrived with has to be
    supplied. Returning a *narrower* set here is what the revocation tests assert on;
    this fixture is the case where nothing changed.
    """
    original = current_entitlement_verifier()
    configure_entitlement_verifier(_ConfirmingVerifier())
    yield
    configure_entitlement_verifier(original)


def test_supervisor_policy_rejects_handoff_before_review() -> None:
    assert supervisor_contract.allowed_tools == ()
    assert supervisor_contract.read_write_scope == "control_only"
    state = initial("Analyze VPN incident")
    decision = SupervisorDecision(
        action=SupervisorAction.HANDOFF_ACTION,
        rationale_summary="Unsafe premature handoff.",
        confidence=1,
    )
    with pytest.raises(SupervisorPolicyError, match="illegal"):
        SupervisorPolicy().validate(decision, state)


def test_supervisor_policy_rejects_parallel_dispatch_without_tool_budget() -> None:
    state = initial("Analyze VPN with the relevant runbook")
    due = datetime.now(UTC) + timedelta(minutes=5)
    budget = Budget(max_tool_calls=1, deadline=due)
    plan = TaskPlan(
        goal=state["goal"],
        tasks=[
            Task(
                task_id="T1",
                agent=AgentName.DATA,
                task_type="get_ticket",
                deadline=due,
            ),
            Task(
                task_id="T2",
                agent=AgentName.KNOWLEDGE,
                task_type="retrieve_knowledge",
                deadline=due,
            ),
        ],
        max_parallel=2,
        max_steps=budget.max_steps,
        max_replans=budget.max_replans,
        deadline=due,
        budget=budget,
    )
    state["task_plan"] = plan.model_dump(mode="json", by_alias=True)
    decision = SupervisorDecision(
        action=SupervisorAction.DISPATCH,
        selected_task_ids=["T1", "T2"],
        rationale_summary="Run both evidence tasks.",
        confidence=1,
    )
    with pytest.raises(SupervisorPolicyError, match="fewer remaining tool calls"):
        SupervisorPolicy().validate(decision, state)


@pytest.mark.asyncio
async def test_real_supervisor_node_drives_dynamic_loop() -> None:
    supervisor = StateDrivenSupervisor()
    graph = build_supervisor_graph(services(supervisor=supervisor))
    result = await graph.ainvoke(initial("Analyze VPN with the relevant runbook"))
    assert result["final_result"]["review"]["decision"] == "passed"
    assert supervisor.calls >= 6
    assert result["trajectory"].count("supervisor") == supervisor.calls
    actions = [
        item.removeprefix("decision:")
        for item in result["trajectory"]
        if item.startswith("decision:")
    ]
    assert actions == ["plan", "dispatch", "join_evidence", "analyze", "review", "finalize"]


@pytest.mark.asyncio
async def test_dynamic_planner_produces_different_dags_for_different_goals() -> None:
    planner = FakePlanner()
    first = await build_supervisor_graph(services(planner=planner)).ainvoke(
        initial("Analyze current ticket facts")
    )
    second = await build_supervisor_graph(services(planner=planner)).ainvoke(
        initial("Analyze current ticket with the relevant runbook")
    )
    first_agents = [task["agent"] for task in first["final_result"]["task_plan"]["tasks"]]
    second_agents = [task["agent"] for task in second["final_result"]["task_plan"]["tasks"]]
    assert first_agents == ["data", "analysis", "reviewer"]
    assert second_agents == ["data", "knowledge", "analysis", "reviewer"]


@pytest.mark.asyncio
async def test_policy_rejection_is_returned_to_supervisor_for_self_correction() -> None:
    supervisor = StateDrivenSupervisor(invalid_first=True)
    result = await build_supervisor_graph(services(supervisor=supervisor)).ainvoke(
        initial("Analyze VPN with the relevant runbook")
    )
    assert result["final_result"]["review"]["decision"] == "passed"
    assert supervisor.feedback[0] is None
    assert supervisor.feedback[1] and "illegal" in supervisor.feedback[1]


@pytest.mark.asyncio
async def test_two_rejections_in_a_row_are_still_correctable() -> None:
    """The second rejection is the one that used to end the run outright.

    Each rejection hands back the legal set for the state the model was looking at, so
    the next attempt is informed rather than repeated -- and the second correction can
    fail on a *different* rule (the action legal, its argument not), which is what
    happened on ACC-03 (2026-09-23): a healthy read-only run whose second proposal was
    already the right action selected wrongly finalized with
    ``supervisor_policy_failure`` and lost its plan, evidence and analysis with it.
    """
    supervisor = StateDrivenSupervisor(invalid_first=2)

    result = await build_supervisor_graph(services(supervisor=supervisor)).ainvoke(
        initial("Analyze VPN with the relevant runbook")
    )

    assert result["final_result"]["termination_code"] is None
    assert result["final_result"]["review"]["decision"] == "passed"
    assert supervisor.feedback[1] and "illegal" in supervisor.feedback[1]
    assert supervisor.feedback[2] and "illegal" in supervisor.feedback[2]
    assert supervisor.feedback[0] is None


@pytest.mark.asyncio
async def test_a_control_plane_that_cannot_choose_still_terminates() -> None:
    """The bound is finite, and reaching it must remain a recorded terminal.

    Raising the allowance cannot turn into "retry until the model agrees": the run's
    model-call budget is the outer bound, and this terminal is what an operator reads
    when a control plane genuinely cannot name a legal next transition.
    """
    supervisor = StateDrivenSupervisor(invalid_first=3)

    result = await build_supervisor_graph(services(supervisor=supervisor)).ainvoke(
        initial("Analyze VPN with the relevant runbook")
    )

    assert result["final_result"]["termination_code"] == "supervisor_policy_failure"
    assert supervisor.calls == 3


@pytest.mark.asyncio
async def test_handoff_changes_control_owner_and_enters_existing_harness() -> None:
    supervisor, action, executor = StateDrivenSupervisor(), CountingAction(), FakeExecutor()
    graph = build_supervisor_graph(
        services(supervisor=supervisor, action=action, executor=executor)
    )
    graph.checkpointer = InMemorySaver()
    state = initial(
        "Analyze VPN with the relevant runbook and prepare a reviewed private work note",
        write=True,
    )
    config = {"configurable": {"thread_id": state["thread_id"]}}
    interrupted = await graph.ainvoke(state, config=config)
    assert interrupted["control_owner"] == "human"
    assert "handoff:supervisor->action" in interrupted["trajectory"]
    assert action.calls == 1
    handoff_events = [
        payload for event, payload in FakeRepository.events if event == "control.handoff"
    ]
    assert len(handoff_events) == 1
    assert handoff_events[0]["from"] == "supervisor"
    assert handoff_events[0]["to"] == "action"
    assert handoff_events[0]["allowed_operations"] == ["append_ticket_followup"]
    assert len(handoff_events[0]["review_digest"]) == 64
    assert len(handoff_events[0]["evidence_digest"]) == 64
    completed = await graph.ainvoke(
        Command(
            resume={
                "decision": "approved",
                "decided_by": "approver-1",
                "comment": "approved",
            }
        ),
        config=config,
    )
    assert executor.calls == 1
    assert completed["final_result"]["execution"]["verified"] is True
    assert completed["final_result"]["control_owner"] == "none"


@pytest.mark.asyncio
async def test_a_voided_action_is_re_derived_rather_than_decided() -> None:
    """What the graph does with an approval that arrived for an action it withdrew.

    ``runtime.resume_run`` sends ``{"voided": True}`` when the requester's scope moved
    while the run sat at the interrupt. The node has to route that somewhere, and the
    three candidates are all wrong in their own way: executing it spends a decision on
    evidence that is gone, finalising it turns somebody else's revocation into the
    requester's failure, and recording it as an approval is what the void exists to
    prevent. It goes back to the supervisor, whose job is to re-derive the action from
    re-retrieved evidence and ask again.
    """
    supervisor, action, executor = StateDrivenSupervisor(), CountingAction(), FakeExecutor()
    graph = build_supervisor_graph(
        services(supervisor=supervisor, action=action, executor=executor)
    )
    graph.checkpointer = InMemorySaver()
    state = initial(
        "Analyze VPN with the relevant runbook and prepare a reviewed private work note",
        write=True,
    )
    config = {"configurable": {"thread_id": state["thread_id"]}}
    interrupted = await graph.ainvoke(state, config=config)
    assert interrupted["control_owner"] == "human"
    assert action.calls == 1

    voided = await graph.ainvoke(Command(resume={"voided": True}), config=config)

    assert executor.calls == 0, "a voided action is never executed"
    assert "approval:voided" in voided["trajectory"]
    assert "execute" not in voided["trajectory"], "the harness never took control"
    trail = voided["trajectory"]
    assert trail[trail.index("approval:voided") + 1] == "supervisor", (
        "the void hands the run back to the supervisor, not to the approval's own branch"
    )
    assert not any(
        payload.get("decision") == "approved"
        for event, payload in FakeRepository.events
        if event.startswith("approval.")
    ), "the human never ruled on the withdrawn action"


@pytest.mark.asyncio
async def test_a_grant_revoked_after_approval_stops_the_write() -> None:
    """The window the approval leaves open, closed at the point of use.

    A human approves an action, and the approval is bound to a hash so the action
    cannot change -- but the *authority behind it* can. Between the approval and the
    write, the requester may be dropped from a group, and everything the approval was
    granted on the strength of was gathered while they still held it. Re-checking only
    at the resume boundary would miss exactly this, so the check is repeated here,
    without a cache, and it refuses rather than narrows.
    """
    from servicemind.security.entitlements import (
        AuthorityWithdrawn,
        EntitlementOutcome,
        EntitlementResult,
    )

    class _Revoked:
        """Same subject, same roles, one entity grant taken away since the approval."""

        async def verify(self, tenant_id, user_id, *, fresh=False):
            return EntitlementResult(
                EntitlementOutcome.VERIFIED,
                roles=frozenset({"viewer", "analyst"}),
                entity_ids=frozenset(),
                group_ids=frozenset(),
            )

    configure_entitlement_verifier(_Revoked())
    supervisor, action, executor = StateDrivenSupervisor(), CountingAction(), FakeExecutor()
    graph = build_supervisor_graph(
        services(supervisor=supervisor, action=action, executor=executor)
    )
    graph.checkpointer = InMemorySaver()
    state = initial(
        "Analyze VPN with the relevant runbook and prepare a reviewed private work note",
        write=True,
    )
    config = {"configurable": {"thread_id": state["thread_id"]}}
    await graph.ainvoke(state, config=config)

    with pytest.raises(AuthorityWithdrawn) as raised:
        await graph.ainvoke(
            Command(resume={"decision": "approved", "decided_by": "approver-1", "comment": "ok"}),
            config=config,
        )

    assert executor.calls == 0, "the write must not be attempted"
    assert raised.value.revoked == {"roles": [], "entity_ids": [1], "group_ids": []}
    audit = [
        payload for event, payload in FakeRepository.events if event == "action.authority_withdrawn"
    ]
    assert len(audit) == 1
    assert audit[0]["reason"] == "revoked"


@pytest.mark.asyncio
async def test_a_verified_requester_without_the_step_role_is_refused() -> None:
    """The other half of the pre-write gate, and the one a revocation check cannot see.

    Nothing was taken away here: the subject is enabled, the entity grant is intact, and
    every recorded role is still held. What is missing is the role the step itself
    requires -- and the reason this has to be stated separately is that a check built
    only around "what changed since the approval" finds no change at all.
    """

    from servicemind.security.entitlements import (
        AuthorityWithdrawn,
        EntitlementOutcome,
        EntitlementResult,
    )

    class _NoStepRole:
        async def verify(self, tenant_id, user_id, *, fresh=False):
            return EntitlementResult(
                EntitlementOutcome.VERIFIED,
                roles=frozenset({"viewer"}),
                entity_ids=frozenset({1}),
                group_ids=frozenset(),
            )

    configure_entitlement_verifier(_NoStepRole())
    supervisor, action, executor = StateDrivenSupervisor(), CountingAction(), FakeExecutor()
    graph = build_supervisor_graph(
        services(supervisor=supervisor, action=action, executor=executor)
    )
    graph.checkpointer = InMemorySaver()
    state = initial(
        "Analyze VPN with the relevant runbook and prepare a reviewed private work note",
        write=True,
    )
    state["roles"] = ["viewer"]
    config = {"configurable": {"thread_id": state["thread_id"]}}
    await graph.ainvoke(state, config=config)

    with pytest.raises(AuthorityWithdrawn) as raised:
        await graph.ainvoke(
            Command(resume={"decision": "approved", "decided_by": "approver-1", "comment": "ok"}),
            config=config,
        )

    assert executor.calls == 0, "the write must not be attempted"
    assert raised.value.reason == "insufficient"
    assert raised.value.missing == {"roles": ["analyst"]}
    audit = [
        payload for event, payload in FakeRepository.events if event == "action.authority_withdrawn"
    ]
    assert audit[0]["missing"] == {"roles": ["analyst"]}


# ---------------------------------------------------------------------------
# Orchestration audit regressions (2026-09-08): pre-review REPLAN crash, plan/
# revision double-failure terminal handling, and ESCALATE human-override loop.
# ---------------------------------------------------------------------------


def minimal_plan() -> TaskPlan:
    due = datetime.now(UTC) + timedelta(minutes=5)
    budget = Budget(deadline=due)
    tasks = [
        Task(
            task_id="T1",
            agent=AgentName.DATA,
            task_type="get_ticket",
            input={"objective": "Read ticket", "ticket_id": 2},
            error_policy=ErrorPolicy.RETRY,
            deadline=due,
        )
    ]
    return TaskPlan(
        goal="Analyze VPN incident",
        tasks=tasks,
        max_parallel=2,
        max_steps=budget.max_steps,
        max_replans=budget.max_replans,
        deadline=due,
        budget=budget,
    )


def test_policy_escalate_allows_finalize_but_human_continue_is_terminal() -> None:
    """A human "continue" on an escalation must not re-enter the interrupt loop."""
    plan = minimal_plan()
    review = ReviewResult(
        decision=ReviewDecision.ESCALATE,
        risk_level=RiskLevel.HIGH,
        feedback="Reviewer could not resolve the blocking finding.",
    )
    state = initial("Analyze VPN incident")
    state["task_plan"] = plan.model_dump(mode="json", by_alias=True)
    state["review_result"] = review.model_dump(mode="json")
    policy = SupervisorPolicy()
    assert policy.legal_actions(state) == {
        SupervisorAction.ESCALATE,
        SupervisorAction.FINALIZE,
    }
    state["human_review"] = {"decision": "continue"}
    assert policy.legal_actions(state) == {SupervisorAction.FINALIZE}


@pytest.mark.asyncio
async def test_supervisor_replan_before_any_review_does_not_crash() -> None:
    """A pre-review REPLAN (legal when evidence is still un-joined) previously hit
    ``_review(state)`` on an empty payload and crashed the graph with a raw
    validation error. The revision node now tolerates a missing review and the run
    must complete normally through to a passed review."""
    supervisor = StateDrivenSupervisor(override={2: SupervisorAction.REPLAN})
    graph = build_supervisor_graph(services(supervisor=supervisor))
    result = await graph.ainvoke(initial("Analyze VPN with the relevant runbook"))
    assert result["final_result"]["review"]["decision"] == "passed"
    assert result["plan_revision"] >= 1
    decisions = [
        item.removeprefix("decision:")
        for item in result["trajectory"]
        if item.startswith("decision:")
    ]
    assert decisions[0] == "plan"
    assert decisions[1] == "replan"


class BrokenReplanner(FakePlanner):
    async def revise_plan(self, **kwargs):
        raise ValueError("replanner model returned an invalid revision")


class RetrieveMoreSupervisor(StateDrivenSupervisor):
    """Picks RETRIEVE_MORE the first time an open review makes it legal."""

    def __init__(self) -> None:
        super().__init__()
        self.took_retrieve_more = False
        self.asked_after_termination = 0

    async def decide(self, state_view, *, policy_feedback=None):
        # The Supervisor is the control plane, not a bystander. Asking it to decide a
        # run that has already ended costs a model call and writes a rationale for a
        # state it is not being shown correctly.
        if state_view.get("termination_code"):
            self.asked_after_termination += 1
        if (
            not self.took_retrieve_more
            and SupervisorAction.RETRIEVE_MORE.value in state_view["legal_actions"]
        ):
            self.calls += 1
            self.feedback.append(policy_feedback)
            self.took_retrieve_more = True
            return SupervisorDecision(
                action=SupervisorAction.RETRIEVE_MORE,
                selected_task_ids=[],
                rationale_summary="The review named a repair.",
                confidence=1,
            )
        return await super().decide(state_view, policy_feedback=policy_feedback)


class ReviewerNamingARepair(FakeReviewer):
    """The live ACC-03 verdict: one defect, and it is a citation, not a missing document.

    Both records the Reviewer compares are already in the joined evidence -- it says so
    itself ("the ticket-25 counterpart ev-ff24bc453152bf9d, not cited"). No retrieval
    can change this analysis, which is why the run needed a planner that could still
    fail while leaving the answer intact.
    """

    async def review(self, *, analysis, evidence, **kwargs):
        return ReviewResult(
            decision=ReviewDecision.RETRIEVE_MORE,
            risk_level=RiskLevel.LOW,
            feedback="C12 cites the wrong record; the right one is already in evidence.",
            reviewed_evidence_refs=evidence.evidence_refs,
        )


class ReplannerThatNeedsTwoCorrections(FakePlanner):
    """Breaks one shape rule per attempt, and fixes the rule it was last told about.

    ``compile_proposal`` raises on the first rule it finds, so this is what a proposal
    looks like from the node's side whenever the model clears the rule it was corrected
    on and then breaks a different one.
    """

    def __init__(self) -> None:
        super().__init__()
        self.corrections: list[str | None] = []

    async def revise_plan(self, **kwargs):
        self.corrections.append(kwargs.get("correction"))
        if len(self.corrections) < 3:
            raise ValueError(f"shape rule {len(self.corrections)} not met")
        return kwargs["previous"]


@pytest.mark.asyncio
async def test_a_second_shape_rejection_still_reaches_the_replanner() -> None:
    """Live regression, 2026-09-23, ACC-03, run 16a6ba03 -- and the same shape as the
    Supervisor's ``policy_attempts`` comment: at two attempts the allowance was one
    informed correction, so a proposal that fixed the rule it was told about and broke a
    different one ended the run. The third call has to happen and has to carry the second
    rule's message; the run's model-call budget is the real bound, not this number.
    """
    supervisor = RetrieveMoreSupervisor()
    planner = ReplannerThatNeedsTwoCorrections()
    graph = build_supervisor_graph(
        services(
            supervisor=supervisor,
            planner=planner,
            reviewer=ReviewerNamingARepair(),
        )
    )
    result = await graph.ainvoke(initial("Analyze VPN incident"))

    assert planner.corrections[0] is None, "the first proposal is not a correction"
    assert "shape rule 1 not met" in planner.corrections[1]
    assert "shape rule 2 not met" in planner.corrections[2], (
        "the second rejection's cause must reach the model, not just the event log"
    )
    assert result["plan_revision"] >= 1
    assert result["final_result"]["termination_code"] is None


@pytest.mark.asyncio
async def test_a_revision_the_planner_cannot_produce_keeps_what_the_run_answered() -> None:
    """Live regression, 2026-09-23, ACC-03, run 16a6ba03.

    The Analyst answered correctly; the Reviewer found exactly one defect and asked for
    a repair; the replanner's proposals were rejected on shape rules. This branch then
    returned ``analysis_result: {}`` and ``review_result: {}`` alongside
    ``critical_error``, so the run's only record of what it had answered -- and of the
    one thing left open -- was erased at the exact moment an operator would need it.
    ``run.failed`` carried neither, and the analysis had to be re-derived from the
    database to find out what the run had said.
    """
    supervisor = RetrieveMoreSupervisor()
    graph = build_supervisor_graph(
        services(
            supervisor=supervisor,
            planner=BrokenReplanner(),
            reviewer=ReviewerNamingARepair(),
        )
    )
    result = await graph.ainvoke(initial("Analyze VPN incident"))
    final = result["final_result"]

    assert final["termination_code"] == "critical_error"
    assert final["analysis"], "the analysis the Reviewer was judging must survive the failure"
    assert final["analysis"]["classification"] == "network/vpn"
    assert final["review"]["decision"] == "retrieve_more"
    assert final["review"]["feedback"] == (
        "C12 cites the wrong record; the right one is already in evidence."
    )
    assert [event for event, _ in FakeRepository.events].count("run.failed") == 1
    # ``retrieve_more`` is ``replan`` with the flag set and ends a run the same way, so
    # it routes on the same condition. As a static edge it woke the control plane after
    # the run had already terminated: on ACC-03 that produced a confident "no dirty
    # evidence, failures, or open review feedback. The only legal action is finalize"
    # for a run whose review had just asked for a repair.
    assert supervisor.asked_after_termination == 0


@pytest.mark.asyncio
async def test_replan_double_failure_finalizes_without_raw_runtime_error() -> None:
    """A replanner that fails validation twice must end the run through the normal
    finalizer (persisted, run.failed) instead of escaping the node and erroring
    the whole graph.

    The finalizer must also run *once*. ``replan`` carries a static edge to
    ``supervisor``, and returning ``Command(goto="finalize")`` from such a node follows
    both routes: finalize wrote ``run.failed`` and persisted the result, the Supervisor
    was asked to decide again with ``FINALIZE`` as its only legal action, and finalize
    wrote a second ``run.failed`` for the same run -- two terminal records for one
    failure, and a control-plane model call spent choosing an action already chosen.
    """
    supervisor = StateDrivenSupervisor(override={2: SupervisorAction.REPLAN})
    graph = build_supervisor_graph(services(supervisor=supervisor, planner=BrokenReplanner()))
    result = await graph.ainvoke(initial("Analyze VPN incident"))
    assert result["final_result"]["termination_code"] == "critical_error"
    event_types = [event for event, _ in FakeRepository.events]
    assert event_types.count("run.failed") == 1
    assert event_types[-1] == "run.failed"


class BrokenSupervisor:
    """A control plane whose model will not answer the decision schema."""

    def __init__(self) -> None:
        self.calls = 0

    async def decide(self, state_view, *, policy_feedback=None):
        del state_view, policy_feedback
        self.calls += 1
        # The shape the live escape takes: the governed gateway spent its schema-repair
        # attempt and re-raised the violation it could not talk the model out of.
        return SupervisorDecision.model_validate(
            {"confidence": 2, "rationale_summary": "over the documented maximum"}
        )


class UnassemblableContext:
    """A governance layer whose governed input cannot be built for one agent.

    The live shape: the Reviewer's context item *is* the ``AnalysisResult`` the model
    just produced, so a verbose analysis can exceed the run's context budget. The
    governance layer reports that as a typed refusal rather than a pydantic error.
    """

    def __init__(self, agent: ContextAgent) -> None:
        self.agent = agent

    async def build_context(self, *, state, task, invocation, agent):
        del state, task, invocation
        if agent is self.agent:
            raise ContextAssemblyError(
                "required_item_exceeds_token_budget",
                f"required context item exceeds token budget: state for {agent.value}",
            )
        return None

    async def post_run(self, *, state, result, status) -> int:
        del state, result, status
        return 0


@pytest.mark.asyncio
async def test_a_context_that_cannot_be_assembled_finalizes_instead_of_crashing_the_graph() -> None:
    """A run whose governed input does not fit ends through ``finalize``, not an exception.

    ``build_context`` raises when a required item exceeds the context budget. Every call
    site in the workflow let that escape the node, so the graph errored: no finalize, no
    trajectory, no review, and ``_process_webhook_run`` recorded FAILED with a bare
    ``MODEL_VALUEERROR`` -- the same ledger entry a genuine bug in this file would get.
    A run that is too big for its own context budget is a property of the run, and it
    has to be readable as one.
    """
    graph = build_supervisor_graph(
        services(phase5=UnassemblableContext(ContextAgent.REVIEWER))  # type: ignore[arg-type]
    )
    result = await graph.ainvoke(initial("Analyze VPN incident"))

    assert result["final_result"]["termination_code"] == "context_assembly_failure"
    event_types = [event for event, _ in FakeRepository.events]
    assert "context.assembly_failed" in event_types
    # Exactly one terminal write. A Command(goto="finalize") from a node that also
    # carries a static edge follows both, so finalize ran twice and one failed run
    # left two run.failed events on the timeline.
    assert event_types.count("run.failed") == 1
    assert event_types[-1] == "run.failed"
    failure = next(
        payload for event, payload in FakeRepository.events if event == "context.assembly_failed"
    )
    assert failure["code"] == "required_item_exceeds_token_budget"
    assert failure["agent"] == "reviewer"


@pytest.mark.asyncio
async def test_an_agent_that_builds_no_context_still_runs_ungoverned() -> None:
    """``None`` from ``build_context`` means "no envelope", not "refuse to run".

    ``SERVICEMIND_CONTEXT_ENABLED`` defaults to off and the governance layer returns
    ``None`` for every agent; that path must stay reachable now that the call sites
    also handle a refusal.
    """
    graph = build_supervisor_graph(
        services(phase5=UnassemblableContext(ContextAgent.KNOWLEDGE))  # type: ignore[arg-type]
    )
    result = await graph.ainvoke(initial("Analyze VPN incident"))

    assert result["final_result"]["review"]["decision"] == "passed"
    assert "context.assembly_failed" not in [event for event, _ in FakeRepository.events]


def test_an_ordinary_data_task_does_not_overflow_its_output_ref() -> None:
    """66 evidence ids is a normal data task, and it must leave a plan the graph can re-read.

    A data task returns a ticket, up to 50 support groups and up to 15 followups
    (``agents/data.py``), so any tenant with 25 support groups exceeds the 500-character
    ``Task.output_ref`` on every run. ``Task`` does not validate assignments, so the join
    wrote 519 characters without complaint; the run then died one node later inside
    ``TaskPlan.model_validate``, after the evidence had already been gathered -- and the
    error named a field nobody had written to in that node.
    """
    evidence_ids = [f"ev-{index:016x}" for index in range(66)]
    plan = minimal_plan()
    plan.tasks[0].status = TaskStatus.RUNNING

    completed = TaskDispatcher().transition(
        plan, "T1", TaskStatus.SUCCESS, output_ref=_bounded_output_ref(evidence_ids)
    )
    reference = completed.tasks[0].output_ref

    assert len(reference) <= TASK_OUTPUT_REF_MAX_LENGTH
    kept, _, dropped = reference.partition(" …[")
    assert dropped.endswith(" more]"), "the clip must say how much it dropped"
    # Every id is either in the reference or counted by the elision marker; the ledger
    # must account for all 66 rather than quietly lose some.
    assert len(kept.split(",")) + int(dropped.removesuffix(" more]")) == len(evidence_ids)
    assert set(kept.split(",")) <= set(evidence_ids), "the clip must not cut an id in half"
    # The real assertion: what the next supervisor node does with the plan it was handed.
    assert TaskPlan.model_validate(completed.model_dump(mode="json", by_alias=True)).goal == (
        completed.goal
    )


def test_a_task_without_evidence_ids_records_the_same_placeholder_as_before() -> None:
    assert _bounded_output_ref([]) == "none"
    assert _bounded_output_ref(["ev-1", "ev-2"]) == "ev-1,ev-2"


@pytest.mark.asyncio
async def test_a_decision_the_schema_rejects_finalizes_instead_of_crashing_the_graph() -> None:
    """``decide`` was the one model call whose failure took the graph down with it.

    It sits outside ``supervisor_node``'s only ``try``, which catches
    ``SupervisorPolicyError`` alone, so a ``ValidationError`` from it escaped the node.
    The graph raised and ``_process_webhook_run`` swallowed that into
    ``update_run(FAILED, error="ValidationError")``: no termination code, no finalize
    record, no review, no timeline entry -- an operator saw a run that simply stopped.
    The planner already finalizes with ``critical_error`` on the same class of failure;
    the control plane must reach the same terminal, because a graph that raises cannot
    be recovered, retried or audited.
    """
    supervisor = BrokenSupervisor()
    graph = build_supervisor_graph(services(supervisor=supervisor))
    result = await graph.ainvoke(initial("Analyze VPN incident"))

    assert result["final_result"]["termination_code"] == "supervisor_decision_failure"
    assert supervisor.calls == 1, "the gateway already repaired once; a third ask cannot help"
    event_types = [event for event, _ in FakeRepository.events]
    assert "supervisor.decision_rejected" in event_types
    assert event_types[-1] == "run.failed"
    rejection = next(
        payload
        for event, payload in FakeRepository.events
        if event == "supervisor.decision_rejected"
    )
    assert rejection["error_type"] == "ValidationError"
    assert rejection["error_code"] == "MODEL_SCHEMA_INVALID"
    assert rejection["attempt"] == 1


def write_plan(*, action_done: bool = False) -> TaskPlan:
    due = datetime.now(UTC) + timedelta(minutes=5)
    budget = Budget(deadline=due)

    def task(
        task_id: str, agent: AgentName, task_type: str, depends: list[str], done: bool
    ) -> Task:
        return Task(
            task_id=task_id,
            agent=agent,
            task_type=task_type,
            input={"objective": f"Run {task_type}", "ticket_id": 2},
            depends_on=depends,
            status=TaskStatus.SUCCESS if done else TaskStatus.PENDING,
            deadline=due,
        )

    tasks = [
        task("T1", AgentName.DATA, "get_ticket", [], done=True),
        task("T2", AgentName.ANALYSIS, "analyze_ticket", ["T1"], done=True),
        task("T3", AgentName.REVIEWER, "review_analysis", ["T2"], done=True),
        task("T4", AgentName.ACTION, "propose_followup", ["T3"], done=action_done),
    ]
    return TaskPlan(
        goal="Analyze and prepare a reviewed work note",
        tasks=tasks,
        max_parallel=2,
        max_steps=budget.max_steps,
        max_replans=budget.max_replans,
        deadline=due,
        budget=budget,
    )


def test_policy_handoff_requires_a_ready_action_task() -> None:
    """A passed write review may only offer HANDOFF_ACTION once an Action task is
    actually ready; otherwise the graph would raise inside handoff_node."""
    review = ReviewResult(
        decision=ReviewDecision.PASSED,
        risk_level=RiskLevel.LOW,
        feedback="Reviewed and passed.",
    )
    state = initial("Analyze and prepare a reviewed private work note", write=True)
    policy = SupervisorPolicy()

    state["task_plan"] = write_plan(action_done=False).model_dump(mode="json", by_alias=True)
    state["review_result"] = review.model_dump(mode="json")
    assert SupervisorAction.HANDOFF_ACTION in policy.legal_actions(state)

    state["task_plan"] = write_plan(action_done=True).model_dump(mode="json", by_alias=True)
    legal = policy.legal_actions(state)
    assert SupervisorAction.HANDOFF_ACTION not in legal
    assert legal == {SupervisorAction.REPLAN}
