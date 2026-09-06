from datetime import datetime
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from servicemind.domain.review import ReviewDecision, ReviewResult, RiskLevel
from servicemind.domain.task import BudgetSnapshot
from servicemind.runtime.contracts import stable_digest

SENSITIVE_KEY_PARTS = {"secret", "password", "credential", "access_token", "api_key"}


class HandoffEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    tenant_id: UUID
    user_id: str = Field(min_length=1, max_length=255)
    evidence_refs: list[str] = Field(min_length=1)
    review_result: ReviewResult
    allowed_operations: list[str] = Field(min_length=1)
    risk_level: RiskLevel
    remaining_budget: BudgetSnapshot
    idempotency_context: dict[str, Any]
    handoff_reason: str = Field(min_length=1, max_length=1000)
    policy_version: str = Field(default="servicemind-action-policy-v2", min_length=1)
    review_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    evidence_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def enforce_safe_handoff(self) -> Self:
        if self.review_result.decision is not ReviewDecision.PASSED:
            raise ValueError("Only a passed review can be handed off to Action Agent")
        if set(self.allowed_operations) != {"append_ticket_followup"}:
            raise ValueError("Phase 3 handoff may only allow append_ticket_followup")
        keys = {str(key).casefold() for key in self.idempotency_context}
        if any(part in key for key in keys for part in SENSITIVE_KEY_PARTS):
            raise ValueError("Handoff idempotency context contains a sensitive key")
        if not set(self.evidence_refs) <= set(self.review_result.reviewed_evidence_refs):
            raise ValueError("Handoff contains evidence not covered by the passed review")
        calculated_review_digest = stable_digest(self.review_result)
        if self.review_digest is not None and self.review_digest != calculated_review_digest:
            raise ValueError("Handoff review digest does not match ReviewResult")
        calculated_evidence_digest = stable_digest(sorted(self.evidence_refs))
        if self.evidence_digest is not None and self.evidence_digest != calculated_evidence_digest:
            raise ValueError("Handoff evidence digest does not match evidence references")
        object.__setattr__(self, "review_digest", calculated_review_digest)
        object.__setattr__(self, "evidence_digest", calculated_evidence_digest)
        object.__setattr__(self, "expires_at", self.expires_at or self.remaining_budget.deadline)
        assert self.expires_at is not None
        if self.expires_at.tzinfo is None:
            raise ValueError("Handoff expiry must be timezone-aware")
        if self.expires_at > self.remaining_budget.deadline:
            raise ValueError("Handoff cannot outlive the remaining run budget")
        return self
