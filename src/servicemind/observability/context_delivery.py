"""Aggregate privacy-safe production evidence for Context/Memory delivery.

The context artifact table deliberately stores selection metadata rather than prompt
content.  This module turns those manifests into deployment evidence without copying
ticket text, memory text, content hashes, or provenance references into another report.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from servicemind.context.contracts import ContextSelection, ContextSource


class DeliveryStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NO_DATA = "NO_DATA"
    INSUFFICIENT_ANALYSIS_DATA = "INSUFFICIENT_ANALYSIS_DATA"
    INSUFFICIENT_MEMORY_DATA = "INSUFFICIENT_MEMORY_DATA"


class ContextArtifactLike(Protocol):
    selection_manifest: list[dict[str, Any]]


class SourceDeliveryTotals(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_items: int = Field(ge=0)
    selected_items: int = Field(ge=0)
    pruned_items: int = Field(ge=0)
    rejected_items: int = Field(ge=0)
    candidate_tokens: int = Field(ge=0)
    selected_tokens: int = Field(ge=0)
    pruned_tokens: int = Field(ge=0)
    rejected_tokens: int = Field(ge=0)
    reasons: dict[str, int]


class Distribution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    minimum: int = Field(ge=0)
    p50: int = Field(ge=0)
    p95: int = Field(ge=0)
    maximum: int = Field(ge=0)


class ContextDeliveryGate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    min_analysis_envelopes: int = Field(ge=1)
    min_memory_candidate_envelopes: int = Field(ge=1)
    max_memory_starvation_rate: float = Field(ge=0, le=1)
    violations: tuple[str, ...]


class ContextDeliveryReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "phase5-context-delivery-observed-v1"
    generated_at: datetime
    status: DeliveryStatus
    analysis_envelopes: int = Field(ge=0)
    excluded_artifacts: int = Field(ge=0)
    excluded_user_ids: tuple[str, ...]
    malformed_artifacts: int = Field(ge=0)
    memory_candidate_envelopes: int = Field(ge=0)
    memory_starved_envelopes: int = Field(ge=0)
    memory_starvation_rate: float | None = Field(default=None, ge=0, le=1)
    memory_selected_tokens: Distribution | None
    evidence_cap_pruned_envelopes: int = Field(ge=0)
    source_totals: dict[str, SourceDeliveryTotals]
    gate: ContextDeliveryGate
    interpretation: str


def _distribution(values: list[int]) -> Distribution | None:
    if not values:
        return None
    ordered = sorted(values)

    def nearest_rank(percentile: float) -> int:
        index = max(0, int(len(ordered) * percentile + 0.999999) - 1)
        return ordered[index]

    return Distribution(
        minimum=ordered[0],
        p50=nearest_rank(0.50),
        p95=nearest_rank(0.95),
        maximum=ordered[-1],
    )


def _source_totals(entries: list[ContextSelection]) -> dict[str, SourceDeliveryTotals]:
    counts: dict[str, Counter[str]] = {}
    reasons: dict[str, Counter[str]] = {}
    for entry in entries:
        source = entry.source.value
        counts.setdefault(source, Counter())
        reasons.setdefault(source, Counter())
        counts[source]["candidate_items"] += 1
        counts[source]["candidate_tokens"] += entry.tokens
        counts[source][f"{entry.decision}_items"] += 1
        counts[source][f"{entry.decision}_tokens"] += entry.tokens
        reasons[source][entry.reason] += 1
    return {
        source: SourceDeliveryTotals(
            candidate_items=counter["candidate_items"],
            selected_items=counter["selected_items"],
            pruned_items=counter["pruned_items"],
            rejected_items=counter["rejected_items"],
            candidate_tokens=counter["candidate_tokens"],
            selected_tokens=counter["selected_tokens"],
            pruned_tokens=counter["pruned_tokens"],
            rejected_tokens=counter["rejected_tokens"],
            reasons=dict(sorted(reasons[source].items())),
        )
        for source, counter in sorted(counts.items())
    }


def build_context_delivery_report(
    artifacts: Iterable[ContextArtifactLike | Mapping[str, Any]],
    *,
    min_analysis_envelopes: int = 30,
    min_memory_candidate_envelopes: int = 30,
    max_memory_starvation_rate: float = 0.0,
    excluded_artifacts: int = 0,
    excluded_user_ids: tuple[str, ...] = (),
    generated_at: datetime | None = None,
) -> ContextDeliveryReport:
    """Build a three-state gate from persisted ANALYSIS context manifests.

    An envelope is *starved* only when Memory candidates reached ContextBuilder but none
    were selected. Runs with no retrieved Memory are kept separate: they say nothing
    about delivery and therefore cannot make this gate pass.
    """
    if min_analysis_envelopes < 1 or min_memory_candidate_envelopes < 1:
        raise ValueError("minimum sample counts must be positive")
    if not 0 <= max_memory_starvation_rate <= 1:
        raise ValueError("maximum memory starvation rate must be between zero and one")
    if excluded_artifacts < 0:
        raise ValueError("excluded artifact count must be non-negative")

    rows = list(artifacts)
    entries: list[ContextSelection] = []
    malformed = 0
    memory_candidate_envelopes = 0
    memory_starved_envelopes = 0
    evidence_cap_pruned_envelopes = 0
    memory_selected_tokens: list[int] = []

    for artifact in rows:
        raw = (
            artifact.get("selection_manifest", [])
            if isinstance(artifact, Mapping)
            else artifact.selection_manifest
        )
        try:
            manifest = [ContextSelection.model_validate(item) for item in raw]
        except (TypeError, ValueError):
            malformed += 1
            continue
        entries.extend(manifest)
        memories = [item for item in manifest if item.source is ContextSource.MEMORY]
        if memories:
            memory_candidate_envelopes += 1
            selected_tokens = sum(item.tokens for item in memories if item.decision == "selected")
            memory_selected_tokens.append(selected_tokens)
            if not any(item.decision == "selected" for item in memories):
                memory_starved_envelopes += 1
        if any(
            item.source is ContextSource.EVIDENCE
            and item.decision == "pruned"
            and item.reason == "source_token_cap_exceeded"
            for item in manifest
        ):
            evidence_cap_pruned_envelopes += 1

    starvation_rate = (
        memory_starved_envelopes / memory_candidate_envelopes
        if memory_candidate_envelopes
        else None
    )
    violations: list[str] = []
    if malformed:
        violations.append(f"malformed_context_artifacts:{malformed}")
    if starvation_rate is not None and starvation_rate > max_memory_starvation_rate:
        violations.append(
            "memory_starvation_rate_exceeded:"
            f"{starvation_rate:.6f}>{max_memory_starvation_rate:.6f}"
        )

    if not rows:
        status = DeliveryStatus.NO_DATA
        interpretation = "No ANALYSIS context artifacts exist in the selected window."
    elif malformed or violations:
        status = DeliveryStatus.FAIL
        interpretation = "Observed manifests violate the declared delivery gate."
    elif len(rows) < min_analysis_envelopes:
        status = DeliveryStatus.INSUFFICIENT_ANALYSIS_DATA
        interpretation = "The analysis-envelope sample is below the preregistered minimum."
    elif memory_candidate_envelopes < min_memory_candidate_envelopes:
        status = DeliveryStatus.INSUFFICIENT_MEMORY_DATA
        interpretation = (
            "Too few envelopes contained retrieved Memory; absence of candidates cannot "
            "be reported as successful delivery."
        )
    else:
        status = DeliveryStatus.PASS
        interpretation = "Observed Memory candidates met the declared delivery gate."

    return ContextDeliveryReport(
        generated_at=generated_at or datetime.now(UTC),
        status=status,
        analysis_envelopes=len(rows),
        excluded_artifacts=excluded_artifacts,
        excluded_user_ids=tuple(sorted(set(excluded_user_ids))),
        malformed_artifacts=malformed,
        memory_candidate_envelopes=memory_candidate_envelopes,
        memory_starved_envelopes=memory_starved_envelopes,
        memory_starvation_rate=starvation_rate,
        memory_selected_tokens=_distribution(memory_selected_tokens),
        evidence_cap_pruned_envelopes=evidence_cap_pruned_envelopes,
        source_totals=_source_totals(entries),
        gate=ContextDeliveryGate(
            min_analysis_envelopes=min_analysis_envelopes,
            min_memory_candidate_envelopes=min_memory_candidate_envelopes,
            max_memory_starvation_rate=max_memory_starvation_rate,
            violations=tuple(violations),
        ),
        interpretation=interpretation,
    )
