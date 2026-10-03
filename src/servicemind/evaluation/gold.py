from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
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

#: Which grade a judged item must reach to count as answering the query, for the *binary*
#: metrics. ``relevant`` has always meant "the documents that answer the query", so this
#: is the graded scale's point where a document starts to answer one: 1 is background, 2
#: "supports part of the answer", 3/4 the whole of it. Named rather than inlined because
#: every binary number in every Phase 4 report moves with it.
ANSWERING_GRADE = 2

#: What §3.3 of the evaluation baseline calls the judgment scale.
MIN_GRADE, MAX_GRADE = 0, 4


class QueryCategory(StrEnum):
    """The four strata §3.2 freezes the release set over.

    Recorded per query rather than counted from a file because the strata are what the
    metrics are *reported by*: a recall figure averaged over the corpus says nothing about
    whether the hard-negative stratum collapsed, and the hard negatives are the ones the
    reranker exists to fix.
    """

    SINGLE_EVIDENCE = "single_evidence"
    MULTI_EVIDENCE = "multi_evidence"
    HARD_NEGATIVE = "hard_negative"
    UNANSWERABLE = "unanswerable"


class RefusalReason(StrEnum):
    """Why an unanswerable query must not be answered.

    The three are not interchangeable and are not one "impossible" bucket. A query whose
    evidence is merely absent is answered by retrieving more; one that turns on a version
    conflict is answered by saying which version is in force; one that asks for something
    the caller has no right to see must be refused *without* disclosing that the material
    exists. Scoring them as a single abstention rate hides which of the three the agent
    got wrong.
    """

    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    VERSION_CONFLICT = "version_conflict"
    MUST_REFUSE_ACCESS = "must_refuse_access"


class Split(StrEnum):
    """§3.2's development/test partition: 120 tune, 280 hide."""

    DEVELOPMENT = "development"
    TEST = "test"


class LabelTier(StrEnum):
    """How much independent human judgment stands behind a set's labels.

    The distinction the tenant release gate turns on. ``SILVER`` is a label a source
    provided or a model proposed; neither is an independent judgment, and no threshold
    applied to such a set is a release verdict. ``SINGLE_ANNOTATOR`` and
    ``DOUBLE_ANNOTATOR`` name §3.3's protocol: one domain expert annotates everything, a
    second re-annotates a stratified 20% and every ambiguous item, and the agreement
    between them is reported as Cohen's kappa.
    """

    SILVER = "silver"
    SINGLE_ANNOTATOR = "single_annotator"
    DOUBLE_ANNOTATOR = "double_annotator"


#: Tier strings that already exist on disk under names this enum does not use. The
#: committed proxy selection records ``external_silver_no_tenant_human_signoff``, and it
#: is written by a run that needs the TechQA dataset, so its vocabulary cannot be renamed
#: retroactively the way a code symbol can -- it can only be translated at the boundary.
#: Mapping rather than a second enum so that every reader arrives at the same ``LabelTier``
#: and no consumer can compare one vocabulary against the other by accident.
RECORDED_LABEL_TIERS = {
    "external_silver_no_tenant_human_signoff": LabelTier.SILVER,
}


def label_tier_from_recorded(recorded: str) -> LabelTier:
    """Read a tier as some file recorded it, refusing anything unrecognised.

    The refusal is the point. An unknown tier string is a set whose provenance nobody has
    taught this code about, and defaulting it to ``SILVER`` (the conservative-looking
    choice) would be indistinguishable from having actually read a silver label; raising
    keeps "we have not seen this" from rendering as "we have judged this".
    """
    if recorded in RECORDED_LABEL_TIERS:
        return RECORDED_LABEL_TIERS[recorded]
    try:
        return LabelTier(recorded)
    except ValueError as error:
        raise ValueError(
            f"unrecognised label tier {recorded!r}; add it to RECORDED_LABEL_TIERS with the "
            f"judgment protocol it stands for, or to LabelTier if it is a new protocol"
        ) from error


