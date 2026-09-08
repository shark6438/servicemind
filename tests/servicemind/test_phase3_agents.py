import hashlib
import inspect
import json
from uuid import UUID, uuid4

import pytest

import servicemind.agents.action as action_module
from servicemind.agents.action import ActionAgent
from servicemind.agents.analysis import AnalysisAgent
from servicemind.agents.knowledge import KnowledgeAgent
from servicemind.agents.reviewer import ReviewerAgent
from servicemind.domain.analysis import AnalysisResult, ProposedAction
from servicemind.domain.evidence import Evidence, EvidenceSourceType, join_evidence
from servicemind.domain.handoff import HandoffEnvelope
from servicemind.domain.knowledge import Citation
from servicemind.domain.review import ReviewDecision, ReviewResult, RiskLevel
from servicemind.domain.task import BudgetSnapshot

TENANT = UUID("11111111-1111-4111-8111-111111111111")


def knowledge_item(
    content: str,
    *,
    metadata: dict | None = None,
    provider: str = "test",
) -> Evidence:
    """KNOWLEDGE evidence carrying a citation that anchors the row itself.

    The reviewer gate validates every KNOWLEDGE item's ``metadata["citation"]``:
    the citation id must be the digest of its own (document, parent, content) and it
    must bind to the evidence (source == provider, source_uri == source_ref,
    parent_chunk_id == resource_id). This builder reproduces the shape the RAG
    service emits so fixtures pass the deterministic gate.
    """
    parent_chunk_id = uuid4()
    document_id = uuid4()
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    source_ref = f"knowledge://runbook/{content_hash[:16]}"
    citation = Citation(
        citation_id="cite-"
        + hashlib.sha256(
            json.dumps(
                [str(document_id), str(parent_chunk_id), content_hash],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:16],
        document_id=document_id,
        parent_chunk_id=parent_chunk_id,
        source=provider,
        source_uri=source_ref,
        source_record_id=f"{provider}://{parent_chunk_id}",
        source_version="v1",
        content_hash=content_hash,
        title="Fixture runbook",
    )
    return Evidence.create(
        tenant_id=TENANT,
        source_type=EvidenceSourceType.KNOWLEDGE,
        source_ref=source_ref,
        resource_type="runbook",
        resource_id=str(parent_chunk_id),
        content=content,
        provider=provider,
        retrieval_method="fixture",
        metadata={**(metadata or {}), "citation": citation.model_dump(mode="json")},
    )


def item(
    source: EvidenceSourceType,
    content: str,
    *,
    metadata: dict | None = None,
) -> Evidence:
    if source is EvidenceSourceType.KNOWLEDGE:
        return knowledge_item(content, metadata=metadata)
    return Evidence.create(
        tenant_id=TENANT,
        source_type=source,
        source_ref=f"{source.value}://source/{abs(hash(content))}",
        resource_type="ticket" if source is EvidenceSourceType.GLPI else "runbook",
        resource_id="2",
        content=content,
        provider="test",
        retrieval_method="fixture",
        metadata=metadata,
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


@pytest.mark.asyncio
async def test_knowledge_agent_returns_provenance_and_supplemental_fallback(monkeypatch) -> None:
    # Hermetic: force the deterministic baseline (RAG off) so this never reaches live
    # OpenSearch or spins up an in-process embedding model, whatever the ambient .env.
    from core import settings

    monkeypatch.setattr(settings, "SERVICEMIND_RAG_ENABLED", False)
    monkeypatch.setattr(settings, "SERVICEMIND_RAG_REQUIRED", False)
    agent = KnowledgeAgent()
    vpn = await agent.retrieve(tenant_id=TENANT, query="VPN MFA issue")
    assert vpn and all(entry.source_type is EvidenceSourceType.KNOWLEDGE for entry in vpn)
    assert all(entry.provenance.provider == "phase3-baseline-runbooks" for entry in vpn)
    assert await agent.retrieve(tenant_id=TENANT, query="zxqv", retrieval_round=0) == []
    assert await agent.retrieve(tenant_id=TENANT, query="zxqv", retrieval_round=1)


@pytest.mark.asyncio
async def test_analysis_fallback_uses_joined_evidence_and_only_safe_action() -> None:
    data = item(
        EvidenceSourceType.GLPI,
        "VPN MFA ticket",
        metadata={"ticket_facts": {"id": 2, "impact": 3, "urgency": 4, "priority": 4}},
    )
    knowledge = item(EvidenceSourceType.KNOWLEDGE, "Network Team owns VPN faults")
    result = AnalysisAgent()._fallback_evidence(
        join_evidence(TENANT, [data, knowledge]), ticket_id=2, request_write=True
    )
    assert result.evidence_refs == [data.evidence_id, knowledge.evidence_id]
    assert [action.operation for action in result.proposed_actions] == [
        "append_ticket_followup"
    ]


@pytest.mark.asyncio
async def test_reviewer_supports_all_decisions_and_never_executes_tools() -> None:
    reviewer = ReviewerAgent()
    data = item(EvidenceSourceType.GLPI, "VPN incident facts")
    group = Evidence.create(
        tenant_id=TENANT,
        source_type=EvidenceSourceType.GLPI,
        source_ref="glpi://groups/5",
        resource_type="support_group",
        resource_id="5",
        content="GLPI support group: Network Team",
        provider="test",
        retrieval_method="fixture",
    )
    knowledge = item(EvidenceSourceType.KNOWLEDGE, "Network Team owns VPN faults")
    joined = join_evidence(TENANT, [data, group, knowledge])
    passed = await reviewer.review(
        analysis=analysis(joined.evidence_refs),
        evidence=joined,
        request_write=False,
        retrieval_round=0,
        replan_count=0,
        max_replans=2,
    )
    assert passed.decision is ReviewDecision.PASSED

    retrieve = await reviewer.review(
        analysis=analysis([data.evidence_id, group.evidence_id]),
        evidence=join_evidence(TENANT, [data, group]),
        request_write=False,
        retrieval_round=0,
        replan_count=0,
        max_replans=2,
    )
    assert retrieve.decision is ReviewDecision.RETRIEVE_MORE

    conflicting = item(
        EvidenceSourceType.KNOWLEDGE,
        "Network Team owns VPN faults",
        metadata={"conflict": "Conflicting CMDB ownership"},
    )
    replan = await reviewer.review(
        analysis=analysis([data.evidence_id, group.evidence_id, conflicting.evidence_id]),
        evidence=join_evidence(TENANT, [data, group, conflicting]),
        request_write=False,
        retrieval_round=0,
        replan_count=0,
        max_replans=2,
    )
    assert replan.decision is ReviewDecision.REPLAN

    escalate = await reviewer.review(
        analysis=analysis(joined.evidence_refs, confidence=0.3),
        evidence=joined,
        request_write=False,
        retrieval_round=0,
        replan_count=0,
        max_replans=2,
    )
    assert escalate.decision is ReviewDecision.ESCALATE

    unsafe = ProposedAction(
        operation="delete_ticket",
        resource_type="ticket",
        resource_id="2",
        evidence_refs=joined.evidence_refs,
        risk_level=RiskLevel.CRITICAL,
    )
    rejected = await reviewer.review(
        analysis=analysis(joined.evidence_refs, proposed_actions=[unsafe]),
        evidence=joined,
        request_write=True,
        retrieval_round=0,
        replan_count=0,
        max_replans=2,
    )
    assert rejected.decision is ReviewDecision.REJECT
    assert not hasattr(reviewer, "tools")


def test_action_agent_is_credential_free_and_requires_passed_handoff() -> None:
    data = item(EvidenceSourceType.GLPI, "VPN incident facts")
    knowledge = item(EvidenceSourceType.KNOWLEDGE, "Network Team owns VPN faults")
    result = analysis([data.evidence_id, knowledge.evidence_id])
    review = ReviewResult(
        decision=ReviewDecision.PASSED,
        risk_level=RiskLevel.LOW,
        feedback="Passed",
        reviewed_evidence_refs=result.evidence_refs,
    )
    envelope = HandoffEnvelope(
        run_id=uuid4(),
        tenant_id=TENANT,
        user_id="user-1",
        evidence_refs=result.evidence_refs,
        review_result=review,
        allowed_operations=["append_ticket_followup"],
        risk_level=RiskLevel.LOW,
        remaining_budget=BudgetSnapshot(
            remaining_steps=5,
            remaining_replans=1,
            remaining_model_calls=2,
            remaining_tool_calls=2,
            deadline=__import__("datetime").datetime.now(__import__("datetime").UTC)
            + __import__("datetime").timedelta(minutes=5),
        ),
        idempotency_context={"run_id": "safe"},
        handoff_reason="Reviewer passed",
    )
    intent = ActionAgent().propose_from_handoff(envelope, result, ticket_id=2)
    assert intent.action_type == "append_ticket_followup"
    assert intent.tenant_id == TENANT
    source = inspect.getsource(action_module)
    assert "GlpiClient" not in source
    assert "CredentialCipher" not in source
