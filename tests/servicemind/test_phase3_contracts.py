from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from servicemind.domain.evidence import (
    JOINED_EVIDENCE_MAX,
    Evidence,
    EvidenceSourceType,
    bounded_join,
    join_evidence,
)
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


def directory_row(index: int) -> Evidence:
    return Evidence.create(
        tenant_id=TENANT_ID,
        source_type=EvidenceSourceType.GLPI,
        source_ref=f"glpi://groups/{index}",
        resource_type="support_group",
        resource_id=str(index),
        content=f"GLPI support group: Team {index}",
        provider="glpi-v2",
        retrieval_method="list_groups",
    )


def test_a_run_that_gathers_too_much_evidence_keeps_the_incident_not_the_directory() -> None:
    """Two channels accumulate across every dispatch, so the join must bound itself.

    ``data_evidence`` and ``knowledge_evidence`` are ``operator.add`` accumulators that no
    node ever clears: one data task is a ticket plus up to 50 support groups plus up to 15
    followups, so a second dispatch passes the ceiling. The overflow used to reach
    ``JoinedEvidence`` and raise there, killing a run that had gathered all of its evidence
    successfully -- and the evidence the incident is actually explained by was the evidence
    most likely to be lost, because the directory rows are the bulk of it.
    """
    gathered = [evidence(), *[directory_row(index) for index in range(JOINED_EVIDENCE_MAX)]]
    assert len(gathered) > JOINED_EVIDENCE_MAX

    joined, dropped = bounded_join(TENANT_ID, gathered)

    assert len(joined.items) == JOINED_EVIDENCE_MAX
    assert dropped == len(gathered) - JOINED_EVIDENCE_MAX
    # The ticket survives every directory row.
    assert gathered[0] in joined.items
    # The kept rows stay a subsequence of what was gathered, in gathering order.
    kept = [gathered.index(item) for item in joined.items]
    assert kept == sorted(kept)


def test_a_join_within_the_ceiling_drops_nothing_and_preserves_order() -> None:
    gathered = [evidence(), directory_row(1), directory_row(2)]

    joined, dropped = bounded_join(TENANT_ID, gathered)

    assert dropped == 0
    assert joined.items == gathered


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
