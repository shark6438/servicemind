from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from servicemind.domain.evidence import Evidence, EvidenceSourceType, join_evidence
from servicemind.domain.handoff import HandoffEnvelope
from servicemind.domain.review import ReviewDecision, ReviewResult, RiskLevel
from servicemind.domain.task import Budget, BudgetSnapshot

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")


def evidence(content: str = "Ticket 2 reports VPN MFA failure") -> Evidence:
    return Evidence.create(
        tenant_id=TENANT_ID,
        source_type=EvidenceSourceType.GLPI,
        source_ref="glpi://tickets/2",
        resource_type="ticket",
        resource_id="2",
        content=content,
        provider="glpi-v2",
        retrieval_method="get_ticket",
    )


def passed_review(ref: str) -> ReviewResult:
    return ReviewResult(
        decision=ReviewDecision.PASSED,
        risk_level=RiskLevel.LOW,
        feedback="Evidence and proposed action are consistent.",
        reviewed_evidence_refs=[ref],
    )


def test_evidence_identity_is_stable_and_provenance_is_content_bound() -> None:
    first = evidence()
    second = evidence()
    changed = evidence("Different fact")
    assert first.evidence_id == second.evidence_id
    assert first.provenance.content_hash == second.provenance.content_hash
    assert first.evidence_id != changed.evidence_id


def test_join_deduplicates_without_losing_provenance() -> None:
    item = evidence()
    joined = join_evidence(TENANT_ID, [item, item])
    assert joined.items == [item]
    assert joined.evidence_refs == [item.evidence_id]
    assert joined.items[0].provenance.provider == "glpi-v2"


def test_join_rejects_cross_tenant_evidence() -> None:
    other = evidence().model_copy(
        update={"tenant_id": UUID("22222222-2222-4222-8222-222222222222")}
    )
    with pytest.raises(ValueError, match="tenant"):
        join_evidence(TENANT_ID, [other])


def test_handoff_requires_passed_review_and_one_frozen_operation() -> None:
    item = evidence()
    envelope = HandoffEnvelope(
        run_id=uuid4(),
        tenant_id=TENANT_ID,
        user_id="user-1",
        evidence_refs=[item.evidence_id],
        review_result=passed_review(item.evidence_id),
        allowed_operations=["append_ticket_followup"],
        risk_level=RiskLevel.LOW,
        remaining_budget=BudgetSnapshot(
            remaining_steps=5,
            remaining_replans=1,
            remaining_model_calls=2,
            remaining_tool_calls=2,
            deadline=datetime.now(UTC) + timedelta(minutes=5),
        ),
        idempotency_context={"run_id": "run-1"},
        handoff_reason="Reviewer passed the recommendation.",
    )
    assert envelope.allowed_operations == ["append_ticket_followup"]
    with pytest.raises(ValidationError, match="passed review"):
        HandoffEnvelope.model_validate(
            {
                **envelope.model_dump(),
                "review_result": {
                    **envelope.review_result.model_dump(),
                    "decision": "reject",
                },
            }
        )
    with pytest.raises(ValidationError, match="sensitive"):
        HandoffEnvelope.model_validate(
            {
                **envelope.model_dump(),
                "idempotency_context": {"access_token": "forbidden"},
            }
        )


def test_budget_contract_rejects_naive_deadline() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        Budget(deadline=datetime.now())
