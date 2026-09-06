import hashlib
import json
from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from servicemind.runtime.contracts import stable_digest


class TicketAnalysis(BaseModel):
    category: str
    impact: int = Field(ge=1, le=5)
    urgency: int = Field(ge=1, le=5)
    recommended_priority: int = Field(ge=1, le=5)
    summary: str
    recommended_group: str
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)
    source: Literal["llm", "deterministic_fallback"] = "llm"


class ActionIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID | None = None
    run_id: UUID
    action_type: Literal["append_ticket_followup"]
    target_id: int
    arguments: dict[str, Any]
    risk_level: Literal["low", "medium", "high"] = "low"
    requires_approval: bool = True
    action_hash: str
    tenant_id: UUID | None = None
    requested_by: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    idempotency_context: dict[str, str] = Field(default_factory=dict)
    intent_version: str = "v1"
    policy_version: str | None = None
    review_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    evidence_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    expires_at: datetime | None = None
    dry_run_preview: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def validate_v2_security_context(self) -> Self:
        if self.intent_version != "v2":
            return self
        required = {
            "tenant_id": self.tenant_id,
            "requested_by": self.requested_by,
            "policy_version": self.policy_version,
            "review_digest": self.review_digest,
            "evidence_digest": self.evidence_digest,
            "expires_at": self.expires_at,
        }
        missing = [key for key, value in required.items() if value is None]
        if missing:
            raise ValueError(f"ActionIntent v2 is missing security fields: {missing}")
        if not self.evidence_refs:
            raise ValueError("ActionIntent v2 requires reviewed evidence references")
        if self.evidence_digest != stable_digest(sorted(self.evidence_refs)):
            raise ValueError("ActionIntent evidence digest does not match evidence references")
        assert self.expires_at is not None
        if self.expires_at.tzinfo is None:
            raise ValueError("ActionIntent expiry must be timezone-aware")
        return self

    @staticmethod
    def calculate_hash(
        *,
        run_id: UUID,
        action_type: str,
        target_id: int,
        arguments: dict[str, Any],
        security_context: dict[str, Any] | None = None,
    ) -> str:
        value = {
            "run_id": str(run_id),
            "action_type": action_type,
            "target_id": target_id,
            "arguments": arguments,
        }
        if security_context is not None:
            value["security_context"] = security_context
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def verify_integrity(self) -> None:
        security_context = None
        if self.intent_version == "v2":
            security_context = {
                "tenant_id": str(self.tenant_id),
                "requested_by": self.requested_by,
                "evidence_refs": sorted(self.evidence_refs),
                "idempotency_context": self.idempotency_context,
                "policy_version": self.policy_version,
                "review_digest": self.review_digest,
                "evidence_digest": self.evidence_digest,
                "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            }
        expected = self.calculate_hash(
            run_id=self.run_id,
            action_type=self.action_type,
            target_id=self.target_id,
            arguments=self.arguments,
            security_context=security_context,
        )
        if expected != self.action_hash:
            raise ValueError("ActionIntent integrity validation failed")


class ApprovalDecision(BaseModel):
    decision: Literal["approved", "rejected"]
    decided_by: str
    comment: str | None = None


class ExecutionResult(BaseModel):
    tool_name: str
    followup_id: int
    ticket_id: int
    verified: bool
    duplicate_suppressed: bool = False
