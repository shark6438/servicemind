from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

import servicemind.agents.analysis as analysis_module
import servicemind.agents.data as data_module
import servicemind.agents.reviewer as reviewer_module
import servicemind.harness.executor as executor_module
from servicemind.agents.action import ActionAgent
from servicemind.agents.analysis import AnalysisAgent
from servicemind.agents.data import DataAcquisitionPlan, DataAgent, DataToolCall
from servicemind.agents.reviewer import ReviewerAgent, SemanticReview
from servicemind.domain.analysis import AnalysisClaim, AnalysisResult, AnalysisStatus
from servicemind.domain.evidence import Evidence, EvidenceSourceType, join_evidence
from servicemind.domain.handoff import HandoffEnvelope
from servicemind.domain.review import ReviewDecision, ReviewResult, RiskLevel
from servicemind.domain.task import BudgetSnapshot
from servicemind.harness.executor import ControlledActionExecutor
from servicemind.runtime.contracts import AgentInvocationContext, AgentRunStatus
from servicemind.runtime.tool_gateway import DataToolName, TenantGlpiReadGateway
from servicemind.security.auth import TenantContext

TENANT = UUID("11111111-1111-4111-8111-111111111111")
OTHER_TENANT = UUID("22222222-2222-4222-8222-222222222222")


class FakeRunnable:
    def __init__(self, *results) -> None:
        self.results = list(results)

    async def ainvoke(self, messages):
        return self.results.pop(0)


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, DataToolName, int]] = []

    async def execute(self, *, invocation, tenant_context, tool_name, ticket_id):
        assert invocation.tenant_id == tenant_context.tenant_id
        self.calls.append((invocation.tenant_id, tool_name, ticket_id))
        if tool_name is DataToolName.GET_TICKET:
            return {"id": ticket_id, "name": "VPN outage", "priority": 3}
        if tool_name is DataToolName.LIST_GROUPS:
            return [{"id": 5, "name": "Network Team"}]
        return []


def invocation(
    task_id: str,
    *,
    tenant_id: UUID = TENANT,
    capabilities: frozenset[str] = frozenset(),
    max_model_calls: int = 2,
) -> AgentInvocationContext:
    return AgentInvocationContext(
        run_id=uuid4(),
        tenant_id=tenant_id,
        user_id="analyst-1",
        task_id=task_id,
        trace_id="trace-1",
        deadline=datetime.now(UTC) + timedelta(minutes=5),
        allowed_capabilities=capabilities,
        max_model_calls=max_model_calls,
        max_tool_calls=6,
        policy_version="test-policy-v2",
    )


def tenant_context(tenant_id: UUID = TENANT) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        user_id="analyst-1",
        username="analyst",
        roles={"analyst"},
        allowed_glpi_entity_ids={1},
    )


def evidence(source: EvidenceSourceType, resource_type: str, content: str) -> Evidence:
    return Evidence.create(
        tenant_id=TENANT,
        source_type=source,
        source_ref=f"{source.value}://test/{resource_type}",
        resource_type=resource_type,
        resource_id="2",
        content=content,
        provider="test",
        retrieval_method="fixture",
    )


def model_analysis(refs: list[str]) -> AnalysisResult:
    return AnalysisResult(
        classification="network/vpn",
        impact=3,
        urgency=3,
        priority=3,
        recommended_group="Network Team",
        recurring_incident=False,
        problem_recommendation="Collect recurrence evidence.",
        change_recommendation="No change is supported.",
        reasoning_summary="Cited ticket and runbook support Network Team.",
        evidence_refs=refs,
        confidence=0.85,
        source="model",
        status=AnalysisStatus.MODEL,
        claims=[
            AnalysisClaim(
                claim_id="C1",
                claim_type="assignment_reason",
                statement="Network Team owns VPN incidents.",
                evidence_refs=refs,
                confidence=0.85,
            )
        ],
    )