class AnnotationProvenance(BaseModel):
    """Who judged the set, and how much their judgments agreed.

    Carried on the set because it is the input to whether the §4.1 thresholds may be
    applied at all. It is deliberately *data* rather than a flag on a harness: the
    harness that produced the TechQA proxy report hardcodes ``applicable: false``, which
    is the right default for a public silver set and the wrong mechanism for a tenant set
    -- a boolean in the code can only be flipped by editing the code, so it could never
    report the difference between "we have no human qrels" and "we have them".
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    label_tier: LabelTier
    guideline_version: str = ""
    annotator_count: int = Field(default=0, ge=0)
    #: §3.3 step 5 reports Cohen's kappa over the double-annotated sample; below 0.80 the
    #: guideline is revised and the disputed categories re-annotated, so a set that does
    #: not clear the bar is not a release set yet.
    cohen_kappa: float | None = Field(default=None, ge=-1.0, le=1.0)
    double_annotated_fraction: float | None = Field(default=None, ge=0.0, le=1.0)
    notes: str = ""

    @property
    def meets_tenant_protocol(self) -> bool:
        return (
            self.label_tier is LabelTier.DOUBLE_ANNOTATOR
            and self.annotator_count >= 2
            and self.cohen_kappa is not None
            and self.cohen_kappa >= 0.80
        )


class JudgedItem(BaseModel):
    """One 0--4 judgment, with the conditions under which it holds.

    §3.3 step 4 binds each judgment to a version, an authority level and a tenant/ACL
    condition. Those are not decoration: §4.1 carries a gate whose threshold is a *count*
    ("wrong tenant/ACL/expired-version items in visible evidence: 0"), and a count is only
    computable if each judgment says which tenant, group and version it was made under.
    A binary ``relevant`` list cannot express that a document is the right answer for one
    caller and a disclosure for another.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: §3.4 upgrades the judgment from a document to a ``parent_chunk_id``: a document can
    #: contain both the answering passage and an obsolete one, and document-level
    #: judgments cannot say which was retrieved.
    parent_chunk_id: str = Field(min_length=1)
    grade: int = Field(ge=MIN_GRADE, le=MAX_GRADE)
    source_record_id: str = ""
    #: The span that carries the judgment, for citation-support scoring.
    span: str = ""

    #: Conditions. ``None`` means the judgment was made without restricting on that axis,
    #: which is not the same as "unrestricted": see ``applies_to``.
    tenant_id: UUID | None = None
    group_ids: frozenset[int] = frozenset()
    entity_ids: frozenset[int] = frozenset()
    effective_version: str | None = None
    authority_level: AuthorityLevel | None = None

    @property
    def answers(self) -> bool:
        return self.grade >= ANSWERING_GRADE

    def applies_to(
        self,
        *,
        tenant_id: UUID | None = None,
        group_ids: frozenset[int] = frozenset(),
    ) -> bool:
        """Whether this judgment was made under a caller's tenant and groups.

        A judgment recorded without a tenant is a judgment about the corpus, not about a
        caller, and applies to everyone; a judgment recorded *with* one applies only to
        that tenant. Group-restricted judgments require an intersecting group, and an
        empty group set is "no group access", never "unrestricted" -- the same reading
        ``KnowledgeACL.allows`` uses, and the one the Phase 7 permission work had to
        establish for the retrieval path.
        """
        if self.tenant_id is not None and self.tenant_id != tenant_id:
            return False
        if not self.group_ids:
            return True
        return bool(self.group_ids & group_ids)


class NuggetSupport(BaseModel):
    """Where one required fact is stated, and under what conditions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    parent_chunk_id: str = Field(min_length=1)
    span: str = ""
    effective_version: str | None = None
    authority_level: AuthorityLevel | None = None
    tenant_id: UUID | None = None
    group_ids: frozenset[int] = frozenset()


class Nugget(BaseModel):
    """A fact the answer must cover, per §3.3 step 4.

    Retrieval metrics score *whether the right passage was ranked*; nuggets score whether
    the passage that was ranked actually says what the answer needed to say. An answer can
    cite the correct document and still miss the fact, which is invisible to Recall@k and
    is the whole reason §3.4 asks for nugget completeness beside it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    weight: float = Field(default=1.0, gt=0.0)
    supporting: list[NuggetSupport] = Field(default_factory=list)
    #: True when the fact must appear for the answer to be correct at all, as opposed to
    #: adding detail. Weighted coverage cannot distinguish "missed the point" from "missed
    #: a detail", so the distinction is recorded rather than folded into the weight.
    required: bool = True


