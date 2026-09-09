from typing import Literal, cast
from uuid import UUID

from servicemind.domain.analysis import AnalysisResult
from servicemind.domain.handoff import HandoffEnvelope
from servicemind.domain.models import ActionIntent, TicketAnalysis
from servicemind.runtime.contracts import stable_digest


class ActionAgent:
    """Create a typed intent; it never owns credentials or executes side effects."""

    def propose_followup(
        self, run_id: UUID, ticket_id: int, analysis: TicketAnalysis
    ) -> ActionIntent:
        arguments = {
            "content": (
                f"ServiceMind analysis: {analysis.summary}\n"
                f"Category: {analysis.category}\n"
                f"Recommended priority: {analysis.recommended_priority}\n"
                f"Recommended group: {analysis.recommended_group}\n"
                f"Confidence: {analysis.confidence:.2f}"
            ),
            "is_private": True,
        }
        action_type = "append_ticket_followup"
        action_hash = ActionIntent.calculate_hash(
            run_id=run_id,
            action_type=action_type,
            target_id=ticket_id,
            arguments=arguments,
        )
        return ActionIntent(
            run_id=run_id,
            action_type=action_type,
            target_id=ticket_id,
            arguments=arguments,
            action_hash=action_hash,
        )

    def propose_from_handoff(
        self,
        handoff: HandoffEnvelope,
        analysis: AnalysisResult,
        *,
        ticket_id: int,
    ) -> ActionIntent:
        if "append_ticket_followup" not in handoff.allowed_operations:
            raise PermissionError("Handoff does not allow the Phase 3 Followup operation")
        content = (
            f"ServiceMind reviewed analysis: {analysis.reasoning_summary}\n"
            f"Classification: {analysis.classification}\n"
            f"Recommended priority: {analysis.priority}\n"
            f"Recommended group: {analysis.recommended_group}\n"
            f"Evidence: {', '.join(handoff.evidence_refs)}\n"
            f"Reviewer: {handoff.review_result.feedback}"
        )
        arguments = {"content": content, "is_private": True}
        evidence_digest = handoff.evidence_digest or stable_digest(sorted(handoff.evidence_refs))
        idempotency_context = {
            key: str(value) for key, value in handoff.idempotency_context.items()
        }
        security_context = {
            "tenant_id": str(handoff.tenant_id),
            "requested_by": handoff.user_id,
            "evidence_refs": sorted(handoff.evidence_refs),
            "idempotency_context": idempotency_context,
            "policy_version": handoff.policy_version,
            "review_digest": handoff.review_digest,
            "evidence_digest": evidence_digest,
            "expires_at": handoff.expires_at.isoformat() if handoff.expires_at else None,
        }
        action_hash = ActionIntent.calculate_hash(
            run_id=handoff.run_id,
            action_type="append_ticket_followup",
            target_id=ticket_id,
            arguments=arguments,
            security_context=security_context,
        )
        return ActionIntent(
            run_id=handoff.run_id,
            action_type="append_ticket_followup",
            target_id=ticket_id,
            arguments=arguments,
            risk_level=cast(
                Literal["low", "medium", "high"],
                "high" if handoff.risk_level.value == "critical" else handoff.risk_level.value,
            ),
            action_hash=action_hash,
            tenant_id=handoff.tenant_id,
            requested_by=handoff.user_id,
            evidence_refs=handoff.evidence_refs,
            idempotency_context=idempotency_context,
            intent_version="v2",
            policy_version=handoff.policy_version,
            review_digest=handoff.review_digest,
            evidence_digest=evidence_digest,
            expires_at=handoff.expires_at,
            dry_run_preview=content,
        )


action_agent = ActionAgent()
