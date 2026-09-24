"""Turn a case list plus recorded executions into verdicts. Pure, and zero I/O.

No file is opened, no clock is read, no network is touched: ``grade`` is a function from
a case set and a list of executions to an outcome, so the same inputs always produce the
same verdicts and CI can re-derive every one of them from a recorded run. That is the
whole reason the driver only *records* -- anything the grader had to ask the world, the
report could not be replayed.

Three verdicts, and the distinctions between them carry the acceptance:

``PASS``
    every assertion held, and held against something that was actually observed.

``FAIL``
    an assertion did not hold. A fail is a statement about the platform.

``BLOCKED``
    an assertion could not be judged, because the deployment cannot answer the question
    it asks (no entitlement verifier is configured) or the driver could not complete the
    observation. A block is a statement about *us*, and it is deliberately not folded
    into either neighbour: counting it as a pass would close an acceptance nobody ran,
    and counting it as a fail would blame the platform for a deployment choice its
    operator made on purpose.

The rule that keeps those honest is that a prerequisite is declared per assertion and
never inherited. An approval refused because the action hash did not match is refused
before the verifier is consulted, so marking it as needing one would report a genuine
pass as blocked -- and, worse, a verifier outage would hide whichever step really does
depend on it behind a row of blocks that all looked alike.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from servicemind.domain.evidence import self_authored_marker
from servicemind.evaluation.acceptance import (
    ACCEPTANCE_SCHEMA_VERSION,
    NEGATORS,
    AcceptanceCase,
    AcceptanceCaseSet,
    ActionIntentStatusExpectation,
    ActionIntentUnchangedExpectation,
    AssertionKind,
    AuditEventAbsentExpectation,
    AuditEventExpectation,
    BannedPhrase,
    BannedRecommendationsExpectation,
    CaseExecution,
    CitationsExcludeExpectation,
    CitationsIncludeExpectation,
    ContextSelectionExpectation,
    EvidenceRefsResolvableExpectation,
    ExactlyOneNewFollowupExpectation,
    FollowupBodyExpectation,
    GraphEvidenceIsolationExpectation,
    HttpStatusExpectation,
    LatencyBudgetExpectation,
    MemoryRecordExpectation,
    NoForeignCitationsExpectation,
    NoNewFollowupsExpectation,
    ProbeOutcomeExpectation,
    RequiredFactsExpectation,
    ReviewDecisionExpectation,
    RunInvisibleToAnotherTenantExpectation,
    RunListedForItsTenantExpectation,
    SubjectScopeExpectation,
    TerminalStatusExpectation,
    TimelineEventExpectation,
    case_set_digest,
    execution_digest,
)
from servicemind.integrations.glpi.models import normalized_text


class Verdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


# ------------------------------------------------------------------- text handling


_WHITESPACE = re.compile(r"\s+")

#: The grader does not get its own opinion about when two followups say the same thing.
#: This is the executor's own comparison, imported rather than restated, so the assertion
#: that checks a write and the code that verifies it cannot drift apart.
normalize_text = normalized_text


def _one_line(value: str, limit: int = 240) -> str:
    """A probe's raw output, folded to one readable line for the assertion detail.

    Only for the detail string: the untruncated output stays in the observation, which is
    what the report prints.
    """
    collapsed = _WHITESPACE.sub(" ", value).strip()
    return collapsed if len(collapsed) <= limit else f"{collapsed[:limit]}..."


#: A violation must be read as the sentence it is written in, not as a substring.
#:
#: The corpus's correct remedy says "do not disable multi-factor authentication", so a
#: ban on "disable ... mfa" that ignored negation would flag the right answer. Sentences
#: rather than commas, because "must not, under any circumstances, disable MFA" splits
#: the negator away from the verb if commas end a clause, and the fragment "disable MFA"
#: then reads as a violation of a sentence that forbids it.
_SENTENCE = re.compile(r"[.;:!?\n]+")

#: Words that invert the sentence they appear in. ``avoid`` is deliberately absent:
#: "avoid the challenge" is a violation phrased as advice, and treating it as a negation
#: would hide precisely the recommendation this check exists to catch. ``instead of`` and
#: ``rather than`` are present for the mirror-image reason -- they unambiguously mean
#: "not this, but that one", so "instead of disabling MFA, reset the device" is a
#: prohibition and flagging it would be a false positive on a correct answer.
_NEGATOR = re.compile(
    r"\b(?:" + "|".join(re.escape(negator) for negator in NEGATORS) + r"|instead of|rather than)\b",
    re.IGNORECASE,
)


def _surfaces(analysis) -> list[tuple[str, str]]:  # noqa: ANN001 - AnalysisResult, kept loose
    """Where a recommendation can live, as (label, text) pairs.

    A recommendation is not only the two summary fields. A ``recommended_action`` claim
    is a recommendation the reviewer reads by name, and the body of a proposed action is
    what a person would eventually see on the ticket -- a ban that skipped either would
    be a ban on the wording rather than on the advice.
    """
    if analysis is None:
        return []
    found: list[tuple[str, str]] = []
    for label in ("problem_recommendation", "change_recommendation"):
        value = getattr(analysis, label, None)
        if isinstance(value, str) and value:
            found.append((label, value))
    for claim in getattr(analysis, "claims", ()) or ():
        if getattr(claim, "claim_type", None) == "recommended_action":
            statement = getattr(claim, "statement", "")
            if statement:
                found.append((f"claim:{claim.claim_id}", statement))
    for index, action in enumerate(getattr(analysis, "proposed_actions", ()) or ()):
        content = (getattr(action, "arguments", None) or {}).get("content")
        if isinstance(content, str) and content:
            found.append((f"proposed_action[{index}]:content", content))
    return found


def _matched_groups(text: str, ban: BannedPhrase) -> list[re.Pattern[str]]:
    """Which of a ban's concepts this text contains, as the patterns that matched them."""
    matched = []
    for alternatives in ban.groups:
        pattern = re.compile("|".join(alternatives), re.IGNORECASE)
        if pattern.search(text):
            matched.append(pattern)
    return matched


