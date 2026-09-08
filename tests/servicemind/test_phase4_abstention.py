"""Slice: runtime abstention semantics -- reviewer consumes ``unsupported_claim_ids``.

Three groups:

A) Adjudicator classification. The semantic judge's ``unsupported_claim_ids`` now
   *drive* the outcome (v3 policy) instead of being mirrored onto the result:
      - contradictions                 -> REPLAN (re-synthesis) else ESCALATE
      - claim-level grounding deficit  -> RETRIEVE_MORE while retrieval_round == 0,
                                          terminal ABSTAIN once a round is spent
      - prompt injection               -> ESCALATE (unchanged, always)
      - clean judge                    -> PASSED
   ABSTAIN is a distinct, terminal "evidence-insufficient, no grounded answer" verdict:
   never a fabricated answer, never a human escalation for an ordinary info gap.

B) Supervisor policy. ReviewDecision.ABSTAIN must admit ONLY FINALIZE -- no replan
   loop, no escalate interrupt -- so the supervisor terminates the run.

C) Supervisor graph end-to-end. Round 0 returns no knowledge -> real reviewer says
   RETRIEVE_MORE; round 1 delivers cited knowledge but the (stubbed) semantic judge
   reports claims unsupported -> terminal ABSTAIN. Asserts the run records
   ``run.abstained``, final review decision == "abstain", and one retrieval round ran
   before abstaining (the retrieve-more opportunity is never skipped).
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

import servicemind.agents.reviewer as reviewer_module
from servicemind.agents.reviewer import ReviewerAgent, SemanticReview
from servicemind.domain.analysis import (
    AnalysisClaim,
    AnalysisResult,
    AnalysisStatus,
    ProposedAction,
)
from servicemind.domain.evidence import Evidence, EvidenceSourceType, join_evidence
from servicemind.domain.knowledge import Citation
from servicemind.domain.review import ReviewDecision, RiskLevel
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


def digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _citation(content: str) -> Citation:
    document_id, parent_chunk_id = uuid4(), uuid4()
    content_hash = digest(content)
    source_uri = f"knowledge://abstain/{content_hash[:12]}"
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
        source_uri=source_uri,
        source_record_id=f"test://{content_hash[:12]}",
        source_version="v1",
        content_hash=content_hash,
        title="Fixture runbook",
    )


def knowledge_evidence(content: str) -> Evidence:
    citation = _citation(content)
    return Evidence.create(
        tenant_id=TENANT,
        source_type=EvidenceSourceType.KNOWLEDGE,
        source_ref=citation.source_uri,
        resource_type="knowledge_parent_chunk",
        resource_id=str(citation.parent_chunk_id),
        content=content,
        provider=citation.source,
        retrieval_method="dense_bm25_rrf_cross_encoder_parent",
        metadata={"citation": citation.model_dump(mode="json")},
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


def _claims(evidence_refs: list[str]) -> list[AnalysisClaim]:
    return [
        AnalysisClaim(
            claim_id="C1",
            claim_type="root_cause_hypothesis",
            statement="The outage is caused by the VPN gateway.",
            evidence_refs=evidence_refs[:1],
            confidence=0.6,
        ),
        AnalysisClaim(
            claim_id="C2",
            claim_type="priority_reason",
            statement="Broad impact justifies urgent priority.",
            evidence_refs=evidence_refs[:1],
            confidence=0.7,
        ),
    ]


def _analysis(*, evidence_refs: list[str]) -> AnalysisResult:
    return AnalysisResult(
        classification="network/vpn",
        impact=3,
        urgency=4,
        priority=4,
        recommended_group="Network Team",
        recurring_incident=False,
        problem_recommendation="Collect recurrence evidence.",
        change_recommendation="No change supported.",
        proposed_actions=[],
        reasoning_summary="Ticket and runbook support Network Team.",
        evidence_refs=evidence_refs,
        confidence=0.8,
        source="test",
        status=AnalysisStatus.MODEL,
        claims=_claims(evidence_refs),
    )


def _joined() -> "object":
    return join_evidence(
        TENANT, [glpi_ticket(), glpi_group(), knowledge_evidence("VPN gateway runbook")]
    )


class FakeRunnable:
    def __init__(self, result) -> None:
        self.result = result

    async def ainvoke(self, messages):
        return self.result


def _semantic_reviewer(monkeypatch, semantic: SemanticReview) -> ReviewerAgent:
    reviewer = ReviewerAgent(enable_semantic_review=True, model_factory=lambda: object())
    monkeypatch.setattr(
        reviewer_module, "structured_output", lambda model, schema: FakeRunnable(semantic)
    )
    return reviewer


def _semantic(claims_supported=True, unsupported=None, **updates) -> SemanticReview:
    values = {
        "claims_supported": claims_supported,
        "action_consistent": True,
        "prompt_injection_detected": False,
        "contradictions": [],
        "unsupported_claim_ids": unsupported or [],
        "feedback": "Judge output for the fixture case.",
        "confidence": 0.9,
    }
    values.update(updates)
    return SemanticReview.model_validate(values)


# ============================================================= A) adjudicator


@pytest.mark.asyncio
async def test_unsupported_claims_retrieve_more_at_round_zero(monkeypatch) -> None:
    joined = _joined()
    semantic = _semantic(claims_supported=False, unsupported=["C1"])
    reviewer = _semantic_reviewer(monkeypatch, semantic)
    result = await reviewer.review(
        analysis=_analysis(evidence_refs=joined.evidence_refs),
        evidence=joined,
        request_write=False,
        retrieval_round=0,
        replan_count=0,
        max_replans=2,
    )
    assert result.decision is ReviewDecision.RETRIEVE_MORE
    assert result.unsupported_claims == ["C1"]
    assert result.findings[0].reason_code == "SEMANTIC_CLAIMS_UNSUPPORTED"
    assert result.findings[0].category == "grounding"


@pytest.mark.asyncio
async def test_unsupported_claims_abstain_terminal_once_round_spent(monkeypatch) -> None:
    joined = _joined()
    semantic = _semantic(claims_supported=False, unsupported=["C1"])
    reviewer = _semantic_reviewer(monkeypatch, semantic)
    result = await reviewer.review(
        analysis=_analysis(evidence_refs=joined.evidence_refs),
        evidence=joined,
        request_write=False,
        retrieval_round=1,
        replan_count=0,
        max_replans=2,
    )
    assert result.decision is ReviewDecision.ABSTAIN
    assert result.unsupported_claims == ["C1"]
    assert result.risk_level is RiskLevel.MEDIUM


@pytest.mark.asyncio
async def test_claims_supported_true_but_unsupported_ids_still_abstain(monkeypatch) -> None:
    # unsupported_claim_ids are the authoritative per-claim signal even when the
    # judge's summary boolean is inconsistent; adjudication must not drop them.
    joined = _joined()
    semantic = _semantic(claims_supported=True, unsupported=["C2"])
    reviewer = _semantic_reviewer(monkeypatch, semantic)
    result = await reviewer.review(
        analysis=_analysis(evidence_refs=joined.evidence_refs),
        evidence=joined,
        request_write=False,
        retrieval_round=1,
        replan_count=0,
        max_replans=2,
    )
    assert result.decision is ReviewDecision.ABSTAIN
    assert result.unsupported_claims == ["C2"]


@pytest.mark.asyncio
async def test_contradictions_still_replan_not_abstain(monkeypatch) -> None:
    joined = _joined()
    semantic = _semantic(
        claims_supported=False,
        contradictions=["C1 conflicts with ticket facts"],
        unsupported=[],
    )
    reviewer = _semantic_reviewer(monkeypatch, semantic)
    result = await reviewer.review(
        analysis=_analysis(evidence_refs=joined.evidence_refs),
        evidence=joined,
        request_write=False,
        retrieval_round=1,
        replan_count=0,
        max_replans=2,
    )
    assert result.decision is ReviewDecision.REPLAN
    assert result.conflicts == ["C1 conflicts with ticket facts"]


@pytest.mark.asyncio
async def test_contradictions_escalate_when_replan_exhausted(monkeypatch) -> None:
    joined = _joined()
    semantic = _semantic(claims_supported=False, contradictions=["C1 conflicts"])
    reviewer = _semantic_reviewer(monkeypatch, semantic)
    result = await reviewer.review(
        analysis=_analysis(evidence_refs=joined.evidence_refs),
        evidence=joined,
        request_write=False,
        retrieval_round=1,
        replan_count=2,
        max_replans=2,
    )
    assert result.decision is ReviewDecision.ESCALATE


@pytest.mark.asyncio
async def test_prompt_injection_always_escalates(monkeypatch) -> None:
    joined = _joined()
    semantic = _semantic(
        claims_supported=False,
        unsupported=["C1"],
        prompt_injection_detected=True,
    )
    reviewer = _semantic_reviewer(monkeypatch, semantic)
    result = await reviewer.review(
        analysis=_analysis(evidence_refs=joined.evidence_refs),
        evidence=joined,
        request_write=False,
        retrieval_round=1,
        replan_count=0,
        max_replans=2,
    )
    assert result.decision is ReviewDecision.ESCALATE


@pytest.mark.asyncio
async def test_clean_semantic_judge_passes(monkeypatch) -> None:
    joined = _joined()
    semantic = _semantic()
    reviewer = _semantic_reviewer(monkeypatch, semantic)
    result = await reviewer.review(
        analysis=_analysis(evidence_refs=joined.evidence_refs),
        evidence=joined,
        request_write=False,
        retrieval_round=1,
        replan_count=0,
        max_replans=2,
    )
    assert result.decision is ReviewDecision.PASSED
    assert result.policy_version == "servicemind-review-policy-v3"


@pytest.mark.asyncio
async def test_action_mismatch_replans_until_cap_then_escalates(monkeypatch) -> None:
    # Regression: the action-consistency branch mirrored the contradictions guard --
    # a mismatch is re-plannable while a replan is owed, and must escalate (never loop
    # or crash at the supervisor boundary) once the replan cap is reached.
    joined = _joined()
    base = _analysis(evidence_refs=joined.evidence_refs)
    analysis = base.model_copy(update={"proposed_actions": []})
    mismatch = _semantic(action_consistent=False, unsupported=[])

    reviewer = _semantic_reviewer(monkeypatch, mismatch)
    reparable = await reviewer.review(
        analysis=analysis,
        evidence=joined,
        request_write=False,
        retrieval_round=1,
        replan_count=1,
        max_replans=2,
    )
    assert reparable.decision is ReviewDecision.REPLAN

    exhausted = await reviewer.review(
        analysis=analysis,
        evidence=joined,
        request_write=False,
        retrieval_round=1,
        replan_count=2,
        max_replans=2,
    )
    assert exhausted.decision is ReviewDecision.ESCALATE


@pytest.mark.asyncio
async def test_clean_judge_with_low_confidence_escalates(monkeypatch) -> None:
    # A PASSED verdict the judge itself is not confident in must not clear an
    # analysis (and cannot authorize a controlled write); the adjudicator escalates
    # symmetrically with the deterministic gate's analysis-confidence floor. The
    # analysis carries a bounded action so the deterministic write gate cannot
    # intercept before the adjudicator.
    joined = _joined()
    analysis = _analysis(evidence_refs=joined.evidence_refs).model_copy(
        update={
            "proposed_actions": [
                ProposedAction(
                    operation="append_ticket_followup",
                    resource_type="ticket",
                    resource_id="2",
                    evidence_refs=joined.evidence_refs,
                    risk_level=RiskLevel.LOW,
                )
            ]
        }
    )
    semantic = _semantic(confidence=0.2)
    reviewer = _semantic_reviewer(monkeypatch, semantic)
    result = await reviewer.review(
        analysis=analysis,
        evidence=joined,
        request_write=True,
        retrieval_round=1,
        replan_count=0,
        max_replans=2,
    )
    assert result.decision is ReviewDecision.ESCALATE
    assert result.findings[0].reason_code == "SEMANTIC_CONFIDENCE_LOW"


@pytest.mark.asyncio
async def test_deterministic_write_gate_escalates_when_replan_exhausted() -> None:
    # Regression (gate tier): a controlled write with no bounded action proposal is
    # re-plannable while a replan is owed; at the replan cap it must escalate, not
    # REPLAN past the limit (which previously reached revise_node and crashed).
    joined = _joined()
    analysis = _analysis(evidence_refs=joined.evidence_refs)  # proposed_actions=[]
    reviewer = ReviewerAgent(enable_semantic_review=False, model_factory=lambda: object())
    result = await reviewer.review(
        analysis=analysis,
        evidence=joined,
        request_write=True,
        retrieval_round=0,
        replan_count=2,
        max_replans=2,
    )
    assert result.decision is ReviewDecision.ESCALATE
    assert result.findings[0].reason_code == "MISSING_ACTION_PROPOSAL"


# ============================================================= B) policy


def _policy_state(review_decision: ReviewDecision) -> dict:
    due = datetime.now(UTC) + timedelta(minutes=5)
    plan = TaskPlan(
        goal="goal",
        tasks=[
            Task(
                task_id="T1",
                agent=AgentName.ANALYSIS,
                task_type="analyze_ticket",
                input={"objective": "x", "ticket_id": 2},
                depends_on=[],
                error_policy=ErrorPolicy.RETRY,
                deadline=due,
            )
        ],
        max_parallel=2,
        max_steps=30,
        max_replans=2,
        deadline=due,
        budget=Budget(
            max_steps=30,
            max_replans=2,
            max_model_calls=50,
            max_tool_calls=50,
            deadline=due,
        ),
    )
    return {
        "task_plan": plan.model_dump(mode="json", by_alias=True),
        "request_write": False,
        "review_result": {
            "decision": review_decision.value,
            "risk_level": "medium",
            "feedback": "fixture",
            "reviewed_evidence_refs": [],
        },
    }


def test_policy_abstain_is_hard_terminal() -> None:
    policy = SupervisorPolicy()
    legal = policy.legal_actions(_policy_state(ReviewDecision.ABSTAIN))
    assert legal == {SupervisorAction.FINALIZE}
    policy.validate(
        SupervisorDecision(
            action=SupervisorAction.FINALIZE,
            selected_task_ids=[],
            rationale_summary="Terminal abstention.",
            confidence=1,
        ),
        _policy_state(ReviewDecision.ABSTAIN),
    )


def test_policy_abstain_never_allows_escalate_or_replan() -> None:
    policy = SupervisorPolicy()
    legal = policy.legal_actions(_policy_state(ReviewDecision.ABSTAIN))
    assert SupervisorAction.ESCALATE not in legal
    assert SupervisorAction.REPLAN not in legal
    assert SupervisorAction.RETRIEVE_MORE not in legal


# ================================================ C) supervisor end-to-end


class FakeRepository:
    def __init__(self, tenant_id: UUID) -> None:
        assert tenant_id == TENANT
        self.events: list[tuple[str, dict]] = []

    async def update_run(self, run_id, status, *, result=None, error=None):
        self.last_status = status.value
        self.last_result = result
        return SimpleNamespace(id=run_id, status=status.value, result=result, error=error)

    async def append_event(self, run_id, event_type, payload):
        self.events.append((event_type, payload))
        return SimpleNamespace()

    async def save_action_intent(self, **kwargs):
        return SimpleNamespace(id=uuid4(), **kwargs)


class FakeData:
    async def get_ticket_evidence(self, context, ticket_id):
        return [glpi_ticket(), glpi_group()]


class RoundAwareKnowledge:
    def __init__(self) -> None:
        self.rounds: list[int] = []

    async def retrieve(self, *, tenant_id, query, retrieval_round=0):
        self.rounds.append(retrieval_round)
        if retrieval_round >= 1:
            return [knowledge_evidence("Network Team owns VPN gateway faults.")]
        return []


class LoopAnalysis:
    async def analyze_evidence(self, joined, goal, *, request_write, ticket_id):
        return _analysis(evidence_refs=joined.evidence_refs)


def _task(task_id: str, agent: AgentName, depends_on: list[str]) -> Task:
    due = datetime.now(UTC) + timedelta(minutes=5)
    return Task(
        task_id=task_id,
        agent=agent,
        task_type="retrieve_knowledge" if agent is AgentName.KNOWLEDGE else "analyze",
        input={"objective": "probe", "ticket_id": 2},
        depends_on=depends_on,
        error_policy=ErrorPolicy.RETRY,
        deadline=due,
    )


def _closed_plan(goal: str, tasks: list[Task]) -> TaskPlan:
    due = datetime.now(UTC) + timedelta(minutes=5)
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


class LoopPlanner:
    async def create_plan(self, *, goal, ticket_id, request_write, correction=None):
        tasks = [
            _task("T1", AgentName.DATA, []),
            _task("T2", AgentName.KNOWLEDGE, []),
            _task("T3", AgentName.ANALYSIS, ["T1", "T2"]),
            _task("T4", AgentName.REVIEWER, ["T3"]),
        ]
        return _closed_plan(goal, tasks)

    async def revise_plan(self, *, previous, review, ticket_id, request_write, correction=None):
        tasks = [
            _task("T5", AgentName.KNOWLEDGE, []),
            _task("T6", AgentName.ANALYSIS, ["T5"]),
            _task("T7", AgentName.REVIEWER, ["T6"]),
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
                        if action in {SupervisorAction.ANALYZE, SupervisorAction.REVIEW}
                        else []
                    ),
                    rationale_summary=f"Progress workflow with {action.value}.",
                    confidence=1,
                )
        raise AssertionError(f"no legal action in {legal}")


def _initial_state() -> dict:
    return {
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
        "raw_request": "Analyze VPN using the relevant runbook",
        "goal": "Analyze VPN using the relevant runbook",
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


@pytest.mark.asyncio
async def test_supervisor_retrieve_then_abstain_terminal(monkeypatch) -> None:
    repo = FakeRepository(TENANT)
    knowledge = RoundAwareKnowledge()
    # Round 1 (after retrieve-more) delivers cited knowledge; the semantic judge then
    # reports claim C1 unsupported by that evidence -> adjudicator must ABSTAIN.
    semantic = _semantic(claims_supported=False, unsupported=["C1"])
    reviewer = ReviewerAgent(enable_semantic_review=True, model_factory=lambda: object())
    monkeypatch.setattr(
        reviewer_module, "structured_output", lambda model, schema: FakeRunnable(semantic)
    )
    graph = build_supervisor_graph(
        SupervisorRuntimeServices(
            router=FastPathRouter(),
            supervisor=StateDrivenSupervisor(),
            planner=LoopPlanner(),
            policy=SupervisorPolicy(),
            dispatcher=TaskDispatcher(),
            data=FakeData(),
            knowledge=knowledge,  # type: ignore[arg-type]
            analysis=LoopAnalysis(),
            reviewer=reviewer,
            repository_factory=lambda tenant_id: repo,
        )
    )
    result = await graph.ainvoke(_initial_state())
    # A retrieval round ran before abstaining -- the recoverable-evidence chance is
    # never skipped -- and the second reviewer pass (round 1) abstained.
    assert knowledge.rounds == [0, 1]
    assert result["final_result"]["review"]["decision"] == "abstain"
    assert repo.last_status == "succeeded"
    assert any(event_type == "run.abstained" for event_type, _ in repo.events)
    actions = [
        item.removeprefix("decision:")
        for item in result["trajectory"]
        if item.startswith("decision:")
    ]
    assert actions[-1] == "finalize"
    assert "abstain" not in result["trajectory"]  # decision label lives on review result
    # No human escalation interrupt was reached for an ordinary info gap.
    assert "human_escalation" not in result["trajectory"]
