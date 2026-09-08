from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from servicemind.domain.knowledge import (
    AuthorityLevel,
    CorpusScope,
    KnowledgeACL,
    KnowledgeDocument,
    KnowledgeProvenance,
    RetrievalIntent,
)

#: The committed corpus is authored "in force" from a fixed past instant. If document
#: ACLs defaulted ``effective_from`` to wall-clock *load time*, every eval process that
#: builds a RetrievalPrincipal before this source hands documents to the pipeline would
#: construct a ``query_time`` that precedes the ACL window and the ACL pre-filter would
#: silently hide the whole corpus (a process-timing-dependent empty index). A fixed past
#: date makes visibility deterministic: any principal querying "now" sees the corpus.
GOLD_CORPUS_EFFECTIVE_FROM = datetime(2026, 1, 1, tzinfo=UTC)


class GoldQuery(BaseModel):
    """One retrieval probe with its human-verified relevance set.

    ``relevant`` holds the ``source_record_id`` values of the documents that answer
    the query (empty for the unanswerable subset). ``unanswerable`` marks queries the
    agent should decline rather than fabricate an answer for.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=100)
    query: str = Field(min_length=1, max_length=4000)
    relevant: list[str] = Field(default_factory=list)
    unanswerable: bool = False
    intent: RetrievalIntent = RetrievalIntent.GENERAL_KNOWLEDGE
    language: str = Field(default="en", min_length=2, max_length=20)

    @model_validator(mode="after")
    def relevant_implies_answerable(self) -> Self:
        if self.unanswerable and self.relevant:
            raise ValueError("an unanswerable query cannot carry relevant documents")
        return self


class GoldSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "phase4-gold-v1"
    name: str
    description: str = ""
    #: source_record_id -> short title, so reports render readable names.
    documents: dict[str, str] = Field(default_factory=dict)
    queries: list[GoldQuery] = Field(default_factory=list)

    @property
    def answerable(self) -> list[GoldQuery]:
        return [query for query in self.queries if not query.unanswerable]

    @property
    def unanswerable(self) -> list[GoldQuery]:
        return [query for query in self.queries if query.unanswerable]


class GoldCorpusSource:
    """Committed synthetic SOP corpus under ``evaluation/gold/corpus``.

    The files double as structure-parser/chunker fixtures (order preservation is
    asserted in tests), and this source hands them to the ingestion pipeline exactly
    like any other source so the harness measures the real pipeline.
    """

    def __init__(self, root: Path, tenant_id: UUID, *, revision: str = "phase4-gold-v1"):
        self.root, self.tenant_id, self.revision = root, tenant_id, revision

    async def load(self) -> list[KnowledgeDocument]:
        documents = []
        for path in sorted(self.root.rglob("*.md")):
            if path.name.startswith("_"):
                continue  # parser-only fixtures are not part of the corpus
            content = path.read_text(encoding="utf-8").strip()
            stem = path.stem
            title = _first_heading(path.read_text(encoding="utf-8")) or stem
            documents.append(
                KnowledgeDocument(
                    title=title,
                    content=content,
                    document_type="internal_sop",
                    language="en",
                    metadata={"gold_corpus": True, "file": path.name},
                    acl=KnowledgeACL(
                        corpus_scope=CorpusScope.TENANT,
                        tenant_id=self.tenant_id,
                        effective_from=GOLD_CORPUS_EFFECTIVE_FROM,
                    ),
                    provenance=KnowledgeProvenance(
                        # Each gold SOP is an independent authoritative document, so it
                        # is its own ``source``: the retrieve() per-source diversity
                        # ceiling is meant to stop one large source from crowding the
                        # context, not to flatten a multi-document eval corpus into the
                        # first few parents it happens to rank.
                        source=stem,
                        source_version=self.revision,
                        source_uri=f"sop://phase4/{stem}",
                        source_record_id=stem,
                        license="project-owned",
                        authority_level=AuthorityLevel.INTERNAL_KNOWLEDGE,
                        content_hash=KnowledgeDocument.content_digest(content),
                    ),
                )
            )
        return documents


def load_gold_set(path: Path) -> GoldSet:
    return GoldSet.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _first_heading(markdown: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""
