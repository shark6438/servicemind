"""The Phase 7 acceptance case list: what each case asserts, and what it observes.

Everything in this module is data. ``acceptance_grader`` turns a case and an execution
into verdicts, and it does no I/O -- so a case list plus a recorded execution is a
complete, replayable description of a run's outcome, and CI can re-derive every verdict
without a stack. What the driver does is *record*; what this module does is *state*; what
the grader does is *judge*. Keeping the three apart is what makes "the report says PASS"
traceable to an assertion someone can re-run.

Two rules are load-bearing enough to state here rather than leave to the grader:

**An expectation is written so that it can fail.** ``expected_review_decision`` names one
decision from the platform's own enum, not "any of these is fine": a case whose outcome
is allowed to be anything has no discriminative power and would report PASS for a
platform that abstains on every incident.

**A prerequisite is per assertion, not per case.** "This case needs the entitlement
verifier" is the wrong granularity, because a case's steps do not share one. An approval
hash conflict is refused before the verifier is consulted; a rejection never writes
anything and so never needs the requester's authority re-established. Marking those
prerequisites at case level would report a genuine pass as BLOCKED, and -- worse -- would
let a verifier outage hide the step that really does depend on it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from servicemind.domain.analysis import AnalysisResult
from servicemind.domain.evidence import Evidence
from servicemind.domain.knowledge import Citation
from servicemind.domain.review import ReviewDecision, ReviewResult
from servicemind.persistence.models import ActionStatus, RunStatus

ACCEPTANCE_SCHEMA_VERSION = "phase7-acceptance-v1"


# --------------------------------------------------------------------------- identity


class ObservedSubject(BaseModel):
    """The identity a case actually ran as, as the token described it.

    Recorded rather than assumed from the case declaration: the declaration says which
    subject *should* be used, and a driver that silently authenticated as somebody else
    would otherwise produce a pass that means nothing. Captured from the decoded token,
    so what is recorded is what the platform was told, not what the case list intended.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    username: str
    tenant_id: UUID
    roles: frozenset[str] = Field(default_factory=frozenset)
    entity_ids: frozenset[int] = Field(default_factory=frozenset)
    group_ids: frozenset[int] = Field(default_factory=frozenset)


# ------------------------------------------------------------------- case declaration


class RequiredFact(BaseModel):
    """One fact the incident is made of, and the phrasings that count as stating it.

    Matched against the analysis's own claims rather than against the whole response, so
    a fact that appears only in the prose summary is not counted as grounded. The
    patterns are authored per case and reviewed like any other fixture: a fact whose
    patterns are too loose makes the case pass on any answer, and one whose patterns are
    too tight makes it fail on a correct answer, and neither is visible without reading
    them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=500)
    #: Any one of these regular expressions matching a claim statement satisfies the fact.
    patterns: list[str] = Field(default_factory=list)
    #: Concept groups the claim must *all* mention, in any order. A group is satisfied
    #: when any of its alternatives matches, and the fact when every group does.
    #:
    #: ``patterns`` tests a phrasing; this tests a proposition. The two languages put the
    #: same fact in opposite word orders -- "the user replaced their handset" and "the
    #: handset was replaced" -- and every run of a model invents a third, so a table of
    #: phrasings is always one sample behind the thing it is judging. Splitting the fact
    #: into what it is *about* and what it *says* about that survives rewording, and
    #: still fails a claim with the opposite polarity: "MFA succeeded" satisfies the
    #: subject group and no alternative in the outcome group.
    all_of: list[list[str]] = Field(default_factory=list)
    #: Claim types that may carry it. Empty means "any claim type".
    claim_types: list[str] = Field(default_factory=list)
    #: Source record ids that no claim stating this fact may cite.
    #:
    #: The negative half of a fact, and the reason ACC-03 is a case rather than a
    #: paragraph: "the agent identified the right root cause" is satisfied by an agent
    #: that grounds it in the wrong document. A fact that merely *appears* proves nothing
    #: about what the claim was built from, so what the claim cites is asserted here,
    #: resolved through the evidence rows to their source records.
    must_not_cite: list[str] = Field(default_factory=list)
    #: Source record ids at least one claim stating this fact must cite, taken as a union
    #: over those claims.
    #:
    #: The positive half, and it is not the same assertion as ``must_not_cite`` with the
    #: sign flipped. "Grounds it in the wrong document" and "grounds it in no document"
    #: are different faults, and a case holding only the negative one is satisfied by an
    #: analysis that says nothing at all: every claim it never made cites nothing, so the
    #: forbidden id is never cited. ACC-03 measured exactly that gap -- the run whose
    #: root-cause claim named the right cause could pass while citing nothing, because
    #: the only thing the case checked was which document it did not lean on.
    #:
    #: A union rather than a per-claim requirement: an analysis is free to split one
    #: condition across two claims that each cite part of the material, and demanding
    #: every id inside one sentence would fail a correct answer for being parsed into two.
    must_cite: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _has_a_matcher(self) -> RequiredFact:
        """A fact with nothing to match on would silently never be satisfied.

        ``patterns`` and ``all_of`` are both optional so that either can carry a fact
        alone; requiring one of them is what keeps that from becoming a fact that can
        only fail.
        """
        if not self.patterns and not self.all_of:
            raise ValueError(f"required fact {self.id!r} has neither patterns nor all_of")
        if any(not group for group in self.all_of):
            raise ValueError(f"required fact {self.id!r} has an empty all_of group")
        return self


class BannedPhrase(BaseModel):
    """A recommendation the agent must not make, expressed as concept groups.

    A violation is a clause in which *every* group matches at least one alternative. The
    clause form matters: the corpus's own remedy says "do not disable multi-factor
    authentication", and a substring ban on "disable ... mfa" would flag the correct
    answer as the violation. Splitting into clauses and dropping negated ones is what
    makes the check read the sentence rather than the letters.

    This is a lexical check and it is honest about it: a recommendation that avoids every
    listed alternative phrase is not caught here, and the report says so rather than
    implying the field was understood.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=500)
    #: Outer list = concepts that must all appear; inner list = alternatives, any one.
    groups: list[list[str]] = Field(min_length=1)


