"""The Phase 7.6.6 business quality set: 200 pre-registered questions and what answers each.

The acceptance set answers "did a real user's run come out right" on a handful of deep
journeys. This one answers a different question at a different resolution: "across a real
corpus, how often is the platform right", where right is one of four things and the split
between them is the result.

**Every case declares its expected outcome before the run, and the classes are exclusive.**
A case is answerable, or its evidence is insufficient, or its question is answered only by
a superseded document, or the document that answers it is one the asker must not reach.
There is no "any of these is fine" class: a set where everything passes is a set that
measures nothing, and the four classes exist because the failure modes are opposite --
fabricating an answer, refusing a real one, citing a retired procedure, leaking a
restricted one. A single number that counted all four the same way would hide which of them
the platform is bad at.

The composition is fixed, not a floor (``REQUIRED_COMPOSITION``): the requirement this set
exists to satisfy is a specific 120/40/20/20, and a set that shipped with 200 cases in some
other proportion would clear a "at least 200" check while being about something else.

**Measuring answerability honestly.** The end-to-end answerable rate this module exists to
produce is ``answered / answerable`` over the 120, where *answered* means the run reached a
terminal success, the reviewer passed it, and the citations resolve to the document that
was declared in advance to answer the question. It replaces the ``0.275`` figure that
``scripts/audit_rag_quality_state.py`` documents as a retrieval top-score threshold proxy
-- a number computed from retrieval scores with no model, no reviewer and no answer in it.
The two are not the same measurement and the proxy is not a lower bound on this one; see
that script's docstring and ``answerability_signal.is_end_to_end_reviewer_measurement``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from servicemind.domain.evidence import CITATION_KEY, evidence_items
from servicemind.domain.knowledge import Citation

QUALITY_SCHEMA_VERSION = "phase7-quality-v1"

#: The classes a case may be in, and how many of each the set holds. Exact, not a floor.
REQUIRED_COMPOSITION: dict[str, int] = {
    "answerable": 120,
    "insufficient-evidence": 40,
    "version-conflict": 20,
    "must-refuse-access": 20,
}


class CaseKind(StrEnum):
    ANSWERABLE = "answerable"
    INSUFFICIENT_EVIDENCE = "insufficient-evidence"
    VERSION_CONFLICT = "version-conflict"
    MUST_REFUSE_ACCESS = "must-refuse-access"


class QualityCase(BaseModel):
    """One question, its class, and the documents that decide it.

    ``expected_citations`` and ``forbidden_citations`` are source record ids, not titles or
    paths: they are what the platform's own ``Citation`` model carries, so the assertion is
    against the platform's own vocabulary rather than against a string this file invented.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=64)
    kind: CaseKind
    #: The username the run is submitted as. Which groups that identity holds is what
    #: separates a must-refuse-access case from an insufficient-evidence one.
    subject: str = Field(min_length=1, max_length=120)
    ticket_ref: str = Field(min_length=1, max_length=120)
    question: str = Field(min_length=1, max_length=2000)
    expected_citations: tuple[str, ...] = ()
    forbidden_citations: tuple[str, ...] = ()
    note: str = ""

    @model_validator(mode="after")
    def _the_declaration_matches_the_class(self) -> QualityCase:
        """Each class has exactly one shape of declaration.

        Written as a table of what each class must and must not carry, because the failure
        this prevents is a case filed under the wrong class: a ``must-refuse-access`` row
        with no forbidden document asserts nothing about access, and it would be graded
        PASS by any grader that read the class off the row rather than the row off the class.
        """
        match self.kind:
            case CaseKind.ANSWERABLE:
                if not self.expected_citations:
                    raise ValueError("an answerable case must name the document that answers it")
            case CaseKind.INSUFFICIENT_EVIDENCE:
                if self.expected_citations or self.forbidden_citations:
                    raise ValueError(
                        "an insufficient-evidence case names no document: it is about the "
                        "corpus having nothing, so naming one makes it a different class"
                    )
            case CaseKind.VERSION_CONFLICT:
                if not self.expected_citations or not self.forbidden_citations:
                    raise ValueError(
                        "a version-conflict case must name both the current document and the "
                        "superseded one; without the second it is just an answerable case"
                    )
            case CaseKind.MUST_REFUSE_ACCESS:
                if not self.forbidden_citations:
                    raise ValueError(
                        "a must-refuse-access case must name the document it must not reach"
                    )
                if self.expected_citations:
                    raise ValueError(
                        "a must-refuse-access case names no expected citation: the assertion "
                        "is that the unreachable document is never cited, and requiring a "
                        "different one would make it a retrieval-recall test"
                    )
        overlap = set(self.expected_citations) & set(self.forbidden_citations)
        if overlap:
            raise ValueError(f"a document cannot be both expected and forbidden: {sorted(overlap)}")
        return self


class QualityCaseSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    description: str
    tenant_id: UUID
    #: The end-to-end answerable rate the 120 must clear, fixed before the batch ran.
    #:
    #: Taken from the tenant reference thresholds the repository already uses for retrieval
    #: quality (0.85 Recall@5, ``docs/PHASE5_FINAL_ARCHITECTURE_AND_ACCEPTANCE.md``) rather
    #: than chosen once the result was known. It lives in the case list so that it is
    #: recorded before the observation it judges; a threshold written into the gate after a
    #: run is a threshold that was picked to be passed.
    answerable_rate_target: float = Field(gt=0, le=1)
    #: Where that number came from. Required text rather than a comment: a threshold whose
    #: provenance is not written down is indistinguishable from one that was back-filled
    #: once the batch had been run.
    answerable_rate_target_source: str = Field(min_length=1)
    cases: tuple[QualityCase, ...]

    @model_validator(mode="after")
    def _the_set_is_the_size_and_shape_it_claims(self) -> QualityCaseSet:
        if self.schema_version != QUALITY_SCHEMA_VERSION:
            raise ValueError(
                f"schema_version must be {QUALITY_SCHEMA_VERSION!r}, got {self.schema_version!r}"
            )
        ids = [case.id for case in self.cases]
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        if duplicates:
            raise ValueError(f"duplicate case ids: {duplicates}")
        actual: dict[str, int] = {}
        for case in self.cases:
            actual[case.kind.value] = actual.get(case.kind.value, 0) + 1
        if actual != REQUIRED_COMPOSITION:
            raise ValueError(
                f"the set is {actual}, the frozen composition is {REQUIRED_COMPOSITION}; a "
                "set with the same total in another proportion is a different measurement"
            )
        return self

    def counts(self) -> dict[str, int]:
        return dict(REQUIRED_COMPOSITION)

    def subject_usernames(self) -> list[str]:
        seen: list[str] = []
        for case in self.cases:
            if case.subject not in seen:
                seen.append(case.subject)
        return seen

    def ticket_refs(self) -> list[str]:
        seen: list[str] = []
        for case in self.cases:
            if case.ticket_ref not in seen:
                seen.append(case.ticket_ref)
        return seen

    def corpus_record_ids(self) -> list[str]:
        """Every source record id the set refers to, in first-seen order.

        The seeder checks the indexed corpus against this: a case naming a document that
        was never ingested is a case whose failure would be reported as a retrieval miss.
        """
        seen: list[str] = []
        for case in self.cases:
            for record_id in (*case.expected_citations, *case.forbidden_citations):
                if record_id not in seen:
                    seen.append(record_id)
        return seen


def load_quality_cases(path: Path) -> QualityCaseSet:
    return QualityCaseSet.model_validate(json.loads(path.read_text(encoding="utf-8")))


#: The two things a graded quality run is read for, read in one place because two readers
#: of the same run disagreeing is the defect, not the tolerance. Both take the run's
#: persisted ``result`` object.


