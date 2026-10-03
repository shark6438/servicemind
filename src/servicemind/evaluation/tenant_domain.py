"""Load a tenant-domain release set: a corpus with ACL strata, and its qrels.

§3 scopes the §4.1 closure gate to a *tenant-domain release set* -- private tenant queries,
expert-signed qrels, ACL and tenant strata, a tune/hold-out partition. Every corpus loader
in this repository loads the same shape: ``evaluation/gold``'s synthetic SOPs, one tenant,
no groups, one effective window, binary relevance. That corpus cannot carry the gate even
in principle, because the checks the gate makes are about strata it does not have -- the
wrong-tenant count has no second tenant to be wrong about, and the wrong-group count has no
restricted document to over-share.

This module loads a corpus that does. It is the only new thing this step adds on the
reading side; the harness, the leakage checker and the gate are unchanged, because the
point of putting the strata in the *data* is that the code consuming them already works.

The manifest format is ``evaluation/acceptance/fixtures/globex/manifest.json``'s, extended
rather than replaced: that file already declares each document's groups and retirement
flag, on the explicit reasoning that a restriction stated only in prose lets an isolation
case pass against an all-public corpus. The extension adds what a *release set* needs and
an acceptance fixture did not: a second tenant, an effective window, an authority level and
a version. A manifest that omits them still loads, and reads as one tenant, in force, at
internal-knowledge authority.

Three rules, all of them "fail closed":

* **An ACL is declared, never defaulted.** A corpus file with no manifest entry raises. The
  convenient default -- treat an unlisted file as tenant-public -- is exactly what makes a
  stratum silently disappear: the file is indexed, it is retrievable, and the run reports
  it as correctly restricted because nothing ever said it was restricted.
* **An entry must name a file.** An entry with no corpus file behind it raises, so a
  renamed file cannot leave the strata reported from a document set that no longer exists.
* **A judgment must be about this corpus.** A qrels ``source_record_id`` the corpus does
  not contain raises, because recall over a key that was never indexed measures a join and
  reports the result as a retrieval miss.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from servicemind.domain.knowledge import (
    AuthorityLevel,
    CorpusScope,
    KnowledgeACL,
    KnowledgeDocument,
    KnowledgeProvenance,
)
from servicemind.evaluation.gold import GOLD_CORPUS_EFFECTIVE_FROM, GoldSet
from servicemind.evaluation.leakage import EvidenceCoordinates

MANIFEST_NAME = "manifest.json"
QRELS_NAME = "qrels.json"

#: Tenant aliases a manifest may write instead of a UUID literal. A fixture spanning two
#: tenants is unreadable as raw UUIDs, and the tenant is the one coordinate a reader most
#: needs to see. An alias that is not here is an error rather than a new tenant: a typo
#: would otherwise create a tenant that exists only inside the fixture, and the wrong-tenant
#: count would be comparing a real tenant against a misspelling.
TENANTS: Mapping[str, UUID] = {
    "acme": UUID("11111111-1111-4111-8111-111111111111"),
    "globex": UUID("22222222-2222-4222-8222-222222222222"),
}

#: Authority levels a manifest may write by name, so that a typo is a lookup error and not
#: a silently different level.
AUTHORITY_LEVELS: Mapping[str, AuthorityLevel] = {
    "public_historical": AuthorityLevel.PUBLIC_HISTORICAL,
    "external_best_practice": AuthorityLevel.EXTERNAL_BEST_PRACTICE,
    "tenant_resolved_case": AuthorityLevel.TENANT_RESOLVED_CASE,
    "internal_knowledge": AuthorityLevel.INTERNAL_KNOWLEDGE,
    "glpi_live": AuthorityLevel.GLPI_LIVE,
}

#: The instant a fixture's queries are asked, unless the queryset says otherwise. Fixed and
#: in the future of every ``effective_from`` an ordinary document writes, so that "in force"
#: is a property of the fixture and not of when the process happened to run -- the same
#: reasoning as ``GOLD_CORPUS_EFFECTIVE_FROM``. A fixture that wants a not-yet-in-force
#: document writes an ``effective_from`` after this instant.
FIXTURE_QUERY_TIME = datetime(2026, 5, 1, tzinfo=UTC)

#: The suffix a document-level judgment carries on ``parent_chunk_id``. §3.4 wants the
#: judgment at chunk granularity, and a static fixture cannot have it: ``parent_chunk_id``
#: is minted by the chunker at index time and differs with the chunking configuration, so a
#: fixture that wrote one would be writing an id that belongs to a particular run of a
#: particular pipeline -- and a manifest that pinned it would silently stop matching the
#: next time the chunker changed. The judgments are therefore recorded against the document,
#: in a form that says so, and chunk-granular judging belongs to whatever step first reads
#: the ingested chunk ids.
DOCUMENT_LEVEL = "#document"


class TenantDomainError(ValueError):
    """A fixture that cannot be loaded without inventing something it did not say."""


class DocumentDeclaration(BaseModel):
    """The declaration one manifest entry makes about one corpus file.

    ``tenant`` accepts an alias or a UUID literal and defaults to the manifest's own
    ``tenant_id``; everything else is what ``KnowledgeACL`` and ``KnowledgeProvenance``
    hold. Defaults are the values that make a single-tenant acceptance fixture load
    unchanged -- in force, internal knowledge, project-owned -- and none of them is a
    default about *who may see the document*, which is the one axis a default could make
    silently permissive.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    file: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)

    tenant: str | None = None
    group_ids: frozenset[int] = frozenset()
    entity_ids: frozenset[int] = frozenset()
    is_active: bool = True

    version: str = "tenant-domain-v1"
    authority: str = "internal_knowledge"
    license: str = "project-owned"
    document_type: str = "internal_sop"
    title: str = ""

    effective_from: datetime = GOLD_CORPUS_EFFECTIVE_FROM
    effective_to: datetime | None = None

    #: Free prose for the reader of the manifest. Not consumed; kept because the existing
    #: fixture documents *why* each restriction is what it is, and that reasoning is the
    #: part a reviewer checks.
    role: str = ""

    @property
    def authority_level(self) -> AuthorityLevel:
        try:
            return AUTHORITY_LEVELS[self.authority]
        except KeyError as error:
            raise TenantDomainError(
                f"unknown authority level {self.authority!r}; known levels are "
                f"{sorted(AUTHORITY_LEVELS)}"
            ) from error

    def resolved_tenant(self, default: UUID) -> UUID:
        if self.tenant is None:
            return default
        if self.tenant in TENANTS:
            return TENANTS[self.tenant]
        try:
            return UUID(self.tenant)
        except ValueError as error:
            raise TenantDomainError(
                f"tenant {self.tenant!r} is neither a known alias {sorted(TENANTS)} nor a "
                "UUID. A fixture must not be able to name a tenant that no other part of "
                "the platform knows, because the wrong-tenant count would then be "
                "comparing a real tenant against a misspelling"
            ) from error

    @model_validator(mode="after")
    def the_window_is_ordered_and_aware(self) -> DocumentDeclaration:
        if self.effective_from.tzinfo is None:
            raise TenantDomainError(f"{self.file}: effective_from must be timezone-aware")
        if self.effective_to is not None:
            if self.effective_to.tzinfo is None:
                raise TenantDomainError(f"{self.file}: effective_to must be timezone-aware")
            if self.effective_to <= self.effective_from:
                raise TenantDomainError(
                    f"{self.file}: effective_to {self.effective_to} is not after "
                    f"effective_from {self.effective_from}, so the document is never in "
                    "force and the expired-version stratum it was meant to populate is "
                    "empty"
                )
        return self


class FixtureManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "tenant-domain-fixtures-v1"
    tenant_id: UUID | None = None
    #: GLPI's entity for this fixture, carried for the acceptance fixtures that seed against
    #: it. Nothing here reads it; it is kept so the existing manifest validates unchanged.
    glpi_entity_id: int | None = None
    query_time: datetime = FIXTURE_QUERY_TIME
    revision: str = "tenant-domain-v1"
    note: str = ""
    documents: list[DocumentDeclaration] = Field(default_factory=list)

    @model_validator(mode="after")
    def a_tenant_is_resolvable_for_every_document(self) -> FixtureManifest:
        if self.tenant_id is None and any(item.tenant is None for item in self.documents):
            raise TenantDomainError(
                "the manifest declares no tenant_id, so documents that do not name their "
                "own tenant have no tenant to belong to"
            )
        if self.query_time.tzinfo is None:
            raise TenantDomainError("query_time must be timezone-aware")
        return self

    @property
    def default_tenant(self) -> UUID:
        if self.tenant_id is None:  # pragma: no cover - guarded by the validator above
            raise TenantDomainError("the manifest declares no tenant_id")
        return self.tenant_id


class TenantDomainFixture:
    """A directory holding ``corpus/*.md``, ``manifest.json`` and ``qrels.json``.

    Read once and held, so the documents handed to the index and the coordinates handed to
    the audit come from the same parse. Loading them separately is how the two drift: the
    audit would check the ACLs the fixture *says* while the index holds the ACLs the loader
    *built*, and a bug in the translation between them would be invisible to the only check
    that exists to find it.
    """

    def __init__(self, root: Path):
        self.root = root
        self._documents: list[KnowledgeDocument] | None = None
        self._manifest: FixtureManifest | None = None

    @property
    def corpus_dir(self) -> Path:
        """Where the markdown lives: ``corpus/`` when there is one, else the root.

        Both layouts exist already -- ``evaluation/gold`` keeps its SOPs in a ``corpus``
        subdirectory, and the acceptance fixtures keep theirs beside their manifest. Taking
        whichever is there means neither has to be moved to be readable.
        """
        nested = self.root / "corpus"
        return nested if nested.is_dir() else self.root

    @property
    def manifest(self) -> FixtureManifest:
        if self._manifest is None:
            path = self.root / MANIFEST_NAME
            try:
                self._manifest = FixtureManifest.model_validate(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except ValidationError as error:
                # Pydantic wraps whatever a validator raises, so the declaration rules
                # above would reach a caller as ``ValidationError`` and a caller catching
                # this module's own error would miss exactly the malformed manifests it was
                # written for. Re-raised under one type so "this fixture does not say what
                # it means" is one thing to catch.
                raise TenantDomainError(f"{path} is not a usable manifest: {error}") from error
        return self._manifest

    @property
    def query_time(self) -> datetime:
        return self.manifest.query_time

    def _corpus_files(self) -> list[Path]:
        return sorted(
            path for path in self.corpus_dir.rglob("*.md") if not path.name.startswith("_")
        )

    def documents(self) -> list[KnowledgeDocument]:
        """The corpus, with each document's declared ACL applied verbatim."""
        if self._documents is not None:
            return list(self._documents)

        declarations = {item.file: item for item in self.manifest.documents}
        if len(declarations) != len(self.manifest.documents):
            raise TenantDomainError(
                f"{self.root / MANIFEST_NAME} declares the same file more than once; the "
                "ACL a document ends up with would then depend on which entry was read last"
            )
        files = self._corpus_files()

        missing = sorted({path.name for path in files} - set(declarations))
        if missing:
            raise TenantDomainError(
                f"{self.corpus_dir} contains documents with no manifest entry: {missing}. "
                "An undeclared document is not an unrestricted one; indexing it while the "
                "audit has no coordinate for it would let the file be reported as correctly "
                "restricted by default, which is the one direction this must not fail in"
            )
        orphaned = sorted(set(declarations) - {path.name for path in files})
        if orphaned:
            raise TenantDomainError(
                f"{self.root / MANIFEST_NAME} declares documents that do not exist: "
                f"{orphaned}. The strata a report describes would then come from entries "
                "that are not the documents that were indexed"
            )

        default_tenant = self.manifest.default_tenant
        documents: list[KnowledgeDocument] = []
        for path in files:
            declaration = declarations[path.name]
            content = path.read_text(encoding="utf-8").strip()
            if not content:
                raise TenantDomainError(f"{path} is empty; an empty document is not a strata")
            documents.append(
                KnowledgeDocument(
                    title=declaration.title or _first_heading(content) or path.stem,
                    content=content,
                    document_type=declaration.document_type,
                    language="en",
                    metadata={"tenant_domain_fixture": True, "file": path.name},
                    acl=KnowledgeACL(
                        corpus_scope=CorpusScope.TENANT,
                        tenant_id=declaration.resolved_tenant(default_tenant),
                        entity_ids=declaration.entity_ids,
                        group_ids=declaration.group_ids,
                        effective_from=declaration.effective_from,
                        effective_to=declaration.effective_to,
                        is_active=declaration.is_active,
                    ),
                    provenance=KnowledgeProvenance(
                        # Each document is its own ``source``, for the same reason
                        # ``GoldCorpusSource`` gives: the per-source diversity ceiling in
                        # ``retrieve()`` exists to stop one large source crowding the
                        # context, not to flatten an eval corpus into its first few
                        # documents.
                        source=path.stem,
                        source_version=declaration.version,
                        source_uri=f"tenant-domain://{declaration.source_record_id}",
                        source_record_id=declaration.source_record_id,
                        license=declaration.license,
                        authority_level=declaration.authority_level,
                        synthetic=True,
                        content_hash=KnowledgeDocument.content_digest(content),
                    ),
                )
            )
        self._documents = documents
        return list(documents)

    def coordinates(self) -> dict[str, EvidenceCoordinates]:
        """The ACL ground truth for the §4.1 audit, read off the documents themselves.

        Not a second parse of the manifest: it is the ACL of the ``KnowledgeDocument`` that
        was handed to ``ingest``. A translation bug between declaration and document would
        otherwise be the one thing both halves of the audit agree on.
        """
        return {
            document.provenance.source_record_id: EvidenceCoordinates(
                tenant_id=document.acl.tenant_id,
                group_ids=document.acl.group_ids,
                is_active=document.acl.is_active,
                effective_from=document.acl.effective_from,
                effective_to=document.acl.effective_to,
                authority_level=document.provenance.authority_level,
            )
            for document in self.documents()
        }

    def gold_set(self) -> GoldSet:
        """The judgments, after checking every one of them is about this corpus."""
        path = self.root / QRELS_NAME
        gold = GoldSet.model_validate(json.loads(path.read_text(encoding="utf-8")))
        known = {document.provenance.source_record_id for document in self.documents()}

        referenced: set[str] = set()
        for query in gold.queries:
            referenced |= set(query.relevant)
            referenced |= {item.source_record_id for item in query.graded}
            referenced |= {
                support.parent_chunk_id for nugget in query.nuggets for support in nugget.supporting
            }
        unknown = sorted(key for key in referenced if key and key.split("#", 1)[0] not in known)
        if unknown:
            raise TenantDomainError(
                f"{path} judges documents this corpus does not contain: {unknown}. Recall "
                "over a key that was never indexed measures a join between two files and "
                "reports the result as a retrieval miss"
            )

        for query in gold.queries:
            for item in query.graded:
                if item.parent_chunk_id not in {
                    item.source_record_id,
                    f"{item.source_record_id}{DOCUMENT_LEVEL}",
                }:
                    raise TenantDomainError(
                        f"query {query.id!r} judges parent chunk {item.parent_chunk_id!r}, "
                        "which is not a chunk id this fixture can know: chunk ids are minted "
                        "by the chunker at index time. A document-level judgment is written "
                        f"as {item.source_record_id!r}{DOCUMENT_LEVEL}; chunk-level judging "
                        "needs the ingested chunk ids and belongs to the step that reads them"
                    )
        return gold

    def strata(self) -> dict[str, int]:
        """How much wrongness this corpus could exhibit, so a zero can be read for what it is.

        Reported beside the §4.1 count for the same reason the production script reports it:
        a zero over a single-tenant corpus with no group-restricted documents is a property
        of the corpus, not a finding about the pipeline. These numbers are what say whether
        the count had a chance to be anything else, and they are what makes this fixture's
        zero a finding rather than a shape.
        """
        documents = self.documents()
        return {
            "documents": len(documents),
            "tenants": len({document.acl.tenant_id for document in documents}),
            "group_restricted": sum(1 for document in documents if document.acl.group_ids),
            "inactive": sum(1 for document in documents if not document.acl.is_active),
            "with_an_effective_end": sum(
                1 for document in documents if document.acl.effective_to is not None
            ),
            "not_yet_in_force": sum(
                1 for document in documents if document.acl.effective_from > self.query_time
            ),
        }


def _first_heading(markdown: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def load_fixture(
    root: Path,
) -> tuple[list[KnowledgeDocument], GoldSet, dict[str, EvidenceCoordinates]]:
    """Everything a run needs, from one directory, parsed once.

    Returned together because the three are only meaningful as a matched set: documents
    indexed from one revision, coordinates from a second and judgments from a third would
    each be internally consistent and jointly meaningless.
    """
    fixture = TenantDomainFixture(root)
    return fixture.documents(), fixture.gold_set(), fixture.coordinates()


__all__ = [
    "AUTHORITY_LEVELS",
    "DOCUMENT_LEVEL",
    "FIXTURE_QUERY_TIME",
    "MANIFEST_NAME",
    "QRELS_NAME",
    "TENANTS",
    "DocumentDeclaration",
    "FixtureManifest",
    "TenantDomainError",
    "TenantDomainFixture",
    "load_fixture",
]
