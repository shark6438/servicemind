from enum import StrEnum
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReviewDecision(StrEnum):
    PASSED = "passed"
    RETRIEVE_MORE = "retrieve_more"
    REPLAN = "replan"
    REJECT = "reject"
    ESCALATE = "escalate"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    check_id: str = Field(min_length=1, max_length=100)
    severity: Literal["info", "warning", "error", "critical"]
    category: Literal[
        "policy",
        "grounding",
        "contradiction",
        "prompt_injection",
        "tenant_scope",
        "action_consistency",
        "runtime",
    ]
    reason_code: str = Field(min_length=1, max_length=100)
    explanation: str = Field(min_length=1, max_length=1000)
    claim_id: str | None = Field(default=None, max_length=100)
    evidence_refs: list[str] = Field(default_factory=list)


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: ReviewDecision
    unsupported_claims: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    policy_issues: list[str] = Field(default_factory=list)
    risk_level: RiskLevel
    feedback: str = Field(min_length=1, max_length=2000)
    reviewed_evidence_refs: list[str] = Field(default_factory=list)
    review_id: UUID = Field(default_factory=uuid4)
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=100)
    reviewed_claim_ids: list[str] = Field(default_factory=list)
    policy_version: str = Field(default="servicemind-review-policy-v1", min_length=1)
    reviewer_model: str | None = Field(default=None, max_length=200)
    confidence: float = Field(default=1, ge=0, le=1)
    degraded: bool = False

    @model_validator(mode="after")
    def passed_review_must_be_clean(self) -> Self:
        blocking = [
            finding
            for finding in self.findings
            if finding.severity in {"error", "critical"}
        ]
        if self.decision is ReviewDecision.PASSED and (self.degraded or blocking):
            raise ValueError("A degraded or blocking review cannot be marked passed")
        return self