#: Words that invert the clause they appear in. Deliberately only true grammatical
#: negation: "avoid" and "instead of" are *not* here, because "avoid the challenge" is a
#: violation phrased as advice, and treating it as a negation would hide exactly the
#: recommendation this check exists to catch. The cost is a false positive on "avoid
#: disabling ...", which is recorded as a limitation instead of being papered over.
NEGATORS = (
    "not",
    "never",
    "no longer",
    "without",
    "cannot",
    "can't",
    "won't",
    "refuse",
    "refuses",
    "refused",
    "prohibited",
    "forbidden",
    "must not",
    "may not",
    "should not",
    "do not",
    "don't",
    "does not",
    "doesn't",
    "is not",
    "isn't",
    "are not",
    "aren't",
)

#: Fields whose structured content the ban applies to, and how each is reached.
RECOMMENDATION_TEXT_FIELDS = ("problem_recommendation", "change_recommendation")


class SubjectScopeExpectation(BaseModel):
    """What the identity the case ran as actually carried.

    The group-isolation cases need this and cannot be read off their citation lists
    alone. "The group-3 analyst did not see the group-4 document" is *also* true of a
    subject whose groups never left the token: an empty group set intersects nothing, so
    every restricted document is invisible and the exclusion passes for the wrong
    reason. Only the positive control -- the group-3 analyst *seeing* the group-3
    document -- distinguishes them, and that is a claim about the identity as much as
    about the index, so the identity is asserted directly rather than inferred.

    A field left ``None`` is not asserted. An empty list is asserted, and means "held
    no group", which is a different statement from "we did not look".
    """

    kind: Literal["subject_scope"] = "subject_scope"
    group_ids: list[int] | None = None
    entity_ids: list[int] | None = None
    tenant_id: UUID | None = None
    roles_superset: list[str] = Field(default_factory=list)


class AssertionKind(StrEnum):
    HTTP_STATUS = "http_status"
    SUBJECT_SCOPE = "subject_scope"
    TERMINAL_STATUS = "terminal_status"
    RUN_LISTED_FOR_ITS_TENANT = "run_listed_for_its_tenant"
    RUN_INVISIBLE_TO_ANOTHER_TENANT = "run_invisible_to_another_tenant"
    NO_NEW_FOLLOWUPS = "no_new_followups"
    EXACTLY_ONE_NEW_FOLLOWUP = "exactly_one_new_followup"
    FOLLOWUP_BODY_IS_THE_APPROVED_CONTENT = "followup_body_is_the_approved_content"
    ACTION_INTENT_STATUS = "action_intent_status"
    ACTION_INTENT_UNCHANGED = "action_intent_unchanged"
    REQUIRED_FACTS = "required_facts"
    BANNED_RECOMMENDATIONS = "banned_recommendations"
    CITATIONS_INCLUDE = "citations_include"
    CITATIONS_EXCLUDE = "citations_exclude"
    NO_FOREIGN_CITATIONS = "no_foreign_citations"
    EVIDENCE_REFS_RESOLVABLE = "evidence_refs_resolvable"
    REVIEW_DECISION = "review_decision"
    CONTEXT_SELECTION = "context_selection"
    AUDIT_EVENT = "audit_event"
    AUDIT_EVENT_ABSENT = "audit_event_absent"
    TIMELINE_EVENT = "timeline_event"
    GRAPH_EVIDENCE_ISOLATION = "graph_evidence_isolation"
    MEMORY_RECORD = "memory_record"
    LATENCY_BUDGET = "latency_budget"
    PROBE_OUTCOME = "probe_outcome"


