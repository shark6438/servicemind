"""Slice 5: deterministic citation gate + knowledge DAG ACL forwarding + retrieve loop.

Three groups:

A) ``ReviewerAgent`` deterministic citation gate. Every KNOWLEDGE evidence item must
   carry a self-consistent citation anchored to the evidence row (the shape the RAG
   service emits); anything else fails closed to ESCALATE (re-retrieval would
   regenerate the same citation, so RETRIEVE_MORE cannot repair an integrity break).
   Code-curated fallback runbooks marked ``degraded_rag`` are exempt.

B) Supervisor regression with the REAL reviewer: round 1 returns no knowledge and the
   reviewer decides RETRIEVE_MORE; the supervisor bumps ``retrieval_round`` and
   dispatches a fresh knowledge task; round 2 retrieval returns cited knowledge and
   the same reviewer PASSES.

C) ``knowledge_task_node`` forwards the full retrieval identity (user, GLPI entity,
   and the optional group/profile ACL) to a real ``KnowledgeAgent`` subclass.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from servicemind.agents.knowledge import KnowledgeAgent
from servicemind.agents.reviewer import ReviewerAgent
from servicemind.domain.analysis import AnalysisResult
from servicemind.domain.evidence import Evidence, EvidenceSourceType, join_evidence
from servicemind.domain.knowledge import Citation
from servicemind.domain.review import ReviewDecision, ReviewResult, RiskLevel
from servicemind.domain.supervisor import SupervisorAction, SupervisorDecision
from servicemind.domain.task import AgentName, Budget, ErrorPolicy, Task, TaskPlan
from servicemind.orchestration.dispatcher import TaskDispatcher
from servicemind.orchestration.router import FastPathRouter
from servicemind.orchestration.supervisor_policy import SupervisorPolicy
from servicemind.orchestration.supervisor_workflow import (
    SupervisorRuntimeServices,
    build_supervisor_graph,
)

TENANT = UUID("11111111-1111-4111-8111-111111111111")


# --------------------------------------------------------------------------- fixtures


def digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def citation_for(content: str, *, parent: UUID | None = None) -> Citation:
    parent_chunk_id = parent or uuid4()
    document_id = uuid4()
    content_hash = digest(content)
    return Citation(
        citation_id="cite-"
        + hashlib.sha256(
            json.dumps(
                [str(document_id), str(parent_chunk_id), content_hash],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:16],
        document_id=document_id,
        parent_chunk_id=parent_chunk_id,
        source="test-source",
        source_uri=f"knowledge://test/{content_hash[:16]}",
        source_record_id=f"test-source://{parent_chunk_id}",
        source_version="v1",
        content_hash=content_hash,
        title="Fixture runbook",
    )


def knowledge_evidence(
    content: str,
    *,
    citation: Citation | None = None,
    extra_metadata: dict | None = None,
) -> Evidence:
    """KNOWLEDGE evidence mirroring ``EnterpriseRAG.to_evidence`` binding."""
    citation = citation or citation_for(content)
    return Evidence.create(
        tenant_id=TENANT,
        source_type=EvidenceSourceType.KNOWLEDGE,
        source_ref=citation.source_uri,
        resource_type="knowledge_parent_chunk",
        resource_id=str(citation.parent_chunk_id),
        content=content,
        provider=citation.source,
        retrieval_method="dense_bm25_rrf_cross_encoder_parent",
        metadata={
            **(extra_metadata or {}),
            "citation": citation.model_dump(mode="json"),
        },
    )


def glpi_ticket() -> Evidence:
    return Evidence.create(
        tenant_id=TENANT,
        source_type=EvidenceSourceType.GLPI,
        source_ref="glpi://tickets/2",
        resource_type="ticket",
        resource_id="2",
        content="VPN MFA outage with broad impact",
        provider="glpi",
        retrieval_method="api",
    )


def glpi_group() -> Evidence:
    return Evidence.create(
        tenant_id=TENANT,
        source_type=EvidenceSourceType.GLPI,
        source_ref="glpi://groups/5",
        resource_type="support_group",
        resource_id="5",
        content="GLPI support group: Network Team",
        provider="glpi",
        retrieval_method="api",
    )


def analysis(refs: list[str], **updates) -> AnalysisResult:
    values = {
        "classification": "network/vpn",
        "impact": 3,
        "urgency": 4,
        "priority": 4,
        "recommended_group": "Network Team",
        "recurring_incident": False,
        "problem_recommendation": "Collect recurrence evidence.",
        "change_recommendation": "No change supported.",
        "proposed_actions": [],
        "reasoning_summary": "Ticket and runbook support Network Team.",
        "evidence_refs": refs,
        "confidence": 0.8,
        "source": "test",
    }
    values.update(updates)
    return AnalysisResult.model_validate(values)


def _task(
    task_id: str,
    agent: AgentName,
    task_type: str,
    depends_on: list[str],
    objective: str,
) -> Task:
    due = datetime.now(UTC) + timedelta(minutes=5)
    return Task(
        task_id=task_id,
        agent=agent,
        task_type=task_type,
        input={"objective": objective, "ticket_id": 2},
        depends_on=depends_on,
        error_policy=ErrorPolicy.RETRY,
        deadline=due,
    )


def _closed_plan(goal: str, tasks: list[Task]) -> TaskPlan:
    due = datetime.now(UTC) + timedelta(minutes=5)
    # Two dispatch rounds plus analysis/review run twice; the loop needs more than
    # the small default model budget, so give the DAG headroom to reach round-2 review.
    budget = Budget(
        max_steps=30,
        max_replans=3,
        max_model_calls=50,
        max_tool_calls=50,
        deadline=due,
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


# =============================================================== A) reviewer gate


@pytest.mark.asyncio
async def test_reviewer_passes_valid_cited_knowledge() -> None:
    reviewer = ReviewerAgent()
    joined = join_evidence(
        TENANT, [glpi_ticket(), glpi_group(), knowledge_evidence("Network Team owns VPN faults")]
    )
    result = await reviewer.review(
        analysis=analysis(joined.evidence_refs),
        evidence=joined,
        request_write=False,
        retrieval_round=1,
        replan_count=0,
        max_replans=2,
    )
    assert result.decision is ReviewDecision.PASSED
    assert result.findings == []


@pytest.mark.asyncio
async def test_reviewer_escalates_knowledge_without_citation() -> None:
    reviewer = ReviewerAgent()
    runbook = Evidence.create(
        tenant_id=TENANT,
        source_type=EvidenceSourceType.KNOWLEDGE,
        source_ref="knowledge://orphan/runbook",
        resource_type="runbook",
        resource_id="orphan",
        content="Network Team owns VPN faults",
        provider="unknown-path",
        retrieval_method="hand_assembled",
    )
    joined = join_evidence(TENANT, [glpi_ticket(), glpi_group(), runbook])
    result = await reviewer.review(
        analysis=analysis(joined.evidence_refs),
        evidence=joined,
        request_write=False,
        retrieval_round=1,
        replan_count=0,
        max_replans=2,
    )
    assert result.decision is ReviewDecision.ESCALATE
    assert result.findings[0].category == "citation"
    assert result.findings[0].reason_code == "MISSING_KNOWLEDGE_CITATION"
    assert result.findings[0].evidence_refs == [runbook.evidence_id]


@pytest.mark.asyncio
async def test_reviewer_escalates_citation_not_anchored_to_evidence() -> None:
    reviewer = ReviewerAgent()
    citation = citation_for("Network Team owns VPN faults")
    anchored = knowledge_evidence("Network Team owns VPN faults", citation=citation)
    # Resource id points at a *different* parent than the citation claims.
    runbook = anchored.model_copy(update={"resource_id": str(uuid4())})
    joined = join_evidence(TENANT, [glpi_ticket(), glpi_group(), runbook])
    result = await reviewer.review(
        analysis=analysis(joined.evidence_refs),
        evidence=joined,
        request_write=False,
        retrieval_round=1,
        replan_count=0,
        max_replans=2,
    )
    assert result.decision is ReviewDecision.ESCALATE
    assert result.findings[0].reason_code == "CITATION_EVIDENCE_MISMATCH"


@pytest.mark.asyncio
async def test_reviewer_escalates_when_citation_id_no_longer_matches_fields() -> None:
    reviewer = ReviewerAgent()
    citation = citation_for("Network Team owns VPN faults").model_copy(
        update={"document_id": uuid4()}
    )
    runbook = knowledge_evidence("Network Team owns VPN faults", citation=citation)
    joined = join_evidence(TENANT, [glpi_ticket(), glpi_group(), runbook])
    result = await reviewer.review(
        analysis=analysis(joined.evidence_refs),
        evidence=joined,
        request_write=False,
        retrieval_round=1,
        replan_count=0,
        max_replans=2,
    )
    assert result.decision is ReviewDecision.ESCALATE
    assert result.findings[0].reason_code == "CITATION_ID_MISMATCH"


@pytest.mark.asyncio
async def test_reviewer_allows_degraded_baseline_knowledge_without_citation() -> None:
    reviewer = ReviewerAgent()
    # KnowledgeAgent fallback (RAG disabled) marks its curated runbooks degraded_rag.
    runbook = knowledge_evidence(
        "Network Team owns VPN faults",
        extra_metadata={"degraded_rag": True},
    ).model_copy(update={"metadata": {"degraded_rag": True}})
    joined = join_evidence(TENANT, [glpi_ticket(), glpi_group(), runbook])
    result = await reviewer.review(
        analysis=analysis(joined.evidence_refs),
        evidence=joined,
        request_write=False,
        retrieval_round=1,
        replan_count=0,
        max_replans=2,
    )
    assert result.decision is ReviewDecision.PASSED


# =========================================================== B) retrieve loop


class FakeRepository:
    events: list[tuple[str, dict]] = []

    def __init__(self, tenant_id: UUID) -> None:
        assert tenant_id == TENANT

    async def update_run(self, run_id, status, *, result=None, error=None):
        return SimpleNamespace(id=run_id, status=status.value, result=result, error=error)

    async def append_event(self, run_id, event_type, payload):
        FakeRepository.events.append((event_type, payload))
        return SimpleNamespace()

    async def save_action_intent(self, **kwargs):
        return SimpleNamespace(id=uuid4(), **kwargs)


class FakeData:
    async def get_ticket_evidence(self, context, ticket_id):
        return [glpi_ticket(), glpi_group()]


class RoundAwareKnowledge:
    """Round 0 returns nothing (forces RETRIEVE_MORE); round >=1 returns cited KB."""

    def __init__(self) -> None:
        self.rounds: list[int] = []

    async def retrieve(self, *, tenant_id, query, retrieval_round=0):
        self.rounds.append(retrieval_round)
        if retrieval_round >= 1:
            return [
                knowledge_evidence(
                    "Network Team owns VPN gateway faults and owns the runbook."
                )
            ]
        return []


class LoopAnalysis:
    async def analyze_evidence(self, joined, goal, *, request_write, ticket_id):
        return analysis(joined.evidence_refs)


class LoopPlanner:
    """create_plan yields one evidence DAG; revise_plan re-adds a knowledge task.

    Mirrors DynamicPlanner revision semantics deterministically: RETRIEVE_MORE must
    add a fresh knowledge task plus new Analysis/Reviewer tasks, so the supervisor
    re-dispatches knowledge on round two.
    """

    def __init__(self) -> None:
        self.revised = False

    async def create_plan(self, *, goal, ticket_id, request_write, correction=None):
        tasks = [
            _task("T1", AgentName.DATA, "get_ticket", [], "Read the ticket"),
            _task("T2", AgentName.KNOWLEDGE, "retrieve_knowledge", [], "Read the runbook"),
            _task("T3", AgentName.ANALYSIS, "analyze_ticket", ["T1", "T2"], "Analyze"),
            _task("T4", AgentName.REVIEWER, "review_analysis", ["T3"], "Review"),
        ]
        return _closed_plan(goal, tasks)

    async def revise_plan(self, *, previous, review, ticket_id, request_write, correction=None):
        self.revised = True
        tasks = [
            _task("T5", AgentName.KNOWLEDGE, "retrieve_knowledge", [], "Read more runbooks"),
            _task("T6", AgentName.ANALYSIS, "analyze_ticket", ["T5"], "Re-analyze"),
            _task("T7", AgentName.REVIEWER, "review_analysis", ["T6"], "Re-review"),
        ]
        return _closed_plan(previous.goal, tasks)


class StateDrivenSupervisor:
    def __init__(self) -> None:
        self.calls = 0

    async def decide(self, state_view, *, policy_feedback=None):
        self.calls += 1
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
        raise AssertionError(f"no legal action in {legal}")


def initial(goal: str, **updates) -> dict:
    state = {
        "run_id": str(uuid4()),
        "tenant_id": str(TENANT),
        "user_id": "user-1",
        "username": "analyst",
        "roles": ["viewer", "analyst"],
        "allowed_glpi_entity_ids": [1],
        "group_ids": [7],
        "profile_ids": [12],
        "thread_id": str(uuid4()),
        "ticket_id": 2,
        "raw_request": goal,
        "goal": goal,
        "request_write": False,
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
    state.update(updates)
    return state


def loop_services(supervisor, planner, knowledge, reviewer=None):
    return SupervisorRuntimeServices(
        router=FastPathRouter(),
        supervisor=supervisor,
        planner=planner,
        policy=SupervisorPolicy(),
        dispatcher=TaskDispatcher(),
        data=FakeData(),
        knowledge=knowledge,
        analysis=LoopAnalysis(),
        reviewer=reviewer or ReviewerAgent(),
        repository_factory=FakeRepository,
    )


@pytest.mark.asyncio
async def test_real_reviewer_retrieve_more_dispatches_second_round_then_passes() -> None:
    FakeRepository.events = []
    supervisor = StateDrivenSupervisor()
    knowledge = RoundAwareKnowledge()
    planner = LoopPlanner()
    graph = build_supervisor_graph(
        loop_services(supervisor, planner, knowledge, reviewer=ReviewerAgent())
    )
    result = await graph.ainvoke(initial("Analyze VPN with the relevant runbook"))
    assert knowledge.rounds == [0, 1], "second retrieval round must have run"
    assert planner.revised is True
    assert result["final_result"]["review"]["decision"] == "passed"
    actions = [
        item.removeprefix("decision:")
        for item in result["trajectory"]
        if item.startswith("decision:")
    ]
    assert "retrieve_more" in actions
    assert actions.index("retrieve_more") < actions.index("finalize")
    # The supervisor bumped the round and a second dispatch delivered evidence.
    assert [t for t in result["trajectory"] if t.startswith("knowledge:")] == [
        "knowledge:T2",
        "knowledge:T5",
    ]


# =========================================================== C) ACL forwarding


class RecordingKnowledgeAgent(KnowledgeAgent):
    """A real KnowledgeAgent whose retrieval records the identity it was handed."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict] = []

    async def retrieve(self, **kwargs):
        self.calls.append(kwargs)
        return await super().retrieve(**kwargs)


