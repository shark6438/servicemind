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


#: What each ``claim_type`` asks of the evidence a claim cites. One text, quoted by both
#: roles that reason with these names rather than restated by each.
#:
#: They used to be two private inferences from the same five words, and the inferences
#: were not the same rule. The Analysis Agent was handed the bare literal through
#: ``AnalysisResult.model_json_schema()`` and chose a type from the name; the Reviewer's
#: semantic judge was handed a hand-written bar and re-derived its own. On ACC-07
#: (2026-09-23) three runs of one deterministic input returned passed, abstain, passed --
#: and the abstention rested on claims that quote a cited graph row's own sentence
#: ("Same-CI correlation points at a shared root cause") back with the attribution kept
#: ("the graph states this..."), which the judge's own feedback conceded the evidence
#: states. A listed claim is terminal for the run: ``unsupported_claim_ids`` is an error
#: finding, so a single disagreement between the two inferences becomes
#: ``retrieve_more`` and then a refusal to answer a question the evidence answered.
#: Quoting one text into both roles is what makes a disagreement mean the evidence
#: disagrees with the analysis, instead of the two prompts disagreeing with each other.
CLAIM_TYPE_BAR: dict[str, str] = {
    "incident_fact": (
        "the cited evidence states it for this incident -- a ticket field, a recorded "
        "check or follow-up, or a statement a cited record makes. A claim that reports "
        "what a record says is stated by that record, provided the claim attributes it "
        '("the graph states...", "the runbook instructs..."); the record it reports may '
        "be another ticket's or a runbook's, and \"for this incident\" asks what the "
        "record is about, not that the record be this ticket's own. Judge the substance: "
        "a fact, number, actor or relationship the cited evidence does not carry is "
        "unsupported; a claim it does state is supported however it might have been "
        "phrased more cautiously"
    ),
    "root_cause_hypothesis": (
        "the cited evidence carries a cause or a mechanism, or the claim records it in assumptions"
    ),
    "priority_reason": (
        "the cited evidence supports following the advice, not that it repeats the "
        "recommendation verbatim"
    ),
    "assignment_reason": (
        "the cited evidence supports following the advice -- the ticket's own record of "
        "who it was reported to or where the work already went, a recorded ownership or "
        "disposition statement, or the ticket's assignment field when it is set. Judge "
        "the substance: the claim need not prove the ownership the recommendation "
        "proposes, and an assignment stated as a recommendation, with its residual "
        "uncertainty in the claim's assumptions, is supported when the cited evidence "
        "points at that group -- the disclaimer that the evidence does not record "
        "ownership is the hedge the claim was asked for, not a defect in it. A "
        "support-group directory on its own shows the group exists, not that it owns "
        "this work, so a recommendation resting on nothing else is unsupported"
    ),
    "recommended_action": (
        "the cited evidence supports following the advice -- a runbook rule or a "
        "recorded disposition statement -- and not that it states the recommendation "
        "verbatim; an action with no supporting basis is still unsupported"
    ),
}

#: The same bar as one sentence, which is the form the Reviewer's judge is asked to
#: apply. Derived rather than written twice: the two roles must be unable to disagree
#: about what ``claim_type`` means without disagreeing about this text.
CLAIM_TYPE_BAR_TEXT = "; ".join(f"{name}: {bar}" for name, bar in CLAIM_TYPE_BAR.items())


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
    ] = Field(description=CLAIM_TYPE_BAR_TEXT)
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
