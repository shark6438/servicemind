from uuid import UUID

import pytest
from fastapi import HTTPException

from servicemind.agents.action import action_agent
from servicemind.domain.models import ActionIntent, TicketAnalysis
from servicemind.security.auth import TenantContext

RUN_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def analysis() -> TicketAnalysis:
    return TicketAnalysis(
        category="network/vpn",
        impact=3,
        urgency=4,
        recommended_priority=4,
        summary="VPN MFA outage",
        recommended_group="Network Team",
        confidence=0.8,
        evidence=["GLPI Ticket #2"],
    )


def test_action_intent_hash_is_stable_and_content_bound() -> None:
    first = ActionIntent.calculate_hash(
        run_id=RUN_ID,
        action_type="append_ticket_followup",
        target_id=2,
        arguments={"content": "hello", "is_private": True},
    )
    reordered = ActionIntent.calculate_hash(
        run_id=RUN_ID,
        action_type="append_ticket_followup",
        target_id=2,
        arguments={"is_private": True, "content": "hello"},
    )
    changed = ActionIntent.calculate_hash(
        run_id=RUN_ID,
        action_type="append_ticket_followup",
        target_id=3,
        arguments={"content": "hello", "is_private": True},
    )

    assert first == reordered
    assert first != changed


def test_action_agent_only_proposes_approved_phase2_action() -> None:
    intent = action_agent.propose_followup(RUN_ID, 2, analysis())

    assert intent.action_type == "append_ticket_followup"
    assert intent.target_id == 2
    assert intent.requires_approval is True
    assert intent.risk_level == "low"
    assert len(intent.action_hash) == 64


def test_tenant_context_enforces_roles() -> None:
    context = TenantContext(
        tenant_id=UUID("11111111-1111-4111-8111-111111111111"),
        user_id="subject",
        username="analyst",
        roles={"analyst"},
        allowed_glpi_entity_ids={1},
    )

    context.require_role("analyst")
    with pytest.raises(HTTPException) as exc_info:
        context.require_role("approver")
    assert exc_info.value.status_code == 403