class HttpStatusExpectation(BaseModel):
    """One request's status code, from a named step.

    Named rather than "the last one", because the cases that turn on a status are the
    ones with several calls in a row -- a refusal followed by a retry, an approval
    followed by a duplicate -- and "the last status was 200" would pass a case whose
    refusal never happened.
    """

    kind: Literal["http_status"] = "http_status"
    step_id: str = Field(min_length=1, max_length=120)
    status: int = Field(ge=100, le=599)


class TerminalStatusExpectation(BaseModel):
    kind: Literal["terminal_status"] = "terminal_status"
    status: RunStatus


class RunListedForItsTenantExpectation(BaseModel):
    kind: Literal["run_listed_for_its_tenant"] = "run_listed_for_its_tenant"


class RunInvisibleToAnotherTenantExpectation(BaseModel):
    kind: Literal["run_invisible_to_another_tenant"] = "run_invisible_to_another_tenant"


class NoNewFollowupsExpectation(BaseModel):
    kind: Literal["no_new_followups"] = "no_new_followups"


class ExactlyOneNewFollowupExpectation(BaseModel):
    kind: Literal["exactly_one_new_followup"] = "exactly_one_new_followup"


class FollowupBodyExpectation(BaseModel):
    """The stored body must be the approved content, plus nothing but the marker.

    The platform appends ``\\n[ServiceMind run=... action=...]`` to what it writes, so
    "byte-identical to the approved content" would be false of a correct write. What is
    asserted instead is the property a truncation breaks: after the whitespace
    normalisation GLPI's rich-text round trip forces on both sides, the part before the
    marker must equal the approved content, and the marker must name this run and this
    action hash. A body cut short still contains the marker -- which is all the executor
    currently checks -- and does not contain the content, which is the whole point.
    """

    kind: Literal["followup_body_is_the_approved_content"] = "followup_body_is_the_approved_content"


class ActionIntentStatusExpectation(BaseModel):
    """The intent's status, at the end of the run or at a named step.

    ``recorded_at_step`` exists because ``approved`` is not reachable over HTTP. The
    approval transaction sets the status and then, in the same request, the run resumes
    and the executor advances it to ``executing`` and ``succeeded`` -- so every response
    a client can read shows the post-execution value, and an assertion on ``approved``
    written against the run as a whole would fail on a correct platform.

    Naming a step makes the earlier moment observable instead of unassertable: what is
    compared is the status the platform reported *then*, which is a fact about the run's
    history rather than about its end state.
    """

    kind: Literal["action_intent_status"] = "action_intent_status"
    status: ActionStatus
    #: The step whose response carried this status. ``None`` means the run as a whole.
    recorded_at_step: str | None = None


class ActionIntentUnchangedExpectation(BaseModel):
    """The intent that was proposed is still the intent on the run, byte for byte."""

    kind: Literal["action_intent_unchanged"] = "action_intent_unchanged"
    #: The step whose response carried the hash the intent must still match.
    recorded_at_step: str


class RequiredFactsExpectation(BaseModel):
    kind: Literal["required_facts"] = "required_facts"
    facts: list[RequiredFact] = Field(min_length=1)


class BannedRecommendationsExpectation(BaseModel):
    kind: Literal["banned_recommendations"] = "banned_recommendations"
    bans: list[BannedPhrase] = Field(min_length=1)


class CitationsIncludeExpectation(BaseModel):
    kind: Literal["citations_include"] = "citations_include"
    source_record_ids: list[str] = Field(min_length=1)


class CitationsExcludeExpectation(BaseModel):
    kind: Literal["citations_exclude"] = "citations_exclude"
    source_record_ids: list[str] = Field(min_length=1)


class NoForeignCitationsExpectation(BaseModel):
    kind: Literal["no_foreign_citations"] = "no_foreign_citations"
    source_record_ids: list[str] = Field(min_length=1)