@pytest.mark.asyncio
async def test_data_subgraph_compiles_invalid_model_plan_to_safe_minimum(monkeypatch) -> None:
    invalid = DataAcquisitionPlan(
        calls=[
            DataToolCall(
                tool_name=DataToolName.GET_TICKET,
                ticket_id=999,
                purpose="Attempt to change scope",
            )
        ],
        rationale_summary="Invalid model proposal",
    )
    monkeypatch.setattr(
        data_module, "structured_output", lambda model, schema: FakeRunnable(invalid)
    )
    gateway = FakeGateway()
    agent = DataAgent(gateway=gateway, model_factory=lambda: object())
    allowed = frozenset(item.value for item in DataToolName)
    result = await agent.run(
        invocation=invocation("T1", capabilities=allowed),
        tenant_context=tenant_context(),
        objective="Read the selected ticket",
        ticket_id=2,
    )
    assert result.status is AgentRunStatus.DEGRADED
    assert result.failure_code == "DATA_PLAN_DEGRADED"
    assert {(tool, ticket_id) for _, tool, ticket_id in gateway.calls} == {
        (DataToolName.GET_TICKET, 2),
        (DataToolName.LIST_GROUPS, 2),
    }
    assert result.metrics.tool_calls == 2


@pytest.mark.asyncio
async def test_tool_gateway_rejects_cross_tenant_before_resolving_credentials() -> None:
    with pytest.raises(PermissionError, match="tenant"):
        await TenantGlpiReadGateway().execute(
            invocation=invocation(
                "T1",
                capabilities=frozenset({DataToolName.GET_TICKET.value}),
            ),
            tenant_context=tenant_context(OTHER_TENANT),
            tool_name=DataToolName.GET_TICKET,
            ticket_id=2,
        )


@pytest.mark.asyncio
async def test_analysis_subgraph_revises_once_until_claims_are_grounded(monkeypatch) -> None:
    ticket = evidence(EvidenceSourceType.GLPI, "ticket", "VPN outage")
    group = evidence(EvidenceSourceType.GLPI, "support_group", "Network Team")
    runbook = evidence(EvidenceSourceType.KNOWLEDGE, "runbook", "Network Team owns VPN")
    joined = join_evidence(TENANT, [ticket, group, runbook])
    draft = model_analysis(joined.evidence_refs).model_copy(update={"claims": []})
    revised = model_analysis(joined.evidence_refs)
    runnable = FakeRunnable(draft, revised)
    monkeypatch.setattr(
        analysis_module, "structured_output", lambda model, schema: runnable
    )
    result = await AnalysisAgent(model_factory=lambda: object()).run(
        invocation=invocation("T2"),
        evidence=joined,
        goal="Analyze VPN incident",
        request_write=False,
        ticket_id=2,
    )
    assert result.status is AgentRunStatus.SUCCEEDED
    assert result.output.claims[0].evidence_refs == joined.evidence_refs
    assert result.metrics.model_calls == 2


@pytest.mark.asyncio
async def test_reviewer_semantic_judge_cannot_bypass_rule_gate(monkeypatch) -> None:
    ticket = evidence(EvidenceSourceType.GLPI, "ticket", "VPN outage")
    group = evidence(EvidenceSourceType.GLPI, "support_group", "Network Team")
    runbook = evidence(EvidenceSourceType.KNOWLEDGE, "runbook", "Network Team owns VPN")
    joined = join_evidence(TENANT, [ticket, group, runbook])
    semantic = SemanticReview(
        claims_supported=True,
        action_consistent=True,
        prompt_injection_detected=False,
        feedback="All claims are entailed by cited evidence.",
        confidence=0.9,
    )
    monkeypatch.setattr(
        reviewer_module, "structured_output", lambda model, schema: FakeRunnable(semantic)
    )
    result = await ReviewerAgent(
        enable_semantic_review=True, model_factory=lambda: object()
    ).run(
        invocation=invocation("T3", max_model_calls=1),
        analysis=model_analysis(joined.evidence_refs),
        evidence=joined,
        request_write=False,
        retrieval_round=0,
        replan_count=0,
        max_replans=2,
    )
    assert result.output.decision is ReviewDecision.PASSED
    assert result.output.policy_version == "servicemind-review-policy-v2"
    assert result.metrics.model_calls == 1


