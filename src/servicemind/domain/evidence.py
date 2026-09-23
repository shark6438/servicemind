import hashlib
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from servicemind.domain.integrity import contains_injection_marker
from servicemind.domain.knowledge import AuthorityLevel


class EvidenceSourceType(StrEnum):
    GLPI = "glpi"
    KNOWLEDGE = "knowledge"
    MEMORY = "memory"
    GRAPH = "graph"
    EXTERNAL = "external"


#: The metadata key knowledge retrieval publishes a document's ``AuthorityLevel`` under,
#: and the key :attr:`Evidence.authority_level` reads it back from. Named once because the
#: writer (``rag.service``) and the readers (the context envelope, the reviewer) are in
#: different modules, and a restated literal is how two sides of one contract drift apart.
AUTHORITY_LEVEL_KEY = "authority_level"

#: The metadata key knowledge retrieval binds a row to the document it came from under.
#: Same reason as above: the writer is ``rag.service``, and the readers are the reviewer's
#: citation gate and the withdrawal path that suspends a poisoned document.
CITATION_KEY = "citation"

#: What each evidence source is worth when this row does not say. Only knowledge retrieval
#: ranks per document -- a runbook, a PagerDuty post and a licensed external corpus are all
#: ``KNOWLEDGE`` and are emphatically not worth the same -- so every other provider is
#: mapped by what it is: the live system of record (``GLPI``, and the graph projected from
#: it), or a case resolved by an earlier run (``MEMORY``).
DEFAULT_AUTHORITY: Mapping[EvidenceSourceType, AuthorityLevel] = {
    EvidenceSourceType.GLPI: AuthorityLevel.GLPI_LIVE,
    EvidenceSourceType.KNOWLEDGE: AuthorityLevel.INTERNAL_KNOWLEDGE,
    EvidenceSourceType.GRAPH: AuthorityLevel.GLPI_LIVE,
    EvidenceSourceType.MEMORY: AuthorityLevel.TENANT_RESOLVED_CASE,
    EvidenceSourceType.EXTERNAL: AuthorityLevel.EXTERNAL_BEST_PRACTICE,
}

#: The highest level a *stored document* may claim. Every other source is read from the
#: system it describes; a knowledge row is a copy, and the envelope sorts all of them by
#: this number. Without the ceiling a document indexed under a compromised or merely
#: hand-edited authority field would outrank the incident's own live record -- the row
#: would be answering from a copy while claiming to be the original.
KNOWLEDGE_AUTHORITY_CEILING = AuthorityLevel.INTERNAL_KNOWLEDGE


def self_authored_marker(run_id: object, action_hash: str) -> str:
    """The marker the platform appends to every followup it writes back to GLPI.

    Named once because three places depend on the exact spelling and a restated literal is
    how they drift apart: the executor writes it, the executor's crash-recovery path
    searches for it, and the evidence builder uses it to tell the tenant's ticket record
    apart from the platform's own prose.
    """
    return f"[ServiceMind run={run_id} action={action_hash[:16]}]"


#: Recognises :func:`self_authored_marker` in text read back from GLPI.
#:
#: This is the only durable signal of authorship a followup carries: GLPI stores the
#: platform's notes and the service desk's notes as the same kind of row, written by the
#: same service account, so nothing in the API response distinguishes them. Without this
#: the platform re-ingests its own past conclusions as ``GLPI_LIVE`` evidence -- authority
#: 100, the top of the ranking -- and a run ends up arguing from a restatement of an
#: earlier run instead of from the ticket. Live regression, 2026-09-23: two such
#: followups took 3774 of the 4921 evidence tokens the Analyst was allowed to see and
#: pushed every knowledge document out of its context.
SELF_AUTHORED_MARKER = re.compile(r"\[ServiceMind run=[^\s\]]+ action=[0-9a-f]{16}\]")


def is_self_authored(content: str) -> bool:
    """Whether this GLPI followup text was written by ServiceMind itself."""
    return SELF_AUTHORED_MARKER.search(content) is not None


#: Hard ceiling on one serialized evidence row. The RAG chunker sizes parents below
#: this so a retrieved window always serializes whole; anything that still exceeds it
#: (legacy rows, other providers) must be bounded at the evidence boundary, never crash.
EVIDENCE_CONTENT_MAX = 8000