def find_ban_violation(text: str, ban: BannedPhrase) -> str | None:
    """The clause that violates a ban, or ``None`` if no clause does.

    A clause violates when every one of the ban's concept groups appears in it and no
    negator precedes the earliest of those matches. Negation is judged per clause rather
    than per text, so a document that forbids the thing in one sentence and requires it
    in the next is caught by the sentence that requires it.
    """
    for sentence in _SENTENCE.split(text):
        clause = sentence.strip()
        if not clause:
            continue
        matched = _matched_groups(clause, ban)
        if len(matched) != len(ban.groups):
            continue
        earliest = min(match.start() for pattern in matched if (match := pattern.search(clause)))
        if _NEGATOR.search(clause[:earliest]):
            continue
        return clause
    return None


# -------------------------------------------------------------------- outcomes


class AssertionOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    assertion_id: str
    description: str
    kind: AssertionKind
    verifies_module: str | None = None
    requires_entitlement_verifier: bool
    verdict: Verdict
    #: Why, in one line, for both a pass and a failure. A pass with no evidence recorded
    #: is indistinguishable from a pass that was assumed, and the report has to be able
    #: to print what was actually seen.
    detail: str
    expectation: dict[str, object] = Field(default_factory=dict)


class CaseOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    title: str
    goal: str
    source: str
    verdict: Verdict
    blocks_acceptance_when_blocked: bool
    modules: list[str] = Field(default_factory=list)
    assertions: list[AssertionOutcome] = Field(default_factory=list)
    failure_reasons: list[str] = Field(default_factory=list)
    blocked_reasons: list[str] = Field(default_factory=list)
    driver_errors: list[str] = Field(default_factory=list)
    run_id: str | None = None
    terminal_status: str | None = None
    total_seconds: float | None = None