def test_action_v2_hash_detects_post_review_mutation() -> None:
    ticket = evidence(EvidenceSourceType.GLPI, "ticket", "VPN outage")
    runbook = evidence(EvidenceSourceType.KNOWLEDGE, "runbook", "Network Team owns VPN")
    analysis = model_analysis([ticket.evidence_id, runbook.evidence_id])
    review = ReviewResult(
        decision=ReviewDecision.PASSED,
        risk_level=RiskLevel.LOW,
        feedback="Passed independent review.",
        reviewed_evidence_refs=analysis.evidence_refs,
    )
    envelope = HandoffEnvelope(
        run_id=uuid4(),
        tenant_id=TENANT,
        user_id="analyst-1",
        evidence_refs=analysis.evidence_refs,
        review_result=review,
        allowed_operations=["append_ticket_followup"],
        risk_level=RiskLevel.LOW,
        remaining_budget=BudgetSnapshot(
            remaining_steps=5,
            remaining_replans=1,
            remaining_model_calls=1,
            remaining_tool_calls=1,
            deadline=datetime.now(UTC) + timedelta(minutes=5),
        ),
        idempotency_context={"run_id": "safe"},
        handoff_reason="Review passed",
    )
    intent = ActionAgent().propose_from_handoff(envelope, analysis, ticket_id=2)
    intent.verify_integrity()
    mutated = intent.model_copy(update={"arguments": {"content": "tampered"}})
    with pytest.raises(ValueError, match="integrity"):
        mutated.verify_integrity()


@pytest.mark.asyncio
async def test_harness_compares_runtime_intent_with_persisted_approval(monkeypatch) -> None:
    ticket = evidence(EvidenceSourceType.GLPI, "ticket", "VPN outage")
    runbook = evidence(EvidenceSourceType.KNOWLEDGE, "runbook", "Network Team owns VPN")
    analysis = model_analysis([ticket.evidence_id, runbook.evidence_id])
    review = ReviewResult(
        decision=ReviewDecision.PASSED,
        risk_level=RiskLevel.LOW,
        feedback="Passed independent review.",
        reviewed_evidence_refs=analysis.evidence_refs,
    )
    envelope = HandoffEnvelope(
        run_id=uuid4(),
        tenant_id=TENANT,
        user_id="analyst-1",
        evidence_refs=analysis.evidence_refs,
        review_result=review,
        allowed_operations=["append_ticket_followup"],
        risk_level=RiskLevel.LOW,
        remaining_budget=BudgetSnapshot(
            remaining_steps=5,
            remaining_replans=1,
            remaining_model_calls=1,
            remaining_tool_calls=1,
            deadline=datetime.now(UTC) + timedelta(minutes=5),
        ),
        idempotency_context={"run_id": "safe"},
        handoff_reason="Review passed",
    )
    intent = ActionAgent().propose_from_handoff(envelope, analysis, ticket_id=2)
    intent.id = uuid4()

    class FakeRepository:
        def __init__(self, tenant_id):
            assert tenant_id == TENANT

        async def get_action_intent(self, run_id):
            return SimpleNamespace(
                id=intent.id,
                action_hash="0" * 64,
                action_type=intent.action_type,
                target_id=intent.target_id,
                arguments=intent.arguments,
                intent_version=intent.intent_version,
                policy_version=intent.policy_version,
                review_digest=intent.review_digest,
                evidence_digest=intent.evidence_digest,
                evidence_refs=intent.evidence_refs,
                idempotency_context=intent.idempotency_context,
                requested_by=intent.requested_by,
                expires_at=intent.expires_at,
                status="approved",
            )

    monkeypatch.setattr(executor_module, "ServiceMindRepository", FakeRepository)
    with pytest.raises(PermissionError, match="persisted approved intent"):
        await ControlledActionExecutor().execute(tenant_context(), intent)