class AclPlanner:
    async def create_plan(self, *, goal, ticket_id, request_write, correction=None):
        tasks = [
            _task("T1", AgentName.DATA, "get_ticket", [], "Read the ticket"),
            _task("T2", AgentName.KNOWLEDGE, "retrieve_knowledge", [], "Retrieve knowledge"),
            _task("T3", AgentName.ANALYSIS, "analyze_ticket", ["T1", "T2"], "Analyze"),
            _task("T4", AgentName.REVIEWER, "review_analysis", ["T3"], "Review"),
        ]
        return _closed_plan(goal, tasks)


class AlwaysPassReviewer:
    async def review(self, *, analysis, evidence, **kwargs):
        return ReviewResult(
            decision=ReviewDecision.PASSED,
            risk_level=RiskLevel.LOW,
            feedback="Fixture review.",
            reviewed_evidence_refs=evidence.evidence_refs,
        )


@pytest.mark.asyncio
async def test_knowledge_dag_node_forwards_full_acls_to_agent(monkeypatch) -> None:
    from core import settings

    monkeypatch.setattr(settings, "SERVICEMIND_RAG_ENABLED", False)
    monkeypatch.setattr(settings, "SERVICEMIND_RAG_REQUIRED", False)
    FakeRepository.events = []
    knowledge = RecordingKnowledgeAgent()
    graph = build_supervisor_graph(
        SupervisorRuntimeServices(
            router=FastPathRouter(),
            supervisor=StateDrivenSupervisor(),
            planner=AclPlanner(),
            policy=SupervisorPolicy(),
            dispatcher=TaskDispatcher(),
            data=FakeData(),
            knowledge=knowledge,  # type: ignore[arg-type]
            analysis=LoopAnalysis(),
            reviewer=AlwaysPassReviewer(),  # type: ignore[arg-type]
            repository_factory=FakeRepository,
        )
    )
    await graph.ainvoke(initial("Analyze current ticket facts"))
    assert knowledge.calls, "knowledge task must have been dispatched"
    forwarded = knowledge.calls[0]
    assert forwarded["user_id"] == "user-1"
    assert forwarded["entity_ids"] == {1}
    assert forwarded["group_ids"] == {7}
    assert forwarded["profile_ids"] == {12}
    assert forwarded["retrieval_round"] == 0