class CoverageRow(BaseModel):
    """One module's row of the coverage table, generated rather than asserted.

    ``involved_cases`` and ``verified_assertions`` are different facts and the table
    keeps them apart on purpose. A case whose path ran *through* a module has not
    verified that module's behaviour -- a run submitted over HTTP and read back from
    GLPI has gone through the frontend only if a browser was involved, and has not
    checked the frontend either way. Only an assertion that names the module as the thing
    it pins down counts, which is why ``verifies_module`` is on the assertion and not
    inferred from the case.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    module: str
    involved_cases: list[str] = Field(default_factory=list)
    verified_assertions: list[dict[str, str]] = Field(default_factory=list)
    blocked_assertions: list[str] = Field(default_factory=list)
    #: True when nothing in the case list pins this module down. A roster entry in this
    #: state is the honest answer to "was this covered?" -- it was not evaluated.
    not_evaluated: bool


class AcceptanceOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    verdict: Verdict
    cases: list[CaseOutcome] = Field(default_factory=list)
    coverage: list[CoverageRow] = Field(default_factory=list)
    cases_digest: str
    observation_digest: str
    generated_at: datetime
    blockers: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------- judging


def _judge(
    case: AcceptanceCase,
    assertion,
    execution: CaseExecution,  # noqa: ANN001 - discriminated union
) -> tuple[Verdict, str, dict[str, object]]:
    """One assertion, against one execution. Returns (verdict, why, expectation echo)."""
    expected = assertion.expect
    expectation = expected.model_dump(mode="json")

    if isinstance(expected, HttpStatusExpectation):
        step = execution.step(expected.step_id)
        if step is None:
            return Verdict.FAIL, f"step {expected.step_id!r} was never performed", expectation
        if step.http_status is None:
            return Verdict.FAIL, f"step {expected.step_id!r} recorded no status", expectation
        if step.http_status != expected.status:
            return (
                Verdict.FAIL,
                f"step {expected.step_id!r} returned {step.http_status}, expected {expected.status}",
                expectation,
            )
        return Verdict.PASS, f"step {expected.step_id!r} returned {step.http_status}", expectation

    if isinstance(expected, SubjectScopeExpectation):
        subject = execution.subject
        if subject is None:
            return Verdict.FAIL, "the identity the case ran as was not recorded", expectation
        mismatches: list[str] = []
        if expected.tenant_id is not None and subject.tenant_id != expected.tenant_id:
            mismatches.append(f"tenant {subject.tenant_id} != {expected.tenant_id}")
        if expected.group_ids is not None and set(expected.group_ids) != set(subject.group_ids):
            mismatches.append(f"groups {sorted(subject.group_ids)} != {sorted(expected.group_ids)}")
        if expected.entity_ids is not None and set(expected.entity_ids) != set(subject.entity_ids):
            mismatches.append(
                f"entities {sorted(subject.entity_ids)} != {sorted(expected.entity_ids)}"
            )
        missing_roles = sorted(set(expected.roles_superset) - set(subject.roles))
        if missing_roles:
            mismatches.append(f"roles missing {missing_roles}")
        if mismatches:
            return (
                Verdict.FAIL,
                f"{subject.username} authenticated as " + "; ".join(mismatches),
                (expectation),
            )
        return (
            Verdict.PASS,
            f"{subject.username} carried tenant {subject.tenant_id}, "
            f"groups {sorted(subject.group_ids)}, entities {sorted(subject.entity_ids)}, "
            f"roles {sorted(subject.roles)}",
            expectation,
        )

    if isinstance(expected, TerminalStatusExpectation):
        if execution.terminal_status is None:
            return Verdict.FAIL, "no terminal status was recorded", expectation
        if execution.terminal_status is not expected.status:
            return (
                Verdict.FAIL,
                f"run ended {execution.terminal_status.value}, expected {expected.status.value}",
                expectation,
            )
        return Verdict.PASS, f"run ended {execution.terminal_status.value}", expectation

    if isinstance(expected, RunListedForItsTenantExpectation):
        if execution.listed_for_own_tenant is None:
            return Verdict.FAIL, "listing the run for its own tenant was not attempted", expectation
        if not execution.listed_for_own_tenant:
            return Verdict.FAIL, "the run was not listed for its own tenant", expectation
        return Verdict.PASS, "the run is listed for its own tenant", expectation

    if isinstance(expected, RunInvisibleToAnotherTenantExpectation):
        if execution.foreign_run_status is None:
            return Verdict.FAIL, "the cross-tenant read was not attempted", expectation
        if execution.foreign_run_status != 404:
            return (
                Verdict.FAIL,
                f"a foreign tenant received {execution.foreign_run_status}, expected 404",
                expectation,
            )
        return Verdict.PASS, "a foreign tenant received 404", expectation

    if isinstance(expected, NoNewFollowupsExpectation):
        new = execution.new_followups
        if new:
            return (
                Verdict.FAIL,
                f"{len(new)} followup(s) were added: {[item.followup_id for item in new]}",
                expectation,
            )
        return Verdict.PASS, "no followup was added", expectation

    if isinstance(expected, ExactlyOneNewFollowupExpectation):
        new = execution.new_followups
        if len(new) != 1:
            return (
                Verdict.FAIL,
                f"{len(new)} followup(s) were added, expected exactly 1: "
                f"{[item.followup_id for item in new]}",
                expectation,
            )
        return (
            Verdict.PASS,
            f"exactly one followup was added (id {new[0].followup_id})",
            expectation,
        )

    if isinstance(expected, FollowupBodyExpectation):
        new = execution.new_followups
        if len(new) != 1:
            return (
                Verdict.FAIL,
                f"{len(new)} new followup(s) to read back, expected exactly 1",
                expectation,
            )
        intent = execution.action_intent
        if intent is None:
            return Verdict.FAIL, "no action intent was recorded", expectation
        approved = intent.arguments.get("content")
        if not isinstance(approved, str) or not approved:
            return Verdict.FAIL, "the approved intent carried no content", expectation
        marker = self_authored_marker(execution.run_id, intent.action_hash)
        observed = normalize_text(new[0].content_raw)
        if marker not in observed:
            return (
                Verdict.FAIL,
                f"the stored body does not carry this run's marker {marker!r}; "
                f"observed {observed[:400]!r}",
                expectation,
            )
        body = normalize_text(observed.replace(marker, " "))
        if body != normalize_text(approved):
            return (
                Verdict.FAIL,
                "the stored body is not the approved content: "
                f"expected {normalize_text(approved)[:400]!r}, observed {body[:400]!r}",
                expectation,
            )
        return (
            Verdict.PASS,
            f"followup {new[0].followup_id} carries the approved content and this run's marker",
            expectation,
        )

    if isinstance(expected, ActionIntentStatusExpectation):
        if expected.recorded_at_step is None:
            if execution.action_intent is None:
                return Verdict.FAIL, "no action intent was recorded", expectation
            actual = execution.action_intent.status
            where = "at the end of the run"
        else:
            step = execution.step(expected.recorded_at_step)
            if step is None:
                return (
                    Verdict.FAIL,
                    f"step {expected.recorded_at_step!r} was never performed",
                    expectation,
                )
            if step.action_status is None:
                return (
                    Verdict.FAIL,
                    f"step {expected.recorded_at_step!r} recorded no action status, so "
                    f"nothing about the intent at that moment was observed",
                    expectation,
                )
            actual = step.action_status
            where = f"at step {expected.recorded_at_step!r}"
        if actual is not expected.status:
            return (
                Verdict.FAIL,
                f"intent status {where} is {actual.value}, expected {expected.status.value}",
                expectation,
            )
        return Verdict.PASS, f"intent status {where} is {actual.value}", expectation

    if isinstance(expected, ActionIntentUnchangedExpectation):
        step = execution.step(expected.recorded_at_step)
        if step is None:
            return (
                Verdict.FAIL,
                f"step {expected.recorded_at_step!r} was never performed",
                expectation,
            )
        if execution.action_intent is None:
            return Verdict.FAIL, "no action intent was recorded", expectation
        if step.action_hash is None:
            return (
                Verdict.FAIL,
                f"step {expected.recorded_at_step!r} recorded no action hash",
                expectation,
            )
        if execution.action_intent.action_hash != step.action_hash:
            return (
                Verdict.FAIL,
                f"the intent changed after step {expected.recorded_at_step!r}: "
                f"{step.action_hash} -> {execution.action_intent.action_hash}",
                expectation,
            )
        return (
            Verdict.PASS,
            f"the intent still carries {step.action_hash} from step {expected.recorded_at_step!r}",
            expectation,
        )

    if isinstance(expected, RequiredFactsExpectation):
        return _judge_required_facts(expected, execution, expectation)

    if isinstance(expected, BannedRecommendationsExpectation):
        return _judge_bans(expected, execution, expectation)

    if isinstance(
        expected,
        (CitationsIncludeExpectation, CitationsExcludeExpectation, NoForeignCitationsExpectation),
    ):
        present = {citation.source_record_id for citation in execution.citations}
        if isinstance(expected, CitationsIncludeExpectation):
            missing = sorted(set(expected.source_record_ids) - present)
            if missing:
                return (
                    Verdict.FAIL,
                    f"citations do not include {missing}; present: {sorted(present)}",
                    expectation,
                )
            return Verdict.PASS, f"citations include {expected.source_record_ids}", expectation
        forbidden = sorted(set(expected.source_record_ids) & present)
        if forbidden:
            return Verdict.FAIL, f"citations include forbidden {forbidden}", expectation
        return Verdict.PASS, f"citations exclude {expected.source_record_ids}", expectation

    if isinstance(expected, EvidenceRefsResolvableExpectation):
        return _judge_resolvable_refs(expected, execution, expectation)

    if isinstance(expected, ReviewDecisionExpectation):
        if execution.review is None:
            return Verdict.FAIL, "no review was recorded", expectation
        review = execution.review
        if review.decision is not expected.decision:
            return (
                Verdict.FAIL,
                f"the reviewer decided {review.decision.value}, expected {expected.decision.value}",
                expectation,
            )
        if expected.forbid_blocking_findings:
            blocking = [
                find.check_id for find in review.findings if find.severity in {"error", "critical"}
            ]
            if blocking:
                return (
                    Verdict.FAIL,
                    f"the reviewer passed the case while reporting blocking findings {blocking}",
                    expectation,
                )
        return Verdict.PASS, f"the reviewer decided {review.decision.value}", expectation

    if isinstance(expected, ContextSelectionExpectation):
        manifest = execution.selection_manifest
        if not manifest:
            return Verdict.FAIL, "the selection manifest is empty", expectation
        selected = {
            str(item.get("item_id")) for item in manifest if item.get("decision") == "selected"
        }
        missing = sorted(set(expected.required_selected_item_ids) - selected)
        if missing:
            return (
                Verdict.FAIL,
                f"the manifest does not select {missing}; selected: {sorted(selected)}",
                expectation,
            )
        if expected.require_pruning_with_reason:
            dropped = [
                item
                for item in manifest
                if item.get("decision") in {"pruned", "rejected"} and str(item.get("reason") or "")
            ]
            if not dropped:
                counts: dict[str, int] = {}
                for item in manifest:
                    key = str(item.get("decision"))
                    counts[key] = counts.get(key, 0) + 1
                return (
                    Verdict.FAIL,
                    f"the over-budget case shows no pruned/rejected item with a reason; "
                    f"decisions were {counts}",
                    expectation,
                )
            return (
                Verdict.PASS,
                f"{len(dropped)} item(s) were dropped with a reason, "
                f"first: {dropped[0].get('item_id')} ({dropped[0].get('decision')})",
                expectation,
            )
        return Verdict.PASS, f"{len(selected)} item(s) were selected", expectation

    if isinstance(expected, AuditEventExpectation):
        if expected.event_type not in execution.audit_events:
            return (
                Verdict.FAIL,
                f"audit event {expected.event_type!r} was not recorded; saw {execution.audit_events}",
                expectation,
            )
        return Verdict.PASS, f"audit event {expected.event_type!r} was recorded", expectation

    if isinstance(expected, AuditEventAbsentExpectation):
        if expected.event_type in execution.audit_events:
            return (
                Verdict.FAIL,
                f"audit event {expected.event_type!r} was recorded, and this case is the "
                f"one where it must not be; saw {execution.audit_events}",
                expectation,
            )
        return (
            Verdict.PASS,
            f"audit event {expected.event_type!r} was not recorded",
            expectation,
        )

    if isinstance(expected, TimelineEventExpectation):
        if expected.event_type not in execution.timeline_events:
            return (
                Verdict.FAIL,
                f"timeline event {expected.event_type!r} was not recorded; "
                f"saw {execution.timeline_events}",
                expectation,
            )
        return Verdict.PASS, f"timeline event {expected.event_type!r} was recorded", expectation

    if isinstance(expected, GraphEvidenceIsolationExpectation):
        reading = execution.graph_reading(execution.subject.username) if execution.subject else None
        if reading is None:
            return (
                Verdict.FAIL,
                "no graph reading was recorded for the case's own subject, so neither the "
                "permitted nor the restricted node was observed",
                expectation,
            )
        found = set(reading.source_record_ids)
        if expected.visible_source_record_id not in found:
            return (
                Verdict.FAIL,
                f"the permitted graph node {expected.visible_source_record_id!r} was not "
                f"retrieved either -- this is not isolation, it is an empty retriever; "
                f"graph returned {sorted(found)}",
                expectation,
            )
        if expected.hidden_source_record_id in found:
            return (
                Verdict.FAIL,
                f"the restricted graph node {expected.hidden_source_record_id!r} was retrieved",
                expectation,
            )
        if expected.witness_subject is not None:
            witness = execution.graph_reading(expected.witness_subject)
            if witness is None:
                return (
                    Verdict.FAIL,
                    f"no graph reading was recorded for the witness {expected.witness_subject!r}, "
                    "so the restricted node's absence was never shown to be filtering",
                    expectation,
                )
            if expected.hidden_source_record_id not in set(witness.source_record_ids):
                return (
                    Verdict.FAIL,
                    f"the witness {expected.witness_subject!r} did not retrieve the restricted "
                    f"node {expected.hidden_source_record_id!r} either, so that node is not on "
                    f"this traversal at all and its absence under {reading.subject!r} proves "
                    f"nothing; witness saw {sorted(set(witness.source_record_ids))}",
                    expectation,
                )
            return (
                Verdict.PASS,
                f"the restricted node is absent for {reading.subject!r} and present for the "
                f"witness {witness.subject!r}, so the difference is the ACL",
                expectation,
            )
        return (
            Verdict.PASS,
            f"the permitted node was retrieved and the restricted one was not ({sorted(found)})",
            expectation,
        )

    if isinstance(expected, MemoryRecordExpectation):
        matches = [
            record
            for record in execution.memory_records
            if record.memory_type == expected.memory_type and record.status == expected.status
        ]
        if expected.linked_to_run:
            # "This run" means a run this case produced, not only the one it is named
            # after. The platform attributes a derived record to the run whose post-run
            # step reached the conclusion, and for a case built out of a *pair* of runs
            # that is a fact about which one wrote last -- not a requirement the case may
            # impose without asserting the order along with it. See ``case_run_ids``.
            produced = set(execution.case_run_ids) or (
                {execution.run_id} if execution.run_id is not None else set()
            )
            matches = [record for record in matches if record.source_run_id in produced]
        if not matches:
            return (
                Verdict.FAIL,
                f"no {expected.memory_type}/{expected.status} record linked to a run of "
                f"this case; "
                f"saw {[(r.memory_type, r.status, str(r.source_run_id)) for r in execution.memory_records]}",
                expectation,
            )
        return (
            Verdict.PASS,
            f"memory record(s) {[str(record.memory_id) for record in matches]} are "
            f"{expected.memory_type}/{expected.status} and linked to a run of this case",
            expectation,
        )

    if isinstance(expected, ProbeOutcomeExpectation):
        step = execution.step(expected.step_id)
        if step is None:
            return (
                Verdict.FAIL,
                f"the probe step {expected.step_id!r} was never taken",
                expectation,
            )
        if step.outcome is None:
            return (
                Verdict.FAIL,
                f"step {expected.step_id!r} recorded no probe outcome, so there is nothing "
                f"to judge; a probe that does not state a verdict is not a pass",
                expectation,
            )
        if step.outcome != expected.outcome:
            return (
                Verdict.FAIL,
                f"probe step {expected.step_id!r} reported {step.outcome!r}, "
                f"expected {expected.outcome!r}: {step.detail or '(no output recorded)'}",
                expectation,
            )
        if not (step.detail or "").strip():
            # An outcome with no output behind it cannot be re-read by anyone, and this is
            # the one assertion class whose evidence is the probe's own word. Requiring the
            # raw output is what keeps that word checkable.
            return (
                Verdict.FAIL,
                f"probe step {expected.step_id!r} reported {step.outcome!r} without "
                f"recording the output behind it",
                expectation,
            )
        return (
            Verdict.PASS,
            f"probe step {expected.step_id!r} reported {step.outcome!r}: "
            f"{_one_line(step.detail or '')}",
            expectation,
        )

    if isinstance(expected, LatencyBudgetExpectation):
        if execution.total_seconds is None:
            return Verdict.FAIL, "no duration was recorded for the case", expectation
        if execution.total_seconds > expected.seconds:
            return (
                Verdict.FAIL,
                f"the case took {execution.total_seconds:.1f}s, budget {expected.seconds:.1f}s",
                expectation,
            )
        return (
            Verdict.PASS,
            f"the case took {execution.total_seconds:.1f}s of {expected.seconds:.1f}s",
            expectation,
        )

    # Reached only by adding an expectation without teaching the grader to judge it, which
    # is a dependency error rather than an unknown result: it must not be able to look
    # like a pass.
    return Verdict.FAIL, f"no grader is implemented for {expected.kind!r}", expectation


def _judge_required_facts(
    expected: RequiredFactsExpectation, execution: CaseExecution, expectation: dict[str, object]
) -> tuple[Verdict, str, dict[str, object]]:
    analysis = execution.analysis
    if analysis is None:
        return Verdict.FAIL, "no analysis was recorded to hold the required facts", expectation
    claims = list(analysis.claims)
    by_id = {evidence.evidence_id: evidence for evidence in execution.evidence}
    unresolved: list[str] = []
    satisfied: list[str] = []
    for fact in expected.facts:
        patterns = [re.compile(pattern, re.IGNORECASE) for pattern in fact.patterns]
        groups = [
            [re.compile(pattern, re.IGNORECASE) for pattern in group] for group in fact.all_of
        ]

        def states(statement: str) -> bool:
            """Whether this claim states the fact, by phrasing or by proposition.

            Ordered phrasings first, then the order-free reading: every concept group
            present somewhere in the sentence. The second is what keeps a correct claim
            in a word order nobody thought to write down from counting as a miss.
            """
            if any(pattern.search(statement) for pattern in patterns):
                return True
            return bool(groups) and all(
                any(pattern.search(statement) for pattern in group) for group in groups
            )

        matching = [
            claim
            for claim in claims
            if (not fact.claim_types or claim.claim_type in fact.claim_types)
            and states(claim.statement)
        ]
        if not matching:
            unresolved.append(
                f"{fact.id}: no {'/'.join(fact.claim_types) or 'any'} claim states {fact.description!r}"
            )
            continue
        if fact.must_not_cite:
            offending: list[str] = []
            for claim in matching:
                for ref in claim.evidence_refs:
                    evidence = by_id.get(ref)
                    if evidence is not None and evidence.source_record_id in fact.must_not_cite:
                        offending.append(f"{claim.claim_id} -> {evidence.source_record_id}")
            if offending:
                unresolved.append(
                    f"{fact.id}: stated, but grounded in a forbidden source ({'; '.join(offending)})"
                )
                continue
        if fact.must_cite:
            cited: set[str] = set()
            for claim in matching:
                for ref in claim.evidence_refs:
                    evidence = by_id.get(ref)
                    if evidence is not None and evidence.source_record_id:
                        cited.add(evidence.source_record_id)
            missing = sorted(set(fact.must_cite) - cited)
            if missing:
                unresolved.append(
                    f"{fact.id}: stated, but no matching claim cites {missing} "
                    f"(cited: {sorted(cited)})"
                )
                continue
        satisfied.append(f"{fact.id} via {[claim.claim_id for claim in matching]}")
    if unresolved:
        return Verdict.FAIL, "; ".join(unresolved), expectation
    return (
        Verdict.PASS,
        f"all facts stated and correctly grounded: {'; '.join(satisfied)}",
        expectation,
    )


def _judge_bans(
    expected: BannedRecommendationsExpectation,
    execution: CaseExecution,
    expectation: dict[str, object],
) -> tuple[Verdict, str, dict[str, object]]:
    surfaces = _surfaces(execution.analysis)
    if execution.analysis is None:
        return Verdict.FAIL, "no analysis was recorded to check for banned advice", expectation
    violations: list[str] = []
    for ban in expected.bans:
        for label, text in surfaces:
            clause = find_ban_violation(text, ban)
            if clause is not None:
                violations.append(f"{ban.id} in {label}: {clause!r}")
        for index, action in enumerate(execution.analysis.proposed_actions or ()):
            # A structured action has no sentence to negate -- an operation named
            # ``disable_mfa`` proposing to act on an account is the violation itself, and
            # scanning its free-text arguments for it would flag the note that forbids it.
            operation = " ".join(
                [action.operation, action.resource_type, action.resource_id]
            ).replace("_", " ")
            clause = find_ban_violation(operation, ban)
            if clause is not None:
                violations.append(f"{ban.id} in proposed_action[{index}] operation: {clause!r}")
    if violations:
        return Verdict.FAIL, "; ".join(sorted(set(violations))), expectation
    checked = [f"{ban.id}" for ban in expected.bans]
    return (
        Verdict.PASS,
        f"no banned advice in {len(surfaces)} recommendation surface(s); checked {checked}",
        expectation,
    )


def _judge_resolvable_refs(
    expected: EvidenceRefsResolvableExpectation,
    execution: CaseExecution,
    expectation: dict[str, object],
) -> tuple[Verdict, str, dict[str, object]]:
    known = {evidence.evidence_id for evidence in execution.evidence}
    dangling: list[str] = []
    checked = 0
    if expected.include_claims and execution.analysis is not None:
        for claim in execution.analysis.claims:
            checked += len(claim.evidence_refs)
            dangling.extend(
                f"{claim.claim_id} -> {ref}" for ref in claim.evidence_refs if ref not in known
            )
    if expected.include_review and execution.review is not None:
        refs = list(execution.review.reviewed_evidence_refs)
        for finding in execution.review.findings:
            refs.extend(finding.evidence_refs)
        checked += len(refs)
        dangling.extend(f"review -> {ref}" for ref in refs if ref not in known)
    if dangling:
        return (
            Verdict.FAIL,
            f"unresolvable evidence references: {sorted(set(dangling))}",
            expectation,
        )
    return (
        Verdict.PASS,
        f"all {checked} evidence reference(s) resolve to a recorded row",
        expectation,
    )


# ----------------------------------------------------------------------- grading


def grade_case(case: AcceptanceCase, execution: CaseExecution) -> CaseOutcome:
    """Judge one case. The execution must record that case; ids are checked, not assumed."""
    if execution.case_id != case.id:
        raise ValueError(f"execution is for {execution.case_id!r}, not {case.id!r}")
    verifier_configured = execution.environment.entitlement_verifier_configured

    outcomes: list[AssertionOutcome] = []
    for assertion in case.assertions:
        if assertion.requires_entitlement_verifier and not verifier_configured:
            verdict, detail, expectation = (
                Verdict.BLOCKED,
                "the entitlement verifier is not configured for this deployment, so this "
                "assertion cannot be judged; the deployment chose to pause rather than "
                "hold realm-admin credentials",
                assertion.expect.model_dump(mode="json"),
            )
        else:
            verdict, detail, expectation = _judge(case, assertion, execution)
        outcomes.append(
            AssertionOutcome(
                assertion_id=assertion.id,
                description=assertion.description,
                kind=AssertionKind(assertion.expect.kind),
                verifies_module=assertion.verifies_module,
                requires_entitlement_verifier=assertion.requires_entitlement_verifier,
                verdict=verdict,
                detail=detail,
                expectation=expectation,
            )
        )

    failures = [item for item in outcomes if item.verdict is Verdict.FAIL]
    blocked = [item for item in outcomes if item.verdict is Verdict.BLOCKED]
    if execution.errors:
        # The driver stopped, so every step after the failure was never taken -- and an
        # assertion about a step that was never taken fails for that reason rather than
        # for the platform's. Letting those failures stand would report our own bug as a
        # platform defect; letting them vanish would hide a real failure that happened
        # *before* the driver gave up. So the case is unobserved, and the assertion-level
        # verdicts are kept in ``failure_reasons`` for the reader doing triage.
        verdict = Verdict.BLOCKED
    elif failures:
        verdict = Verdict.FAIL
    elif blocked:
        verdict = Verdict.BLOCKED
    else:
        verdict = Verdict.PASS

    blocked_reasons = [f"{item.assertion_id}: {item.detail}" for item in blocked]
    if execution.errors:
        # A driver that could not finish has not observed the thing the case is about, and
        # an unobserved case is neither a pass nor a platform failure. Recording the
        # driver's own errors here is what keeps a half-run from being read as a result.
        blocked_reasons.extend(f"driver: {error}" for error in execution.errors)

    return CaseOutcome(
        case_id=case.id,
        title=case.title,
        goal=case.goal,
        source=case.source,
        verdict=verdict,
        blocks_acceptance_when_blocked=case.blocks_acceptance_when_blocked,
        modules=list(case.modules),
        assertions=outcomes,
        failure_reasons=[f"{item.assertion_id}: {item.detail}" for item in failures],
        blocked_reasons=blocked_reasons,
        driver_errors=list(execution.errors),
        run_id=str(execution.run_id) if execution.run_id else None,
        terminal_status=execution.terminal_status.value if execution.terminal_status else None,
        total_seconds=execution.total_seconds,
    )


def coverage_rows(case_set: AcceptanceCaseSet, outcomes: list[CaseOutcome]) -> list[CoverageRow]:
    """Generate the coverage table from the assertions that actually ran.

    Rows come from the declared roster as well as from the cases, so a module nobody wrote
    a case for appears as ``not_evaluated`` instead of being absent. A table built only
    from what was covered can never show a gap, which is the failure mode this column
    exists to prevent.
    """
    by_case = {outcome.case_id: outcome for outcome in outcomes}
    modules = sorted(
        set(case_set.module_roster) | {m for case in case_set.cases for m in case.modules}
    )
    rows: list[CoverageRow] = []
    for module in modules:
        involved = sorted(case.id for case in case_set.cases if module in case.modules)
        verified: list[dict[str, str]] = []
        blocked: list[str] = []
        for case in case_set.cases:
            outcome = by_case.get(case.id)
            if outcome is None:
                continue
            for item in outcome.assertions:
                if item.verifies_module != module:
                    continue
                pointer = f"{case.id}/{item.assertion_id}"
                if item.verdict is Verdict.BLOCKED:
                    blocked.append(pointer)
                    continue
                verified.append(
                    {
                        "case": case.id,
                        "assertion": item.assertion_id,
                        "kind": item.kind.value,
                        "verdict": item.verdict.value,
                        "evidence": item.detail,
                    }
                )
        rows.append(
            CoverageRow(
                module=module,
                involved_cases=involved,
                verified_assertions=verified,
                blocked_assertions=blocked,
                not_evaluated=not verified,
            )
        )
    return rows


def grade(
    case_set: AcceptanceCaseSet, executions: list[CaseExecution], *, generated_at: datetime
) -> AcceptanceOutcome:
    """Judge every case. ``generated_at`` is passed in rather than read from the clock.

    A grader that reads the time is a grader whose output changes when nothing did, which
    would make the report's digest useless as a statement about the run.
    """
    by_id = {execution.case_id: execution for execution in executions}
    missing = [case.id for case in case_set.cases if case.id not in by_id]
    outcomes = [grade_case(case, by_id[case.id]) for case in case_set.cases if case.id in by_id]

    blockers: list[str] = []
    for outcome in outcomes:
        if outcome.verdict is Verdict.FAIL:
            blockers.append(f"{outcome.case_id} FAILED: {outcome.failure_reasons}")
        elif outcome.verdict is Verdict.BLOCKED and outcome.blocks_acceptance_when_blocked:
            blockers.append(f"{outcome.case_id} BLOCKED: {outcome.blocked_reasons}")
        elif outcome.driver_errors:
            # An unobserved case blocks whatever it declared, because the claim the
            # acceptance makes is that its cases were executed. ``blocks_acceptance_when_blocked``
            # is a statement about what a *platform* block costs, and a driver that could
            # not finish is not the platform choosing to pause.
            blockers.append(
                f"{outcome.case_id} NOT OBSERVED: driver errors {outcome.driver_errors}"
            )
    for case_id in missing:
        # A case that was never run blocks exactly as hard as one that failed: the
        # acceptance claims its cases were executed, and an absent execution is the one
        # state in which that claim is unverified rather than disproven.
        blockers.append(f"{case_id} NO EXECUTION RECORDED")

    unobserved = any(outcome.driver_errors for outcome in outcomes)
    if any(outcome.verdict is Verdict.FAIL for outcome in outcomes) or missing:
        verdict = Verdict.FAIL
    elif unobserved or any(
        outcome.verdict is Verdict.BLOCKED and outcome.blocks_acceptance_when_blocked
        for outcome in outcomes
    ):
        # A case the driver could not carry through keeps the acceptance open even when
        # the case itself would not have: "this one would not have closed it anyway" is
        # an argument about a result nobody obtained.
        verdict = Verdict.BLOCKED
    else:
        verdict = Verdict.PASS

    return AcceptanceOutcome(
        schema_version=ACCEPTANCE_SCHEMA_VERSION,
        verdict=verdict,
        cases=outcomes,
        coverage=coverage_rows(case_set, outcomes),
        cases_digest=case_set_digest(case_set),
        observation_digest=execution_digest(executions),
        generated_at=generated_at,
        blockers=blockers,
    )


def render_coverage_markdown(outcome: AcceptanceOutcome) -> str:
    """The coverage table as markdown, with the two columns kept apart."""
    lines = [
        "| 模块 | 涉及（案例） | 验证了该模块的具体行为（断言 id + 证据） | 未评估 |",
        "|---|---|---|---|",
    ]
    for row in outcome.coverage:
        verified = (
            "<br>".join(
                f"`{item['case']}/{item['assertion']}` ({item['kind']}, {item['verdict']}) — "
                f"{item['evidence']}"
                for item in row.verified_assertions
            )
            or "—"
        )
        if row.blocked_assertions:
            verified += "<br>**BLOCKED**: " + ", ".join(
                f"`{pointer}`" for pointer in row.blocked_assertions
            )
        lines.append(
            f"| `{row.module}` | {', '.join(row.involved_cases) or '—'} | {verified} | "
            f"{'**是**' if row.not_evaluated else '否'} |"
        )
    return "\n".join(lines)


def dump_outcome(outcome: AcceptanceOutcome) -> str:
    return json.dumps(outcome.model_dump(mode="json"), indent=2, sort_keys=True, ensure_ascii=False)