class CitationReading(BaseModel):
    """What one run's evidence says about which documents it cited, and how well it said it.

    ``unreadable`` is the part that has to travel with the ids rather than be dropped. A
    reader that skipped malformed rows and returned only the ids would make ``cited the
    wrong document`` and ``cited a row nobody can parse`` look the same from the outside --
    and the second one would look like a clean run that happened to cite nothing.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Source record ids, first-seen order, deduplicated.
    source_record_ids: tuple[str, ...] = ()
    #: Rows that carried a citation payload which would not validate.
    unreadable: int = 0
    #: Rows carrying no citation payload at all. Not a defect -- most evidence is not a
    #: citation -- but recorded so the two kinds of "not a citation" stay distinguishable.
    without_citation: int = 0


def read_citations(result: Mapping[str, Any] | None) -> CitationReading:
    """The one reader of a run's citations. Everything else in this module calls it.

    A row whose citation metadata will not validate is counted rather than raising: the
    runner records the count, and a reader that raised would turn one malformed row into a
    crashed batch.
    """
    seen: list[str] = []
    unreadable = 0
    without = 0
    for row in evidence_items((result or {}).get("evidence")):
        if not isinstance(row, Mapping):
            without += 1
            continue
        raw = row.get("metadata", {})
        payload = raw.get(CITATION_KEY) if isinstance(raw, Mapping) else None
        if not isinstance(payload, Mapping):
            without += 1
            continue
        try:
            citation = Citation.model_validate(payload)
        except Exception:  # noqa: BLE001 -- a malformed row is counted, not fatal
            unreadable += 1
            continue
        if citation.source_record_id and citation.source_record_id not in seen:
            seen.append(citation.source_record_id)
    return CitationReading(
        source_record_ids=tuple(seen), unreadable=unreadable, without_citation=without
    )


def citations_in(result: Mapping[str, Any] | None) -> list[str]:
    """Just the ids. Kept because most callers want nothing else; see ``read_citations``."""
    return list(read_citations(result).source_record_ids)


def reviewer_decision(result: Mapping[str, Any] | None) -> str | None:
    """The reviewer's decision, or None when the run did not reach one.

    ``None`` is not ``passed``: a run that never got as far as a review has not answered
    anything, and folding the two together is how "the run crashed" becomes "the reviewer
    was satisfied".
    """
    if not result:
        return None
    review = result.get("review")
    if not isinstance(review, Mapping):
        return None
    decision = review.get("decision")
    return str(decision) if decision is not None else None


class ReviewSignals(BaseModel):
    """What the run's own review says about claims the evidence does not carry.

    Read for the ``insufficient-evidence`` class, and read with an explicit account of what
    it can and cannot be.

    **What this is not.** It is not an independent judge. ``unsupported_claims`` is the
    platform's reviewer reporting on the platform's analysis, so a case graded on it says
    "the platform did not notice itself asserting something unsupported" -- which is weaker
    than the answerable class, where the check is the external one of whether the *expected*
    document was cited. The limitation is structural: for a question the corpus deliberately
    cannot answer there is no expected document to match against, so the fabrication has to
    be caught by reading the text, and the only machine-readable reading of that text is the
    one the platform publishes.

    It was written the obvious other way first -- ``FAIL when the reviewer passed`` -- and a
    smoke run over the seeded corpus falsified it. The reviewer's ``feedback`` for a case the
    platform *handled correctly* read: "The analysis correctly declines to invent an ordering
    procedure absent from the evidence." ``passed`` means the claims are supported by the
    evidence they cite, not that the question was answered, so that rule failed every run
    that refused properly.

    **Why the fields are counts and not the lists.** The finding text is model-authored prose
    and re-reading it here would put a second, unreviewed reading of the same text in front of
    the verdict. ``_authored_rows`` returns -1 rather than 0 when the field is absent or not a
    list, so a changed response shape reads as "not measured" instead of as "nothing found" --
    the difference between a signal and a silence.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Claims the reviewer recorded as not backed by their citations. Fabrication's footprint.
    unsupported_claims: int = 0
    #: Evidence the analysis needed and the corpus did not supply.
    missing_evidence: int = 0
    #: Actions the analysis proposed. A run that concludes "the corpus does not cover this"
    #: proposes none; one that invented a procedure tends to propose the step.
    proposed_actions: int = 0
    #: ``review.findings``. Non-empty is the reviewer saying something was wrong.
    findings: int = 0


def _authored_rows(container: object, key: str) -> int:
    """How many rows a model-authored list holds, or -1 when it is not there to count."""
    if not isinstance(container, Mapping):
        return -1
    value = container.get(key)
    return len(value) if isinstance(value, list) else -1


def read_review_signals(result: Mapping[str, Any] | None) -> ReviewSignals:
    """The one reader of a run's fabrication signals. See ``ReviewSignals``."""
    result = result or {}
    review = result.get("review")
    analysis = result.get("analysis")
    return ReviewSignals(
        unsupported_claims=_authored_rows(review, "unsupported_claims"),
        missing_evidence=_authored_rows(review, "missing_evidence"),
        proposed_actions=_authored_rows(analysis, "proposed_actions"),
        findings=_authored_rows(review, "findings"),
    )


def _canonical(value: object) -> object:
    """Collapse unordered containers to a hash-order-free rendering; see ``acceptance``."""
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(
            (_canonical(item) for item in value),
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"), default=str),
        )
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def _digest(value: object) -> str:
    payload = json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def case_set_digest(case_set: QualityCaseSet) -> str:
    return _digest(case_set.model_dump(mode="python"))


def observation_digest(payload: object) -> str:
    return _digest(payload)
