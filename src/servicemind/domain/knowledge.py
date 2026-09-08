from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from typing import Any, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CorpusScope(StrEnum):
    TENANT = "tenant"
    GLOBAL_LICENSED = "global_licensed"


class AuthorityLevel(IntEnum):
    PUBLIC_HISTORICAL = 20
    EXTERNAL_BEST_PRACTICE = 40
    TENANT_RESOLVED_CASE = 70
    INTERNAL_KNOWLEDGE = 80
    GLPI_LIVE = 100


class BlockType(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    LIST = "list"
    CODE = "code"
    OTHER = "other"


class RetrievalIntent(StrEnum):
    PROCEDURE = "procedure"
    POLICY = "policy"
    HISTORICAL_CASE = "historical_case"
    GENERAL_KNOWLEDGE = "general_knowledge"


class RetrievalMode(StrEnum):
    """Which search channel produces the candidate set.

    Baselines (evaluation) compare DENSE / BM25 / HYBRID; production defaults to
    HYBRID (RRF fusion over dense + BM25). Mode is never a security control -- the
    ACL pre-filter is applied identically in every mode.
    """

    DENSE = "dense"
    BM25 = "bm25"
    HYBRID = "hybrid"


class KnowledgeACL(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    corpus_scope: CorpusScope
    tenant_id: UUID | None = None
    entity_ids: frozenset[int] = Field(default_factory=frozenset)
    group_ids: frozenset[int] = Field(default_factory=frozenset)
    profile_ids: frozenset[int] = Field(default_factory=frozenset)
    user_ids: frozenset[str] = Field(default_factory=frozenset)
    effective_from: datetime = Field(default_factory=lambda: datetime.now(UTC))
    effective_to: datetime | None = None
    is_active: bool = True

    @model_validator(mode="after")
    def validate_scope_and_time(self) -> Self:
        if self.corpus_scope is CorpusScope.TENANT and self.tenant_id is None:
            raise ValueError("Tenant-scoped knowledge requires tenant_id")
        if self.corpus_scope is CorpusScope.GLOBAL_LICENSED and self.tenant_id is not None:
            raise ValueError("Global licensed knowledge cannot carry a tenant_id")
        if self.effective_from.tzinfo is None:
            raise ValueError("effective_from must be timezone-aware")
        if self.effective_to is not None:
            if self.effective_to.tzinfo is None:
                raise ValueError("effective_to must be timezone-aware")
            if self.effective_to <= self.effective_from:
                raise ValueError("effective_to must be later than effective_from")
        return self


class KnowledgeProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str = Field(min_length=1, max_length=100)
    source_version: str = Field(min_length=1, max_length=200)
    source_uri: str = Field(min_length=1, max_length=1000)
    source_record_id: str = Field(min_length=1, max_length=500)
    license: str = Field(min_length=1, max_length=100)
    authority_level: AuthorityLevel
    synthetic: bool = False
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class KnowledgeDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: UUID = Field(default_factory=uuid4)
    schema_version: str = "knowledge-document-v1"
    title: str = Field(min_length=1, max_length=1000)
    language: str = Field(default="en", min_length=2, max_length=20)
    document_type: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    acl: KnowledgeACL
    provenance: KnowledgeProvenance

    @staticmethod
    def content_digest(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @model_validator(mode="after")
    def validate_hash(self) -> Self:
        if self.provenance.content_hash != self.content_digest(self.content):
            raise ValueError("KnowledgeDocument content does not match provenance hash")
        return self


class KnowledgeBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    block_id: UUID = Field(default_factory=uuid4)
    document_id: UUID
    section_path: list[str] = Field(default_factory=list)
    order: int = Field(ge=0)
    block_type: BlockType
    content: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ParentChunk(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    parent_chunk_id: UUID = Field(default_factory=uuid4)
    document_id: UUID
    section_path: list[str] = Field(default_factory=list)
    block_ids: list[UUID] = Field(min_length=1)
    order: int = Field(ge=0)
    content: str = Field(min_length=1)
    token_count: int = Field(ge=1)


class ChildChunk(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    child_chunk_id: UUID = Field(default_factory=uuid4)
    parent_chunk_id: UUID
    document_id: UUID
    section_path: list[str] = Field(default_factory=list)
    order: int = Field(ge=0)
    content: str = Field(min_length=1)
    token_count: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class KnowledgeQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    raw_query: str = Field(min_length=1, max_length=4000)
    normalized_query: str = Field(min_length=1, max_length=4000)
    rewritten_queries: list[str] = Field(default_factory=list, max_length=3)
    identifiers: list[str] = Field(default_factory=list, max_length=30)
    entities: list[str] = Field(default_factory=list, max_length=30)
    intent: RetrievalIntent = RetrievalIntent.GENERAL_KNOWLEDGE
    language: str = Field(default="en", min_length=2, max_length=20)


class RetrievalPrincipal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: UUID
    user_id: str
    entity_ids: frozenset[int] = Field(default_factory=frozenset)
    group_ids: frozenset[int] = Field(default_factory=frozenset)
    profile_ids: frozenset[int] = Field(default_factory=frozenset)
    query_time: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RetrievalHit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    child_chunk_id: UUID
    parent_chunk_id: UUID
    document_id: UUID
    child_content: str
    score: float
    rerank_score: float | None = None
    title: str
    source: str
    source_uri: str
    source_record_id: str
    source_version: str
    license: str
    authority_level: AuthorityLevel
    synthetic: bool
    content_hash: str
    index_version: str | None = None
    acl: KnowledgeACL


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    citation_id: str = Field(pattern=r"^cite-[0-9a-f]{16}$")
    document_id: UUID
    parent_chunk_id: UUID
    source: str
    source_uri: str
    source_record_id: str
    source_version: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    title: str

    @classmethod
    def from_hit(cls, hit: RetrievalHit) -> Self:
        value = json.dumps(
            [str(hit.document_id), str(hit.parent_chunk_id), hit.content_hash],
            separators=(",", ":"),
        )
        return cls(
            citation_id=f"cite-{hashlib.sha256(value.encode()).hexdigest()[:16]}",
            document_id=hit.document_id,
            parent_chunk_id=hit.parent_chunk_id,
            source=hit.source,
            source_uri=hit.source_uri,
            source_record_id=hit.source_record_id,
            source_version=hit.source_version,
            content_hash=hit.content_hash,
            title=hit.title,
        )


class ContextItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    parent_content: str
    hit: RetrievalHit
    citation: Citation
    token_count: int = Field(ge=1)


class KnowledgeRAGResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query: KnowledgeQuery
    items: list[ContextItem]
    retrieval_mode: str
    candidate_count: int = Field(ge=0)
    latency_ms: float = Field(ge=0)
    degraded: bool = False
    failure_code: str | None = None
