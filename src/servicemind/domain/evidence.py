import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvidenceSourceType(StrEnum):
    GLPI = "glpi"
    KNOWLEDGE = "knowledge"
    MEMORY = "memory"
    GRAPH = "graph"
    EXTERNAL = "external"


class EvidenceProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=100)
    retrieval_method: str = Field(min_length=1, max_length=100)
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def require_timezone(self) -> Self:
        if self.retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at must be timezone-aware")
        return self


class Evidence(BaseModel):
    """A bounded fact with tenant identity and verifiable provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str = Field(pattern=r"^ev-[0-9a-f]{16}$")
    tenant_id: UUID
    source_type: EvidenceSourceType
    source_ref: str = Field(min_length=1, max_length=500)
    resource_type: str = Field(min_length=1, max_length=100)
    resource_id: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=8000)
    provenance: EvidenceProvenance
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    confidence: float | None = Field(default=None, ge=0, le=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        tenant_id: UUID,
        source_type: EvidenceSourceType,
        source_ref: str,
        resource_type: str,
        resource_id: str,
        content: str,
        provider: str,
        retrieval_method: str,
        confidence: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Self:
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        identity = "|".join(
            [str(tenant_id), source_type.value, source_ref, resource_type, resource_id, content_hash]
        )
        evidence_id = f"ev-{hashlib.sha256(identity.encode()).hexdigest()[:16]}"
        return cls(
            evidence_id=evidence_id,
            tenant_id=tenant_id,
            source_type=source_type,
            source_ref=source_ref,
            resource_type=resource_type,
            resource_id=resource_id,
            content=content,
            provenance=EvidenceProvenance(
                provider=provider,
                retrieval_method=retrieval_method,
                content_hash=content_hash,
            ),
            confidence=confidence,
            metadata=metadata or {},
        )


class JoinedEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: UUID
    items: list[Evidence] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def enforce_tenant_and_unique_ids(self) -> Self:
        identifiers: set[str] = set()
        for item in self.items:
            if item.tenant_id != self.tenant_id:
                raise ValueError("Evidence from another tenant cannot be joined")
            if item.evidence_id in identifiers:
                raise ValueError("Joined evidence IDs must be unique")
            identifiers.add(item.evidence_id)
        return self

    @property
    def evidence_refs(self) -> list[str]:
        return [item.evidence_id for item in self.items]


def join_evidence(tenant_id: UUID, evidence: list[Evidence]) -> JoinedEvidence:
    """Deterministically de-duplicate evidence without discarding provenance."""
    unique: dict[tuple[str, str], Evidence] = {}
    for item in evidence:
        if item.tenant_id != tenant_id:
            raise ValueError("Evidence tenant mismatch")
        key = (item.source_ref, item.provenance.content_hash)
        unique.setdefault(key, item)
    return JoinedEvidence(tenant_id=tenant_id, items=list(unique.values()))
