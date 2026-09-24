from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)


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

    @field_validator("provenance")
    @classmethod
    def validate_provenance(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("memory provenance must be JSON serializable") from exc
        if len(encoded.encode()) > 16_384:
            raise ValueError("memory provenance exceeds 16 KiB")
        for key in ("required_entity_ids", "required_group_ids"):
            raw = value.get(key, [])
            if not isinstance(raw, list) or any(
                isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in raw
            ):
                raise ValueError(f"{key} must be a list of non-negative integers")
        return value

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
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))

    def visible_at(self, when: datetime) -> bool:
        return (
            self.status is MemoryStatus.ACTIVE
            and self.valid_from <= when
            and (self.valid_to is None or self.valid_to > when)
            and (self.expires_at is None or self.expires_at > when)
        )

    def corroborable_at(self, when: datetime) -> bool:
        """Whether this record may stand as *support* for a derived conclusion.

        Deliberately wider than :meth:`visible_at`, and the difference is the whole
        point. A post-run episode is written to quarantine and stays there until a
        human reviews it, so requiring ACTIVE here meant the corroboration query could
        only ever see episodes a human had already blessed one by one -- while the
        proposal it exists to enable is precisely what a human is meant to review.
        Measured on ACC-12b (2026-09-23): two structurally isomorphic tickets produced
        episodes at confidence 0.85 against a 0.90 auto-activation threshold, so both
        sat in quarantine, and the mechanism could not start on the first pattern it
        was built to catch. Relaxing only the producer's gate changed nothing, because
        the query agreed with it -- both sides had to move together.

        Quarantine says a record is *unreviewed*, not that it is invalid, so it is
        admitted here. What stays out is what a human has actively removed or what has
        lapsed: a revoked, superseded or expired episode must never corroborate
        anything, and the time bounds still apply, so an episode outside its validity
        window supports nothing either.

        This is a predicate about *derivation*, not about serving. Nothing unreviewed
        becomes visible to a model by virtue of it: a procedure derived from these
        episodes is itself written to quarantine and needs its own human activation,
        which re-runs the same revalidation before it can turn active.
        """
        return (
            self.status in {MemoryStatus.ACTIVE, MemoryStatus.QUARANTINE}
            and self.valid_from <= when
            and (self.valid_to is None or self.valid_to > when)
            and (self.expires_at is None or self.expires_at > when)
        )

    def model_payload(self) -> dict[str, Any]:
        """This record as a model may see it: what it says, and who wrote it.

        A memory reached the prompt as its bare ``content`` string, while evidence --
        also untrusted text -- arrived as a whole row with its provider, hash and
        citation. The asymmetry hid the one field that matters most about a memory
        written by the run middleware: ``created_by``. ``POST_RUN_MEMORY_WRITER`` is
        already a first-class distinction in this module, used at the serving boundary
        to refuse pre-5.1 rows, so a passage whose ``outcome`` is prose a *previous
        run's model* wrote was being read as an undifferentiated fact about the estate.

        Everything the record knows about itself *as a row* is dropped: ``memory_id``,
        ``lineage_id``, ``tenant_id``, ``source_run_id``, ``content_hash``,
        ``idempotency_key`` and ``version`` are keys the reader cannot resolve -- the
        item's own ``item_id`` already names this record, and ``provenance`` carries
        the ticket and review a human would recognise instead. ``scope`` is an ACL
        coordinate the query already enforced; restating it invites the model to reason
        about authorization it cannot act on. ``created_at``/``updated_at``/``status``
        are storage: a record only reaches a prompt while it is active.
        ``evidence_refs``/``supporting_episode_ids`` are ids the model does not cite,
        and ``taint_labels`` is already stated on the context item that carries this
        payload. Nothing that describes the warrant is dropped -- ``created_by``,
        ``provenance``, ``activation_reason``, ``consent_ref`` and the validity window
        all travel, because a reader deciding how much to trust a passage needs exactly
        those. The line matters: a full row cost ~650 characters to carry a 65-character
        memory, and the memory channel competes for a shared token budget.
        """
        return self.model_dump(
            mode="json",
            exclude={
                "memory_id",
                "lineage_id",
                "tenant_id",
                "source_run_id",
                "content_hash",
                "source_trace_id",
                "evidence_refs",
                "supporting_episode_ids",
                "version",
                "idempotency_key",
                "taint_labels",
                "scope",
                "status",
                "created_at",
                "updated_at",
            },
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
        required_entities = _required_acl_ids(record.provenance, "required_entity_ids")
        required_groups = _required_acl_ids(record.provenance, "required_group_ids")
        return (
            record.tenant_id == self.tenant_id
            and record.memory_type in self.memory_types
            and self.allows_scope(record.scope)
            and record.visible_at(self.at)
            and not record.taint_labels
            and required_entities is not None
            and required_entities.issubset(self.entity_ids)
            and required_groups is not None
            and required_groups.issubset(self.group_ids)
            # Automated ticket summaries written before Phase 5.1 used tenant scope.
            # Never serve them: the widening was never reviewed and the rows predate
            # the entity/group ACL. New summaries declare the scope on purpose and
            # carry the ACL, so the marker -- not the writer's identity -- is what
            # separates the two. See :data:`POST_RUN_SCOPE_POLICY`.
            and not (
                record.created_by == POST_RUN_MEMORY_WRITER
                and record.scope.scope_type is not MemoryScopeType.USER
                and record.provenance.get("post_run_scope") != POST_RUN_TENANT_EPISODE_POLICY
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


class MemoryReviewQuery(BaseModel):
    """Authorization and keyset-pagination boundary for the human review queue."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: UUID
    reviewer_id: str = Field(min_length=1, max_length=255)
    entity_ids: frozenset[int] = Field(default_factory=frozenset)
    group_ids: frozenset[int] = Field(default_factory=frozenset)
    service_ids: frozenset[str] = Field(default_factory=frozenset)
    memory_types: frozenset[MemoryType] = Field(default_factory=lambda: frozenset(MemoryType))
    at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    after_created_at: AwareDatetime | None = None
    after_memory_id: UUID | None = None
    limit: int = Field(default=50, ge=1, le=100)

    @model_validator(mode="after")
    def require_complete_cursor(self) -> MemoryReviewQuery:
        if (self.after_created_at is None) != (self.after_memory_id is None):
            raise ValueError("review cursor requires both timestamp and memory id")
        return self

    def allows_record(self, record: MemoryRecord) -> bool:
        required_entities = _required_acl_ids(record.provenance, "required_entity_ids")
        required_groups = _required_acl_ids(record.provenance, "required_group_ids")
        after_cursor = True
        if self.after_created_at is not None and self.after_memory_id is not None:
            after_cursor = (record.created_at, record.memory_id) > (
                self.after_created_at,
                self.after_memory_id,
            )
        return (
            record.tenant_id == self.tenant_id
            and record.status is MemoryStatus.QUARANTINE
            and record.memory_type in self.memory_types
            and self.allows_scope(record.scope)
            and record.valid_from <= self.at
            and (record.valid_to is None or record.valid_to > self.at)
            and (record.expires_at is None or record.expires_at > self.at)
            and required_entities is not None
            and required_entities.issubset(self.entity_ids)
            and required_groups is not None
            and required_groups.issubset(self.group_ids)
            and after_cursor
        )

    def allows_scope(self, scope: MemoryScope) -> bool:
        if scope.scope_type is MemoryScopeType.TENANT:
            return True
        if scope.scope_type is MemoryScopeType.USER:
            return scope.scope_id == self.reviewer_id
        if scope.scope_type is MemoryScopeType.ENTITY:
            return bool(scope.scope_id and int(scope.scope_id) in self.entity_ids)
        if scope.scope_type is MemoryScopeType.GROUP:
            return bool(scope.scope_id and int(scope.scope_id) in self.group_ids)
        return bool(scope.scope_id and scope.scope_id in self.service_ids)


class MemoryPatternQuery(BaseModel):
    """Exact, ACL-constrained lookup for episodes supporting one learned procedure."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: UUID
    pattern_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    entity_ids: frozenset[int] = Field(default_factory=frozenset)
    group_ids: frozenset[int] = Field(default_factory=frozenset)
    at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    limit: int = Field(default=50, ge=2, le=100)

    def allows_record(self, record: MemoryRecord) -> bool:
        required_entities = _required_acl_ids(record.provenance, "required_entity_ids")
        required_groups = _required_acl_ids(record.provenance, "required_group_ids")
        return (
            record.tenant_id == self.tenant_id
            and record.memory_type is MemoryType.EPISODIC
            and record.scope.scope_type is MemoryScopeType.TENANT
            and record.corroborable_at(self.at)
            and not record.taint_labels
            and record.created_by == POST_RUN_MEMORY_WRITER
            and record.provenance.get("post_run_scope") == POST_RUN_TENANT_EPISODE_POLICY
            and record.provenance.get("procedure_pattern_key") == self.pattern_key
            and required_entities is not None
            and required_entities.issubset(self.entity_ids)
            and required_groups is not None
            and required_groups.issubset(self.group_ids)
        )


#: The one writer that produces records outside the caller's own scope.
POST_RUN_MEMORY_WRITER = "post-run-memory-middleware"

#: ``provenance["post_run_scope"]`` value a post-run writer sets when its non-USER
#: scope is a deliberate, ACL-carrying choice rather than the pre-Phase 5.1 widening.
#: Absent (the case for every legacy row) means "assume widened, do not serve".
POST_RUN_TENANT_EPISODE_POLICY = "tenant_episode_v2"

#: A conservative automatic proposer may create a PROCEDURAL candidate only
#: after the same normalized recommendation appears in distinct verified tickets.
CROSS_TICKET_PROCEDURE_POLICY = "cross_ticket_verified_episode_v1"

#: Identity recorded on automatically proposed procedural candidates.
POST_RUN_PROCEDURAL_WRITER = "post-run-procedural-proposer"


def _required_acl_ids(provenance: dict[str, Any], key: str) -> frozenset[int] | None:
    raw = provenance.get(key, [])
    if not isinstance(raw, list) or any(
        isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in raw
    ):
        return None
    return frozenset(raw)


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


class MemoryEvent(BaseModel):
    """One append-only memory transition, in the shape both authorities record it.

    The in-memory authority did not record transitions at all -- its ``transition`` and
    ``revoke_by_evidence`` opened with ``del actor_id`` -- so a test that "verified"
    memory governance was verifying a repository that kept no ledger, and the run
    correlation below could not be asserted anywhere without a PostgreSQL fixture.

    ``created_at`` is absent rather than optional on purpose: the database fills it with
    a server default so the ledger's ordering is the database's clock, and a field here
    would invite a caller to supply one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: UUID
    memory_id: UUID
    actor_id: str
    event_type: str
    reason_codes: tuple[str, ...] = ()
    payload: dict[str, Any] = Field(default_factory=dict)
    #: The run that caused this transition, when a run caused it. ``None`` is a fact --
    #: TTL expiry, procedure support revalidation and human review happen outside any
    #: run, and naming one for them would be a fabrication rather than a correlation.
    run_id: UUID | None = None
    #: The run's trace, when it has one. ``phase5_governance`` sets a memory's
    #: ``source_trace_id`` to the run's thread id when there is one and to the run id
    #: otherwise, and this carries the same value for the same reason.
    trace_id: str | None = None


def memory_event(
    *,
    tenant_id: UUID,
    memory_id: UUID,
    actor_id: str,
    event_type: str,
    reason_codes: Sequence[str] = (),
    payload: Mapping[str, Any] | None = None,
    run_id: UUID | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """The one construction of a memory event, shared by both authorities.

    Returns keywords rather than an object so the PostgreSQL authority can build its ORM
    row and the in-memory authority its :class:`MemoryEvent` from the same field set. Two
    independent constructions of "what a memory event contains" is how the ledger and the
    thing the tests assert on drift apart -- and the drift is invisible, because both
    sides keep working.
    """
    return {
        "tenant_id": tenant_id,
        "memory_id": memory_id,
        "actor_id": actor_id,
        "event_type": event_type,
        "reason_codes": list(reason_codes),
        "payload": dict(payload or {}),
        "run_id": run_id,
        "trace_id": trace_id,
    }


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