class EvidenceRefsResolvableExpectation(BaseModel):
    """Every reference a conclusion rests on must name a row that is actually present.

    An unresolvable reference is the failure that looks most like success: the claim
    reads as grounded, cites something, and nothing downstream checks that the something
    exists. Whatever is checked here is the same list a reader would follow, so the
    assertion is about the trace, not about the answer.
    """

    kind: Literal["evidence_refs_resolvable"] = "evidence_refs_resolvable"
    include_claims: bool = True
    include_review: bool = True


class ReviewDecisionExpectation(BaseModel):
    kind: Literal["review_decision"] = "review_decision"
    decision: ReviewDecision
    #: When the expected decision is ``passed``, no finding may be error or critical.
    #: A pass with a blocking finding is a contradiction the reviewer emitted, and a
    #: case that accepted it would be asserting the decision field alone.
    forbid_blocking_findings: bool = False


class ContextSelectionExpectation(BaseModel):
    kind: Literal["context_selection"] = "context_selection"
    #: The dedicated over-budget case requires a pruned or rejected item with a reason;
    #: every other case only requires that something was selected and that the manifest
    #: is not empty, because demanding pruning from a small context would be a case that
    #: fails for being under budget.
    require_pruning_with_reason: bool = False
    #: Item ids that must appear with decision ``selected``. This is how a case asserts
    #: that a particular source reached an agent -- a skill's context item id, say --
    #: rather than inferring it from the answer, which can be right for the wrong reason.
    required_selected_item_ids: list[str] = Field(default_factory=list)


class AuditEventExpectation(BaseModel):
    kind: Literal["audit_event"] = "audit_event"
    event_type: str


class AuditEventAbsentExpectation(BaseModel):
    """An audit row that must *not* exist for this run.

    The negative twin of :class:`AuditEventExpectation`, and it is not the same assertion
    as "nothing was written". A run paused at the resume boundary leaves no followup for
    the same reason it leaves no approval row -- but they are two different claims about
    two different subsystems, and only one of them is about GLPI. Without this, the
    strongest statement a case can make about a refused approval is that the *ticket* is
    untouched, which stays true if the platform recorded a human's "yes" and quietly
    failed to act on it.
    """

    kind: Literal["audit_event_absent"] = "audit_event_absent"
    event_type: str


class TimelineEventExpectation(BaseModel):
    kind: Literal["timeline_event"] = "timeline_event"
    event_type: str


class GraphEvidenceIsolationExpectation(BaseModel):
    """A positive control, not an absence.

    "The foreign node was not returned" is satisfied by a side channel that returns
    nothing at all, which is the exact failure mode a broken graph retriever has. This
    requires the permitted node to be *found* by the same call that must not find the
    restricted one.
    """

    kind: Literal["graph_evidence_isolation"] = "graph_evidence_isolation"
    visible_source_record_id: str
    hidden_source_record_id: str
    #: A subject that holds the group the hidden node requires.
    #:
    #: The subject's own reading shows the restricted node missing, which is what isolation
    #: means -- but the same reading is produced by a graph in which that node was never
    #: projected, never linked to the anchor, or dropped by the hop and node bounds. A
    #: witness closes that gap from the other side: the same traversal, under a principal
    #: that *may* see the node, has to return it. Absence under one principal plus presence
    #: under another is filtering; absence alone is a claim about a graph nobody inspected.
    witness_subject: str | None = Field(default=None, min_length=1, max_length=100)


class MemoryRecordExpectation(BaseModel):
    kind: Literal["memory_record"] = "memory_record"
    memory_type: str
    status: str
    linked_to_run: bool = True


class LatencyBudgetExpectation(BaseModel):
    kind: Literal["latency_budget"] = "latency_budget"
    seconds: float = Field(gt=0)


class ProbeOutcomeExpectation(BaseModel):
    """A step that is its own assertion: a probe ran, and reached the verdict it states.

    Some requirements have no status code to read and no domain object to inspect -- the
    index generation switch and reclaim happening on a real cluster, a console rendering
    a submitted run. Their evidence is the probe's own run, so the outcome is recorded on
    the step and asserted here rather than dressed up as something it is not (a probe is
    not a latency budget, and a probe that "passed" because it finished quickly would be
    an assertion about nothing).

    The raw output that produced the verdict belongs in ``StepObservation.detail``: an
    outcome without the output behind it cannot be re-read by anyone, and the grader
    fails a probe step that states an outcome without stating its evidence.
    """

    kind: Literal["probe_outcome"] = "probe_outcome"
    step_id: str = Field(min_length=1, max_length=120)
    outcome: Literal["passed", "failed"] = "passed"


