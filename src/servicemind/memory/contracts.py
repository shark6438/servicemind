from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, computed_field, model_validator


class MemoryType(StrEnum):
    SEMANTIC = "semantic"
    EPISODIC = "episodic"
    PROCEDURAL = "procedural"


class SemanticSubtype(StrEnum):
    LEARNED_FACT = "learned_fact"
    PREFERENCE = "preference"


class MemoryStatus(StrEnum):
    CANDIDATE = "candidate"
    QUARANTINE = "quarantine"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"
    EXPIRED = "expired"


class MemoryScopeType(StrEnum):
    TENANT = "tenant"
    USER = "user"
    ENTITY = "entity"
    GROUP = "group"
    SERVICE = "service"


class MemoryScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope_type: MemoryScopeType = MemoryScopeType.TENANT
    scope_id: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def require_scoped_id(self) -> MemoryScope:
        if self.scope_type is MemoryScopeType.TENANT and self.scope_id is not None:
            raise ValueError("tenant memory scope must not carry a scope_id")
        if self.scope_type is not MemoryScopeType.TENANT and not self.scope_id:
            raise ValueError("non-tenant memory scope requires scope_id")
        if self.scope_type in {MemoryScopeType.ENTITY, MemoryScopeType.GROUP}:
            if self.scope_id is None or not self.scope_id.isdecimal():
                raise ValueError("entity and group scope IDs must be non-negative integers")
        return self


class MemoryEvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str = Field(min_length=1, max_length=255)
    source_ref: str = Field(min_length=1, max_length=1000)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    verified: bool = False


def normalize_memory_content(value: str) -> str:
    return " ".join(value.casefold().split())


def memory_content_hash(value: str) -> str:
    return hashlib.sha256(normalize_memory_content(value).encode()).hexdigest()