class GoldQuery(BaseModel):
    """One retrieval probe with its human-verified relevance set.

    ``relevant`` holds the ``source_record_id`` values of the documents that answer
    the query (empty for the unanswerable subset). ``unanswerable`` marks queries the
    agent should decline rather than fabricate an answer for.

    The fields below it are the §3.4 upgrade and are all optional, so every set written
    before this extension stays valid and every consumer that only reads ``relevant``
    keeps working. ``graded`` supersedes ``relevant`` where it is present: judging at the
    ``parent_chunk_id`` level with a 0--4 scale is what makes NDCG@10 and the citation
    checks meaningful, and ``relevant`` is derivable from it (``grade >= ANSWERING_GRADE``)
    while the converse is not.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=100)
    query: str = Field(min_length=1, max_length=4000)
    relevant: list[str] = Field(default_factory=list)
    unanswerable: bool = False
    intent: RetrievalIntent = RetrievalIntent.GENERAL_KNOWLEDGE
    language: str = Field(default="en", min_length=2, max_length=20)

    #: §3.2's four strata. Defaulted to the single-evidence case so a pre-extension set
    #: reads as what it was: a set of ordinary answerable queries with no hard negatives.
    category: QueryCategory = QueryCategory.SINGLE_EVIDENCE
    #: Required when ``unanswerable``; refused in the same validator that enforces it, so
    #: "we do not know why this is unanswerable" cannot be recorded.
    refusal_reason: RefusalReason | None = None
    split: Split = Split.TEST

    graded: list[JudgedItem] = Field(default_factory=list)
    nuggets: list[Nugget] = Field(default_factory=list)

    #: The caller this query is asked as. Recorded per query because §3.2 asks for a
    #: "wrong tenant" stratum and §4.1 for a zero-count on wrong-ACL evidence: the same
    #: query text is answerable for one caller and must be refused for another, so the
    #: caller is part of the case, not a property of the run.
    asker_tenant_id: UUID | None = None
    asker_group_ids: frozenset[int] = frozenset()
    asker_entity_ids: frozenset[int] = frozenset()

    @model_validator(mode="after")
    def relevant_implies_answerable(self) -> Self:
        if self.unanswerable and self.relevant:
            raise ValueError("an unanswerable query cannot carry relevant documents")
        return self

    @model_validator(mode="after")
    def the_refusal_reason_matches_the_verdict(self) -> Self:
        if self.unanswerable and self.refusal_reason is None:
            raise ValueError(
                "an unanswerable query must say which of insufficient_evidence, "
                "version_conflict or must_refuse_access it is: they are scored separately "
                "because they fail separately"
            )
        if not self.unanswerable and self.refusal_reason is not None:
            raise ValueError("an answerable query cannot carry a refusal reason")
        if self.unanswerable and self.category is not QueryCategory.UNANSWERABLE:
            raise ValueError("an unanswerable query belongs to the unanswerable stratum")
        return self

    @model_validator(mode="after")
    def the_binary_view_agrees_with_the_graded_one(self) -> Self:
        """``relevant`` and ``graded`` must not disagree about the same document.

        Both fields are reachable by different consumers, so a set that carried a document
        in one and not the other would score differently depending on which metric ran --
        silently, and in the direction that flatters whichever consumer was updated last.
        """
        if not self.graded or not self.relevant:
            return self
        answering = {
            item.source_record_id or item.parent_chunk_id for item in self.graded if item.answers
        }
        if answering != set(self.relevant):
            raise ValueError(
                f"query {self.id!r} lists relevant documents that the graded judgments do "
                f"not support: only in relevant={sorted(set(self.relevant) - answering)}, "
                f"only in graded={sorted(answering - set(self.relevant))}"
            )
        return self

    @property
    def grades(self) -> dict[str, int]:
        """``parent_chunk_id -> grade``, the form every graded metric wants."""
        return {item.parent_chunk_id: item.grade for item in self.graded}

    @property
    def required_nuggets(self) -> list[Nugget]:
        return [nugget for nugget in self.nuggets if nugget.required]


@dataclass(frozen=True)
class ReleaseSetShape:
    """What the §4.1 applicability check needs, separate from the set that carries it.

    A set and its *shape* are split because the sets that need judging are not all
    ``GoldSet``: the TechQA proxy run reads a selection manifest of query ids and
    filenames, which has no query text, no categories and no grades, and yet is the set
    the currently committed numbers come from. Giving that manifest a ``GoldSet`` it does
    not have would be a lie about what was recorded; giving it a shape it can state
    truthfully -- this many queries, none graded, no hard negatives -- is not.

    Every default is the *absent* value, so a container that cannot express a stratum
    reports it missing. That is the honest reading: a binary relevance file does not
    contain a hard-negative stratum, it does not merely fail to mention one.
    """

    provenance: AnnotationProvenance | None = None
    graded_queries: int = 0
    hard_negative_queries: int = 0
    refusal_reasons: frozenset[RefusalReason] = frozenset()
    development_queries: int = 0


def release_gate_blockers(shape: ReleaseSetShape) -> list[str]:
    """Why the §4.1 release thresholds may not be applied to a set with this shape.

    Empty means they may. §3 scopes the closure gate to a tenant-domain release set --
    private tenant queries, expert-signed qrels, ACL and tenant strata, a tune/hold-out
    partition -- and §6.4 makes every one of those a precondition rather than a
    preference. The list below is that sentence turned into a computation.

    It is a function rather than a flag because a flag can only report what someone typed
    into the code: the harness that produced the TechQA report hardcodes
    ``applicable: false``, which is the right answer for a public silver set and the wrong
    mechanism, since it gives the same answer for a set that would qualify. Here the answer
    is whatever the recorded data supports, so a set that clears every clause stops being
    blocked without anyone editing a boolean -- and a set that does not cannot be reported
    as if it did.
    """
    blockers: list[str] = []
    if shape.provenance is None:
        blockers.append(
            "the set records no annotation provenance, so nothing says whether its labels "
            "are an independent human judgment"
        )
    elif not shape.provenance.meets_tenant_protocol:
        blockers.append(
            f"labels are tier {shape.provenance.label_tier.value} with "
            f"{shape.provenance.annotator_count} annotator(s) and kappa "
            f"{shape.provenance.cohen_kappa}; §3.3 requires two domain annotators over a "
            "stratified 20% sample with kappa >= 0.80"
        )
    if not shape.graded_queries:
        blockers.append(
            "no query carries 0--4 judgments, so the set can only be scored by the binary "
            "metrics and cannot express the §4.1 wrong-ACL/expired-version count"
        )
    if not shape.hard_negative_queries:
        blockers.append("the set has no hard-negative stratum (§3.2 asks for 80)")
    for reason in RefusalReason:
        if reason not in shape.refusal_reasons:
            blockers.append(
                f"the set has no must-refuse query of kind {reason.value}; a single "
                "abstention rate cannot tell the three apart"
            )
    if not shape.development_queries:
        blockers.append(
            "the set has no development split, so nothing could have been tuned without "
            "touching the queries that are supposed to stay hidden"
        )
    return blockers


class GoldSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "phase4-gold-v1"
    name: str
    description: str = ""
    #: source_record_id -> short title, so reports render readable names.
    documents: dict[str, str] = Field(default_factory=dict)
    queries: list[GoldQuery] = Field(default_factory=list)

    #: Absent for a set that predates the extension, and absent for a set whose labels
    #: nobody signed. Both read as "not a release set", which is the safe direction: the
    #: only thing this field can do is unlock the §4.1 thresholds, so failing closed is
    #: the whole point of it being optional.
    provenance: AnnotationProvenance | None = None

    @property
    def answerable(self) -> list[GoldQuery]:
        return [query for query in self.queries if not query.unanswerable]

    @property
    def unanswerable(self) -> list[GoldQuery]:
        return [query for query in self.queries if query.unanswerable]

    def by_category(self, category: QueryCategory) -> list[GoldQuery]:
        return [query for query in self.queries if query.category is category]

    def by_split(self, split: Split) -> list[GoldQuery]:
        return [query for query in self.queries if query.split is split]

    @property
    def release_set_shape(self) -> ReleaseSetShape:
        return ReleaseSetShape(
            provenance=self.provenance,
            graded_queries=len(self.graded_queries),
            hard_negative_queries=len(self.by_category(QueryCategory.HARD_NEGATIVE)),
            refusal_reasons=frozenset(
                query.refusal_reason for query in self.unanswerable if query.refusal_reason
            ),
            development_queries=len(self.by_split(Split.DEVELOPMENT)),
        )

    @property
    def release_gate_blockers(self) -> list[str]:
        """Why the §4.1 release thresholds may not be applied to this set.

        Empty means they may. See ``release_gate_blockers`` for the check itself.
        """
        return release_gate_blockers(self.release_set_shape)

    @property
    def graded_queries(self) -> list[GoldQuery]:
        return [query for query in self.queries if query.graded]


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