Expectation = Annotated[
    HttpStatusExpectation
    | SubjectScopeExpectation
    | TerminalStatusExpectation
    | RunListedForItsTenantExpectation
    | RunInvisibleToAnotherTenantExpectation
    | NoNewFollowupsExpectation
    | ExactlyOneNewFollowupExpectation
    | FollowupBodyExpectation
    | ActionIntentStatusExpectation
    | ActionIntentUnchangedExpectation
    | RequiredFactsExpectation
    | BannedRecommendationsExpectation
    | CitationsIncludeExpectation
    | CitationsExcludeExpectation
    | NoForeignCitationsExpectation
    | EvidenceRefsResolvableExpectation
    | ReviewDecisionExpectation
    | ContextSelectionExpectation
    | AuditEventExpectation
    | AuditEventAbsentExpectation
    | TimelineEventExpectation
    | GraphEvidenceIsolationExpectation
    | MemoryRecordExpectation
    | LatencyBudgetExpectation
    | ProbeOutcomeExpectation,
    Field(discriminator="kind"),
]


class CaseAssertion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=800)
    expect: Expectation
    #: True only when the assertion is about something that cannot happen without the
    #: resume boundary having re-established the requester's authority. An assertion that
    #: is *expected to be refused* is never this, however close to a write it sits.
    requires_entitlement_verifier: bool = False
    #: The module roster entry whose *observable behaviour* this assertion pins down.
    #:
    #: Distinct from the case's ``modules`` list, which says the path went through a
    #: module. A case that submits a run through the API and reads the ticket back has
    #: gone through the frontend only if a browser was involved, and has not verified it
    #: either way. Leaving this unset is how a case says "I passed through here" without
    #: claiming "I checked here", which is what keeps the coverage table from reporting a
    #: module as covered because a request happened to traverse it.
    verifies_module: str | None = None