class MemoryCandidate(BaseModel):
    """Untrusted proposal entering the deterministic memory governance pipeline."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: UUID
    scope: MemoryScope = Field(default_factory=MemoryScope)
    memory_type: MemoryType
    semantic_subtype: SemanticSubtype | None = None
    subject_key: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=8000)
    source_run_id: UUID | None = None
    source_trace_id: str = Field(min_length=1, max_length=255)
    evidence_refs: tuple[MemoryEvidenceRef, ...] = ()
    supporting_episode_ids: tuple[UUID, ...] = ()
    final_state_verified: bool = False
    consent_ref: str | None = Field(default=None, max_length=500)
    confidence: float = Field(ge=0, le=1)
    importance: float = Field(ge=0, le=1)
    valid_from: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    valid_to: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)
    taint_labels: frozenset[str] = Field(default_factory=frozenset)
    created_by: str = Field(min_length=1, max_length=255)

    @computed_field
    @property
    def content_hash(self) -> str:
        return memory_content_hash(self.content)

    @computed_field
    @property
    def idempotency_key(self) -> str:
        body = {
            "tenant_id": str(self.tenant_id),
            "scope": self.scope.model_dump(mode="json"),
            "memory_type": self.memory_type.value,
            "semantic_subtype": self.semantic_subtype.value if self.semantic_subtype else None,
            "subject_key": self.subject_key,
            "content_hash": self.content_hash,
            "source_run_id": str(self.source_run_id) if self.source_run_id else None,
        }
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    @model_validator(mode="after")
    def enforce_type_contract(self) -> MemoryCandidate:
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be later than valid_from")
        if self.expires_at is not None and self.expires_at <= self.valid_from:
            raise ValueError("expires_at must be later than valid_from")
        if self.memory_type is MemoryType.SEMANTIC:
            if self.semantic_subtype is None:
                raise ValueError("semantic memory requires semantic_subtype")
            if self.semantic_subtype is SemanticSubtype.PREFERENCE:
                if self.scope.scope_type is not MemoryScopeType.USER or not self.consent_ref:
                    raise ValueError("preference memory requires user scope and explicit consent")
            elif not self.evidence_refs:
                raise ValueError("learned facts require evidence")
        elif self.semantic_subtype is not None:
            raise ValueError("semantic_subtype is valid only for semantic memory")
        if self.memory_type is MemoryType.EPISODIC:
            if self.source_run_id is None or not self.final_state_verified:
                raise ValueError("episodic memory requires a verified source run")
            if not self.evidence_refs:
                raise ValueError("episodic memory requires evidence")
        if self.memory_type is MemoryType.PROCEDURAL:
            if len(set(self.supporting_episode_ids)) < 2:
                raise ValueError("procedural memory requires at least two supporting episodes")
            if not self.evidence_refs:
                raise ValueError("procedural memory requires verified evidence")
        return self


class MemoryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_id: UUID = Field(default_factory=uuid4)
    lineage_id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    scope: MemoryScope
    memory_type: MemoryType
    semantic_subtype: SemanticSubtype | None = None
    subject_key: str
    content: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_run_id: UUID | None = None
    source_trace_id: str
    evidence_refs: tuple[MemoryEvidenceRef, ...] = ()
    supporting_episode_ids: tuple[UUID, ...] = ()
    confidence: float = Field(ge=0, le=1)
    importance: float = Field(ge=0, le=1)
    version: int = Field(default=1, ge=1)
    valid_from: AwareDatetime
    valid_to: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None
    status: MemoryStatus
    provenance: dict[str, Any] = Field(default_factory=dict)
    taint_labels: frozenset[str] = Field(default_factory=frozenset)
    consent_ref: str | None = None
    created_by: str
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    activation_reason: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def visible_at(self, when: datetime) -> bool:
        return (
            self.status is MemoryStatus.ACTIVE
            and self.valid_from <= when
            and (self.valid_to is None or self.valid_to > when)
            and (self.expires_at is None or self.expires_at > when)
        )


class MemoryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: UUID
    text: str = Field(min_length=1, max_length=4000)
    user_id: str = Field(min_length=1, max_length=255)
    entity_ids: frozenset[int] = Field(default_factory=frozenset)
    group_ids: frozenset[int] = Field(default_factory=frozenset)
    service_ids: frozenset[str] = Field(default_factory=frozenset)
    memory_types: frozenset[MemoryType] = Field(default_factory=lambda: frozenset(MemoryType))
    at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    limit: int = Field(default=8, ge=1, le=50)

    def allows_record(self, record: MemoryRecord) -> bool:
        return (
            record.tenant_id == self.tenant_id
            and record.memory_type in self.memory_types
            and self.allows_scope(record.scope)
            and record.visible_at(self.at)
            and not record.taint_labels
            and set(record.provenance.get("required_entity_ids", ())).issubset(self.entity_ids)
            and set(record.provenance.get("required_group_ids", ())).issubset(self.group_ids)
            # Legacy post-run summaries were widened to tenant scope. Never serve
            # them while awaiting the quarantine migration.
            and not (
                record.created_by == "post-run-memory-middleware"
                and record.scope.scope_type is not MemoryScopeType.USER
            )
        )

    def allows_scope(self, scope: MemoryScope) -> bool:
        if scope.scope_type is MemoryScopeType.TENANT:
            return True
        if scope.scope_type is MemoryScopeType.USER:
            return scope.scope_id == self.user_id
        if scope.scope_type is MemoryScopeType.ENTITY:
            return bool(scope.scope_id and int(scope.scope_id) in self.entity_ids)
        if scope.scope_type is MemoryScopeType.GROUP:
            return bool(scope.scope_id and int(scope.scope_id) in self.group_ids)
        return bool(scope.scope_id and scope.scope_id in self.service_ids)


class MemorySelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    memory: MemoryRecord
    score: float = Field(ge=0, le=1)
    score_breakdown: dict[str, float]


class MemoryWriteAction(StrEnum):
    REJECT = "reject"
    QUARANTINE = "quarantine"
    ACTIVATE = "activate"


class MemoryWriteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: MemoryWriteAction
    reason_codes: tuple[str, ...]


SECRET_PATTERN = re.compile(
    r"(?i)(?:"
    r"(?:password|passwd|secret|api[_ -]?key|private[_ -]?key|token|密码|密钥)"
    r"[\"']?\s*[:=：]\s*[\"']?[^\s,;\"']{1,}"
    r"|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
    r"|(?:postgres(?:ql)?|mysql|mongodb)://[^\s:/]+:[^\s@]+@"
    r")"
)

PII_PATTERN = re.compile(
    r"(?i)(?:"
    r"(?<![\w.-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])"
    r"|(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"
    r")"
)