#: Ceilings on the identifying pair, shared with the providers that build them. Graph-RAG
#: declares its own, looser bounds on the same values -- ``GraphNode.ref`` allows 1000
#: characters against 500 here, and ``GraphNode.key`` allows 500 against 255 -- so a
#: finding whose node came from a long external reference converts into an ``Evidence``
#: that this model rejects. Two unreconciled bounds for one value is the same defect as a
#: restated literal: whichever side is not enforced is the side that fails.
EVIDENCE_SOURCE_REF_MAX = 500
EVIDENCE_RESOURCE_ID_MAX = 255


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
    source_ref: str = Field(min_length=1, max_length=EVIDENCE_SOURCE_REF_MAX)
    resource_type: str = Field(min_length=1, max_length=100)
    resource_id: str = Field(min_length=1, max_length=EVIDENCE_RESOURCE_ID_MAX)
    content: str = Field(min_length=1, max_length=EVIDENCE_CONTENT_MAX)
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
            [
                str(tenant_id),
                source_type.value,
                source_ref,
                resource_type,
                resource_id,
                content_hash,
            ]
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

    @property
    def authority_level(self) -> AuthorityLevel:
        """What this evidence is worth, as the source that produced it declared it.

        The envelope sorts by authority before it applies any budget, so this number is
        the one that decides which rows survive a crowded prompt. It was flattened to a
        single constant for every row, which meant ``AuthorityLevel`` -- declared by each
        source, stored on the index, read back on every hit, written into this row's
        metadata -- had no effect on anything: a public historical post and an internal
        runbook tied, and the packer broke the tie on the evidence id, a hash.

        A knowledge row is capped at :data:`KNOWLEDGE_AUTHORITY_CEILING` rather than
        trusted outright, because unlike every other source it is a *copy* whose level
        travelled through storage. An unknown level falls back to this source's default
        instead of raising: the value crosses a deployment boundary inside a JSON column,
        and a row written before a level was retired must still be readable.
        """
        declared = self.metadata.get(AUTHORITY_LEVEL_KEY)
        if declared is None:
            return DEFAULT_AUTHORITY[self.source_type]
        try:
            level = AuthorityLevel(int(declared))
        except (TypeError, ValueError):
            return DEFAULT_AUTHORITY[self.source_type]
        if self.source_type is not EvidenceSourceType.KNOWLEDGE:
            return level
        return min(level, KNOWLEDGE_AUTHORITY_CEILING)

    @property
    def source_record_id(self) -> str | None:
        """The stored document this row was retrieved from, if it came from an index.

        Read out of the citation rather than off the row because the citation is where
        retrieval binds a passage back to its document, and that binding is what a
        withdrawal has to act on. Returns ``None`` for every provider that is not a
        search index -- a live GLPI read has no stored copy to suspend.
        """
        raw = self.metadata.get(CITATION_KEY)
        if not isinstance(raw, dict):
            return None
        record = raw.get("source_record_id")
        return record if isinstance(record, str) and record else None

    def taints(self) -> frozenset[str]:
        """Every reason this row must not be handed to a model as content.

        ``untrusted_content`` is structural: evidence is retrieved text, and retrieved
        text is data, never instructions. ``prompt_injection`` is the deterministic
        tripwire, applied here rather than trusted to the semantic judge -- the judge is
        asked whether the *envelope* looks injected, which arrives after the analysis
        model has already read the passage, and cannot name the row even when it is
        right. A taint is only worth having if something refuses on it, and
        ``BLOCKED_TAINTS`` in the context builder is that refusal.
        """
        taints = {"untrusted_content"}
        if contains_injection_marker(self.content):
            taints.add("prompt_injection")
        return frozenset(taints)


#: How many evidence rows one joined set may carry. The join enforces this itself rather
#: than trusting its inputs to: ``data_evidence`` and ``knowledge_evidence`` accumulate
#: with ``operator.add`` across every dispatch of a run and are never cleared, so a single
#: data task (one ticket, up to 50 support groups, up to 15 followups) plus one knowledge
#: task already approaches the ceiling and a second dispatch passes it. The overflow used
#: to reach ``JoinedEvidence`` and raise there, killing a run that had gathered all of its
#: evidence successfully.
JOINED_EVIDENCE_MAX = 100


class JoinedEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: UUID
    items: list[Evidence] = Field(min_length=1, max_length=JOINED_EVIDENCE_MAX)

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


#: Which evidence survives when a run gathers more than a joined set may hold. The order
#: is the order an answer is grounded in, not the order the run gathered it: the incident's
#: own record and its history are the facts being explained, retrieved knowledge and graph
#: findings are what explains them, and directory rows -- the tenant's support groups --
#: are enumerable context the analysis can be given a slice of without losing a fact about
#: this incident. A run that gathers 60 support groups and one ticket must not spend its
#: whole budget on the directory.
_INCIDENT_RECORD = 0
_RETRIEVED_CONTEXT = 1
_EXTERNAL_CORPUS = 2
_ENUMERABLE_DIRECTORY = 3


def _evidence_priority(item: Evidence) -> int:
    if item.source_type is EvidenceSourceType.GLPI:
        if item.resource_type == "support_group":
            return _ENUMERABLE_DIRECTORY
        return _INCIDENT_RECORD
    if item.source_type is EvidenceSourceType.EXTERNAL:
        return _EXTERNAL_CORPUS
    return _RETRIEVED_CONTEXT


def bounded_join(tenant_id: UUID, evidence: list[Evidence]) -> tuple[JoinedEvidence, int]:
    """Join evidence within ``JOINED_EVIDENCE_MAX``, reporting how many rows it dropped.

    Selection is by ``_evidence_priority``; the rows that survive keep the order they were
    gathered in, so the joined set is always a subsequence of the input. The drop count is
    returned rather than swallowed -- a run whose analysis saw less evidence than it
    gathered has to say so, or the ledger reads as if nothing was left out.
    """
    unique: dict[tuple[str, str], Evidence] = {}
    for item in evidence:
        if item.tenant_id != tenant_id:
            raise ValueError("Evidence tenant mismatch")
        key = (item.source_ref, item.provenance.content_hash)
        unique.setdefault(key, item)
    candidates = list(unique.values())
    if len(candidates) > JOINED_EVIDENCE_MAX:
        keep = {
            index
            for index, _ in sorted(
                enumerate(candidates),
                key=lambda pair: (_evidence_priority(pair[1]), pair[0]),
            )[:JOINED_EVIDENCE_MAX]
        }
        candidates = [item for index, item in enumerate(candidates) if index in keep]
    joined = JoinedEvidence(tenant_id=tenant_id, items=candidates)
    return joined, len(unique) - len(candidates)


def join_evidence(tenant_id: UUID, evidence: list[Evidence]) -> JoinedEvidence:
    """Deterministically de-duplicate evidence without discarding provenance."""
    return bounded_join(tenant_id, evidence)[0]