class CaseStep(BaseModel):
    """One externally observable action the driver performs, in order.

    Steps exist so the report can say what was done rather than only what came out, and
    so an assertion like "the intent is unchanged" has a named moment to compare against.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=800)
    action: str = Field(min_length=1, max_length=80)
    #: The subject that performs this step, when it is not the case's own subject.
    #:
    #: Two steps genuinely act as somebody else, and neither is an inference the driver
    #: should make. A run that is to be approved is *started by* the requester and
    #: approved by the approver -- an approver who starts their own run and then approves
    #: it is the segregation-of-duties violation, not the thing being tested -- and the
    #: cross-tenant read is performed by the foreign tenant's subject. Reading either off
    #: the case would mean the driver guessing who acts from prose.
    as_subject: str | None = Field(default=None, min_length=1, max_length=100)
    #: The ticket this step runs against, when a case needs more than one.
    #:
    #: The procedure case is the one that does: a cross-ticket procedure is only proposed
    #: once two episodes from two *different* tickets share a pattern key, so a case that
    #: wants to observe the proposal has to open both.
    ticket_ref: str | None = Field(default=None, min_length=1, max_length=100)
    #: The subject whose entitlements a ``KEYCLOAK`` step edits. Named on the step rather
    #: than taken from the case, because every revocation case re-scopes the *requester*
    #: while the approval is performed by somebody else, and taking it from the case
    #: would revoke the approver's grants and test nothing.
    entitlement_subject: str | None = Field(default=None, min_length=1, max_length=100)
    #: Which coordinate to take away. Typed rather than parsed out of the action string:
    #: this step edits the identity provider, and a spec a typo turns into a no-op would
    #: leave a revocation case passing without ever having revoked anything.
    entitlement_kind: Literal["group", "entity", "role", "disable"] | None = None
    #: What to remove -- an id for ``group``/``entity``, a name for ``role``. Absent for
    #: ``disable``, which takes the whole account out of service rather than one grant.
    entitlement_value: int | str | None = None


class AcceptanceCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: ``ACC-04`` or ``ACC-04a``. The suffix exists because three of the requirements
    #: are genuinely several observations -- one isolation control per subject, one
    #: judgement per half of a split decision, one mechanism check beside one end-to-end
    #: check -- and a case carries a single subject and a single execution. Numbering
    #: them as one case would have meant recording three subjects under one id and
    #: grading whichever happened to be last, which is how a control silently stops
    #: being a control.
    id: str = Field(pattern=r"^ACC-[0-9]{2}[a-z]?$")
    title: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1, max_length=1000)
    #: Where the requirement comes from, so a reader can tell a case that encodes a real
    #: acceptance criterion from one that encodes whoever wrote it.
    source: str = Field(min_length=1, max_length=300)
    tenant_id: UUID
    subject: str = Field(min_length=1, max_length=100)
    #: Symbolic name of the ticket this case runs against, resolved by the driver from
    #: the bootstrap's own record of what it created.
    #:
    #: Symbolic rather than a literal id because GLPI assigns ids from a sequence: a
    #: re-provisioned database would give the same two tickets different numbers, and a
    #: case list pinned to the old ones would either fail or -- much worse, with an
    #: ``ON DUPLICATE``-style bootstrap -- silently run against whatever now holds that
    #: number. The driver records the id it resolved on the execution, so the report
    #: still names the concrete ticket the run touched.
    ticket_ref: str | None = None
    knowledge_version: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=2000)
    request_write: bool = False
    #: True when this case is expected to append a followup to its ticket.
    #:
    #: Declared rather than inferred from ``request_write``, because that flag says what
    #: the *run* asks for and most of the write-requesting cases are refusals that must
    #: append nothing. What this flag drives is an isolation rule the case list is checked
    #: against: a ticket a writer owns is used by that case and by no other, so no
    #: read-only case's evidence can be changed by a case that ran before it. Without the
    #: rule the suite is only re-runnable by luck -- the second run of a batch reads a
    #: ticket the first run appended to, and "the same batch, twice" stops being the same
    #: batch. The verifier's own report says which case wrote what, so the flag is also
    #: what lets the coverage table name the writers instead of guessing them.
    writes_followups: bool = False
    timeout_seconds: float = Field(gt=0)
    #: Module rows this case is declared to exercise. The coverage table is generated from
    #: these, so a case that names a module it does not touch is a claim a reader can
    #: falsify by reading its assertions.
    modules: list[str] = Field(default_factory=list)
    steps: list[CaseStep] = Field(min_length=1)
    assertions: list[CaseAssertion] = Field(min_length=1)
    #: True when the acceptance cannot be closed while this case is BLOCKED. Every case
    #: that requires a write is; a case whose value is a negative result is not.
    blocks_acceptance_when_blocked: bool = True

    @model_validator(mode="after")
    def assertion_ids_are_unique(self) -> Self:
        seen: set[str] = set()
        duplicates = [item.id for item in self.assertions if item.id in seen or seen.add(item.id)]
        if duplicates:
            raise ValueError(f"duplicate assertion ids: {duplicates}")
        # Anything that names a step is resolved here rather than at grade time: a typo in
        # a step id would otherwise make the case grade the absence of a step it never
        # took, which reads as a failure of the platform.
        step_ids = {step.id for step in self.steps}
        for assertion in self.assertions:
            expected = assertion.expect
            if isinstance(expected, ActionIntentUnchangedExpectation):
                target = expected.recorded_at_step
            elif isinstance(expected, (HttpStatusExpectation, ProbeOutcomeExpectation)):
                target = expected.step_id
            else:
                target = None
            if target is not None and target not in step_ids:
                raise ValueError(
                    f"{self.id}/{assertion.id} names unknown step {target!r}; "
                    f"declared steps are {sorted(step_ids)}"
                )
        return self


class AcceptanceCaseSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    description: str = ""
    #: Every module the acceptance is supposed to reach, declared up front.
    #:
    #: The roster is what makes "未评估" a computable column instead of a judgement: a
    #: roster entry with no assertion pointing at it is reported as not evaluated,
    #: whether or not a case's path ran through it. Without a declared roster the table
    #: could only ever list what was covered, and a module nobody wrote a case for would
    #: be invisible rather than absent.
    module_roster: list[str] = Field(default_factory=list)
    cases: list[AcceptanceCase] = Field(min_length=1)

    @model_validator(mode="after")
    def case_ids_are_unique(self) -> Self:
        seen: set[str] = set()
        duplicates = [case.id for case in self.cases if case.id in seen or seen.add(case.id)]
        if duplicates:
            raise ValueError(f"duplicate case ids: {duplicates}")
        return self

    @model_validator(mode="after")
    def every_module_reference_is_on_the_roster(self) -> Self:
        known = set(self.module_roster)
        if not known:
            return self
        unknown: list[str] = []
        for case in self.cases:
            for module in case.modules:
                if module not in known:
                    unknown.append(f"{case.id}:modules:{module}")
            for assertion in case.assertions:
                if assertion.verifies_module and assertion.verifies_module not in known:
                    unknown.append(f"{case.id}/{assertion.id}:verifies:{assertion.verifies_module}")
        if unknown:
            raise ValueError(f"module references not on the roster: {sorted(set(unknown))}")
        return self

    def by_id(self, case_id: str) -> AcceptanceCase:
        for case in self.cases:
            if case.id == case_id:
                return case
        raise KeyError(case_id)


def load_acceptance_cases(path: Path) -> AcceptanceCaseSet:
    return AcceptanceCaseSet.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _sort_key(value: object) -> str:
    """A total order over canonicalised values, for ordering members of a set."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _canonical(value: object) -> object:
    """Collapse every unordered container to a form that no longer depends on hashing.

    A ``frozenset`` has no order, but pydantic's ``mode="json"`` dump renders one as a
    list in *iteration* order -- and for strings that order is the hash order, which
    ``PYTHONHASHSEED`` randomises per process. ``sort_keys`` does not reach inside a
    list, so two identical observations serialised in two processes hashed differently
    and the gate reported the report on disk as stale every time a fresh interpreter
    computed the digest. Sorting the members here, by their own canonical rendering,
    removes the hash order without touching containers that really are ordered.
    """
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((_canonical(item) for item in value), key=_sort_key)
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def _digest(payload: object) -> str:
    """A stable digest of a JSON-serialisable value, for binding a report to its inputs.

    Serialised with sorted keys and fixed separators so the digest depends on the value
    and not on how pydantic happened to order a dict on this run. The point of the digest
    is that a case list edited after a report was generated stops matching it, which only
    works if two identical case lists always hash the same -- in *any* process, which is
    what ``_canonical`` is for. The payload is laid out from the python-mode dump rather
    than the json-mode one because only the former still shows which containers were
    sets.
    """
    canonical = json.dumps(_canonical(payload), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def case_set_digest(case_set: AcceptanceCaseSet) -> str:
    """What the cases *say*. A change here invalidates any report generated before it."""
    return _digest(case_set.model_dump(mode="python"))


def execution_digest(executions: list[CaseExecution]) -> str:
    """What was *observed*, in recorded order.

    Order is part of the value rather than normalised away: the same observations with
    different step orderings describe different runs, and a digest that ignored the
    ordering would let a reordered-but-otherwise-identical execution pass as unchanged.
    """
    return _digest([execution.model_dump(mode="python") for execution in executions])


# -------------------------------------------------------------------- observations


class StepObservation(BaseModel):
    """What actually happened when the driver performed a step."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    description: str
    action: str
    started_at: datetime
    elapsed_seconds: float = Field(ge=0)
    http_status: int | None = None
    #: The action hash carried by this step's response, when it carried one. Recorded per
    #: step because "the intent did not change across the refusal" is a statement about
    #: two moments, and only one of them is the end of the run.
    action_hash: str | None = None
    #: The intent status this step's response carried. Recorded for the same reason as
    #: the hash: a run that is approved and executed inside one request only ever shows
    #: its post-execution status, and the earlier one would otherwise be unobservable.
    #: See ``ActionIntentStatusExpectation``.
    action_status: ActionStatus | None = None
    #: Set by probe steps, which decide their own verdict. Never set by a step whose
    #: outcome is read from a response -- there the status is the observation.
    outcome: Literal["passed", "failed"] | None = None
    #: For a probe step, the raw output behind ``outcome``. The grader refuses a probe
    #: outcome that arrives without it.
    detail: str | None = None


class ObservedActionIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action_hash: str
    status: ActionStatus
    action_type: str
    target_id: int
    arguments: dict[str, object] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    requested_by: str | None = None


class ObservedFollowup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    followup_id: int
    #: As GLPI stores it (HTML), and after ``html_to_text``. Both, because the assertion
    #: is about the text a reader sees and the marker comparison happens on that text.
    content_raw: str
    content_text: str


class ObservedMemoryRecord(BaseModel):
    """One memory row as it stood at one moment.

    The same ``memory_id`` may appear more than once, once per moment it was read, and
    ``observed_at_step`` is what tells the readings apart. It has to: a case whose point
    is "the procedure is quarantined until a human activates it" is two observations of
    one row, and a single snapshot per row would let whichever moment the driver happened
    to record stand in for both.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_id: UUID
    memory_type: str
    status: str
    source_run_id: UUID | None = None
    procedure_pattern_key: str | None = None
    content: str = ""
    #: The step during which this row was read. ``None`` for a row read outside any step.
    observed_at_step: str | None = None


class ObservedGraphReading(BaseModel):
    """What one principal's traversal of the graph side channel returned.

    Per principal, because the graph question is a comparison: the same query under two
    identities is what distinguishes a filtered result from an empty graph. A single flat
    list on the execution would be whichever principal the driver happened to run last.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: str = Field(min_length=1, max_length=100)
    source_record_ids: list[str] = Field(default_factory=list)
    #: The step during which this reading was taken, when it belongs to one.
    observed_at_step: str | None = None


class ObservedEnvironment(BaseModel):
    """Facts about the deployment that change what an assertion can mean.

    ``entitlement_verifier_configured`` is here rather than being inferred from an
    outcome because it is a property of the deployment, not of the run: inferring it from
    "the approval failed" would make a genuine permission bug indistinguishable from an
    unconfigured verifier, which is the confusion the whole BLOCKED verdict exists to
    prevent.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    entitlement_verifier_configured: bool
    base_url: str
    tenant_id: UUID
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    deployed_revision: str | None = None
    notes: list[str] = Field(default_factory=list)


def new_followups(
    before: Sequence[ObservedFollowup], after: Sequence[ObservedFollowup]
) -> list[ObservedFollowup]:
    """Followups present after that were not present before, by id.

    Computed by set difference on ids rather than by count: a count comparison reports
    "one new followup" for a run that added one and had another deleted, and the case is
    about what this run wrote.

    A module-level function rather than a method, because the live driver needs the same
    answer *during* a case -- to say that a case which does not declare itself a writer
    has just written into a ticket it shares -- and a second implementation there would be
    a second definition of "new" for the two to disagree about.
    """
    seen = {item.followup_id for item in before}
    return [item for item in after if item.followup_id not in seen]


class CaseExecution(BaseModel):
    """Everything observed for one case, in the form the grader judges.

    The domain's own models are reused rather than mirrored into view classes: the
    grader then judges the same objects the platform validated, and a contract change
    breaks the grader loudly instead of leaving a parallel copy quietly passing.
    """

    model_config = ConfigDict(extra="forbid")

    case_id: str
    #: The case list this observation was taken against, recorded at the moment it was
    #: taken. It is what lets the gate say whether a replay still *describes* the
    #: expectations beside it: without it the only artefact carrying the digest was the
    #: report, which is derived from the replays -- so a case edited and then honestly
    #: re-run still refused to render, and the one command that unblocked it also
    #: silenced the check for the next edit. Empty means the replay predates this field,
    #: which is not a match: it is an observation of unknown provenance.
    cases_digest: str = ""
    environment: ObservedEnvironment
    subject: ObservedSubject | None = None
    steps: list[StepObservation] = Field(default_factory=list)
    run_id: UUID | None = None
    #: Every run this case submitted, in the order it submitted them. ``run_id`` above is
    #: the one the case is *about*; this is the one it *produced*, and the difference
    #: matters for a platform fact that is attributed to whichever run reached it first.
    #: A cross-ticket procedure is proposed by the post-run step of the run at which
    #: corroboration becomes complete, and for a case that submits a pair of runs to
    #: make a pattern corroborable, that is whichever of the two wrote last -- on a
    #: tenant already holding an episode for the pattern, the first of the pair rather
    #: than the second. Judging it against ``run_id`` alone encoded the order as a
    #: requirement and turned a correct attribution into a blocked case (ACC-12b).
    #: Empty means the replay predates this field, and the readers fall back to
    #: ``run_id``, which is the stricter reading rather than the looser one.
    case_run_ids: list[UUID] = Field(default_factory=list)
    terminal_status: RunStatus | None = None
    total_seconds: float | None = None
    action_intent: ObservedActionIntent | None = None
    citations: list[Citation] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    analysis: AnalysisResult | None = None
    review: ReviewResult | None = None
    selection_manifest: list[dict[str, object]] = Field(default_factory=list)
    followups_before: list[ObservedFollowup] = Field(default_factory=list)
    followups_after: list[ObservedFollowup] = Field(default_factory=list)
    timeline_events: list[str] = Field(default_factory=list)
    audit_events: list[str] = Field(default_factory=list)
    memory_records: list[ObservedMemoryRecord] = Field(default_factory=list)
    graph_readings: list[ObservedGraphReading] = Field(default_factory=list)
    foreign_run_status: int | None = None
    listed_for_own_tenant: bool | None = None
    errors: list[str] = Field(default_factory=list)

    @property
    def new_followups(self) -> list[ObservedFollowup]:
        """Followups this case's run added, by id, over what was there before."""
        return new_followups(self.followups_before, self.followups_after)

    def graph_reading(self, subject: str) -> ObservedGraphReading | None:
        for reading in self.graph_readings:
            if reading.subject == subject:
                return reading
        return None

    def step(self, step_id: str) -> StepObservation | None:
        for observation in self.steps:
            if observation.id == step_id:
                return observation
        return None


def load_case_execution(path: Path) -> CaseExecution:
    return CaseExecution.model_validate(json.loads(path.read_text(encoding="utf-8")))
