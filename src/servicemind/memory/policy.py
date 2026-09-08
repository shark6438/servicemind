from __future__ import annotations

import json

from servicemind.memory.contracts import (
    PII_PATTERN,
    SECRET_PATTERN,
    MemoryCandidate,
    MemoryType,
    MemoryWriteAction,
    MemoryWriteDecision,
    SemanticSubtype,
)

INJECTION_MARKERS = (
    "ignore previous",
    "ignore all prior",
    "override policy",
    "bypass approval",
    "system prompt",
    "忽略所有",
    "绕过审批",
    "无视系统",
)


class MemoryGovernancePolicy:
    """Deterministic fail-closed write policy; an LLM never decides activation."""

    def __init__(self, *, auto_activation_confidence: float = 0.9) -> None:
        if not 0.5 <= auto_activation_confidence <= 1:
            raise ValueError("auto activation confidence must be between 0.5 and 1")
        self.auto_activation_confidence = auto_activation_confidence

    def assess(self, candidate: MemoryCandidate) -> MemoryWriteDecision:
        reasons: list[str] = []
        inspected = json.dumps(
            {"content": candidate.content, "provenance": candidate.provenance,
             "evidence_refs": [ref.model_dump() for ref in candidate.evidence_refs]},
            ensure_ascii=False,
        )
        normalized = inspected.casefold()
        if SECRET_PATTERN.search(inspected):
            return MemoryWriteDecision(
                action=MemoryWriteAction.REJECT,
                reason_codes=("SECRET_DETECTED",),
            )
        if any(marker in normalized for marker in INJECTION_MARKERS):
            reasons.append("PROMPT_INJECTION_TAINT")
        if candidate.taint_labels:
            reasons.append("UNRESOLVED_TAINT")
        if PII_PATTERN.search(inspected) and not (
            candidate.semantic_subtype is SemanticSubtype.PREFERENCE
            and candidate.consent_ref
        ):
            reasons.append("PII_REQUIRES_CONSENT_OR_REDACTION")
        if candidate.importance < 0.4:
            return MemoryWriteDecision(
                action=MemoryWriteAction.REJECT,
                reason_codes=("NOT_WORTH_SAVING",),
            )
        if candidate.memory_type is MemoryType.PROCEDURAL:
            reasons.append("PROCEDURAL_REQUIRES_HUMAN_REVIEW")
        if candidate.memory_type is not MemoryType.SEMANTIC or (
            candidate.semantic_subtype is not SemanticSubtype.PREFERENCE
        ):
            if not candidate.evidence_refs or not all(
                reference.verified for reference in candidate.evidence_refs
            ):
                reasons.append("EVIDENCE_NOT_VERIFIED")
        if candidate.memory_type is MemoryType.EPISODIC and not candidate.final_state_verified:
            reasons.append("FINAL_STATE_NOT_VERIFIED")
        if candidate.provenance.get("conflict_detected"):
            reasons.append("CONFLICT_DETECTED")
        if candidate.confidence < self.auto_activation_confidence:
            reasons.append("CONFIDENCE_BELOW_AUTO_ACTIVATION")
        if reasons:
            return MemoryWriteDecision(
                action=MemoryWriteAction.QUARANTINE,
                reason_codes=tuple(dict.fromkeys(reasons)),
            )
        return MemoryWriteDecision(
            action=MemoryWriteAction.ACTIVATE,
            reason_codes=("DETERMINISTIC_POLICY_PASSED",),
        )
