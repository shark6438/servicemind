from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from servicemind.agents.action import ActionAgent
from servicemind.domain.analysis import AnalysisResult, ProposedAction
from servicemind.domain.evidence import Evidence, EvidenceSourceType
from servicemind.domain.models import ExecutionResult
from servicemind.domain.review import ReviewDecision, ReviewResult, RiskLevel
from servicemind.domain.supervisor import SupervisorAction, SupervisorDecision
from servicemind.domain.task import AgentName, Budget, ErrorPolicy, Task, TaskPlan
from servicemind.orchestration.dispatcher import TaskDispatcher
from servicemind.orchestration.registry import supervisor_contract
from servicemind.orchestration.router import FastPathRouter
from servicemind.orchestration.supervisor_policy import (
    SupervisorPolicy,
    SupervisorPolicyError,
)
from servicemind.orchestration.supervisor_workflow import (
    SupervisorRuntimeServices,
    build_supervisor_graph,
)

TENANT = UUID("11111111-1111-4111-8111-111111111111")


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
    def __init__(self, invalid_first: bool = False) -> None:
        self.calls = 0
        self.feedback: list[str | None] = []
        self.invalid_first = invalid_first

    async def decide(self, state_view, *, policy_feedback=None):
        self.calls += 1
        self.feedback.append(policy_feedback)
        if self.invalid_first and self.calls == 1:
            return SupervisorDecision(
                action=SupervisorAction.HANDOFF_ACTION,
                rationale_summary="Intentionally invalid first decision.",
                confidence=1,
            )
        legal = set(state_view["legal_actions"])
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
                            == (
                                "analysis"
                                if action is SupervisorAction.ANALYZE
                                else "reviewer"
                            )
                        ][:1]
                        if action
                        in {SupervisorAction.ANALYZE, SupervisorAction.REVIEW}
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
        return self.delegate.propose_from_handoff(
            handoff, analysis, ticket_id=ticket_id
        )


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


def services(supervisor=None, planner=None, action=None, executor=None):
    return SupervisorRuntimeServices(
        router=FastPathRouter(),
        supervisor=supervisor or StateDrivenSupervisor(),
        planner=planner or FakePlanner(),
        policy=SupervisorPolicy(),
        dispatcher=TaskDispatcher(),
        data=FakeData(),
        knowledge=FakeKnowledge(),
        analysis=FakeAnalysis(),
        reviewer=FakeReviewer(),
        action=action or CountingAction(),
        executor=executor or FakeExecutor(),
        repository_factory=FakeRepository,
    )


@pytest.fixture(autouse=True)
def reset_fakes():
    FakeRepository.reset()
    FakeData.calls = 0
    FakeKnowledge.calls = 0


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
    handoff_events = [payload for event, payload in FakeRepository.events if event == "control.handoff"]
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
