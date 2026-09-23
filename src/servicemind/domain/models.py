import hashlib
import json
from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from servicemind.domain.integrity import stable_digest
from servicemind.domain.task import TICKET_ID_MAX


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


#: Ceiling on ``ActionIntent.dry_run_preview``. The Action Agent composes the preview
#: from the analysis summary, the reviewer's feedback and every reviewed evidence
#: reference. Each is bounded on its own -- 2000, 2000 and an unbounded list -- and
#: nothing bounds the composition: at the field ceilings it reaches ~6600 characters, so
#: an analysis that is merely verbose at both ends built an ``ActionIntent`` pydantic
#: rejected, and the call site threw the run away instead of finalizing it.
ACTION_PREVIEW_MAX = 4000

#: The two version strings an ``action_intents`` row holds, quoted from their columns.
#: Both are free-form here and both are written straight through, so an over-long one is
#: the same failure as an over-long identity: the row is built, the driver refuses it, and
#: a run that had already passed review ends without a finalize record. ``handoff``
#: quotes ``ACTION_POLICY_VERSION_MAX`` too -- the value it passes in is this field.
INTENT_VERSION_MAX = 20
ACTION_POLICY_VERSION_MAX = 100


#: The role a requester must hold, at the moment of execution, for the platform's one
#: action to be performed on their behalf. Quoted from the endpoint that lets a run be
#: created at all (``create_run`` requires ``analyst``): a step may not be executed under
#: a role that would not have been allowed to ask for it. Establishing that a subject is
#: who they claim to be and establishing that they may perform this operation are two
#: questions, and a verified subject holding no role answers only the first.
ACTION_REQUIRED_ROLES = frozenset({"analyst"})


class ActionIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID | None = None
    run_id: UUID
    action_type: Literal["append_ticket_followup"]
    #: ``action_intents.target_id`` shares the run table's 32-bit ticket id contract.
    target_id: int = Field(le=TICKET_ID_MAX)
    arguments: dict[str, Any]
    risk_level: Literal["low", "medium", "high"] = "low"
    requires_approval: bool = True
    action_hash: str
    tenant_id: UUID | None = None
    requested_by: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    idempotency_context: dict[str, str] = Field(default_factory=dict)
    intent_version: str = Field(default="v1", max_length=INTENT_VERSION_MAX)
    policy_version: str | None = Field(default=None, max_length=ACTION_POLICY_VERSION_MAX)
    review_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    evidence_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    expires_at: datetime | None = None
    dry_run_preview: str | None = Field(default=None, max_length=ACTION_PREVIEW_MAX)

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
