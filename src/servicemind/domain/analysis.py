from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from servicemind.domain.review import RiskLevel


class ProposedAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: str = Field(min_length=1, max_length=100)
    resource_type: str = Field(min_length=1, max_length=100)
    resource_id: str = Field(min_length=1, max_length=255)
    arguments: dict[str, object] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(min_length=1)
    risk_level: RiskLevel


class AnalysisStatus(StrEnum):
    MODEL = "model"
    DEGRADED = "degraded"
    FAILED = "failed"


class AnalysisClaim(BaseModel):
    """One auditable conclusion and the exact evidence that supports it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_id: str = Field(pattern=r"^C[1-9][0-9]*$")
    claim_type: Literal[
        "incident_fact",
        "root_cause_hypothesis",
        "priority_reason",
        "assignment_reason",
        "recommended_action",
    ]
    statement: str = Field(min_length=1, max_length=1000)
    evidence_refs: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    assumptions: list[str] = Field(default_factory=list)


class AnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    classification: str = Field(min_length=1, max_length=200)
    impact: int = Field(ge=1, le=5)
    urgency: int = Field(ge=1, le=5)
    priority: int = Field(ge=1, le=5)
    recommended_group: str = Field(min_length=1, max_length=200)
    recurring_incident: bool
    problem_recommendation: str
    change_recommendation: str
    proposed_actions: list[ProposedAction] = Field(default_factory=list)
    reasoning_summary: str = Field(min_length=1, max_length=2000)
    evidence_refs: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    source: str = Field(min_length=1, max_length=50)
    status: AnalysisStatus = AnalysisStatus.MODEL
    claims: list[AnalysisClaim] = Field(default_factory=list, max_length=30)
    alternatives: list[str] = Field(default_factory=list, max_length=10)
    unresolved_questions: list[str] = Field(default_factory=list, max_length=20)
    validation_feedback: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def claims_must_use_declared_evidence(self) -> Self:
        declared = set(self.evidence_refs)
        unknown = {
            ref for claim in self.claims for ref in claim.evidence_refs if ref not in declared
        }
        if unknown:
            raise ValueError(f"Claims use undeclared evidence references: {sorted(unknown)}")
        return self
