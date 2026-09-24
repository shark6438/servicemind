"""The acceptance contract, judged on its own. No services, no cluster, no clock.

This file is what the online run's verdicts are worth. The grader is a pure function, so
everything the online half relies on -- that a kind has a judge, that a prerequisite is
read from the assertion rather than inherited, that a block cannot be spent as a pass, that
a probe has to show its output -- can be pinned down here, in CI, on every pull request,
against no infrastructure at all.

The shipped ``cases.v1.json`` is loaded here too. A case list nobody validates is a case
list that goes stale quietly, and the failure mode that matters is the one where the file
still parses: a case that dropped its one discriminating assertion still loads, still
grades, and still reports a pass.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import typing
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from servicemind.domain.evidence import CITATION_KEY, Evidence, EvidenceSourceType
from servicemind.evaluation.acceptance import (
    AcceptanceCase,
    AcceptanceCaseSet,
    CaseExecution,
    CaseStep,
    Expectation,
    MemoryRecordExpectation,
    ObservedEnvironment,
    ObservedFollowup,
    ObservedMemoryRecord,
    ObservedSubject,
    ProbeOutcomeExpectation,
    RequiredFact,
    StepObservation,
    case_set_digest,
)
from servicemind.evaluation.acceptance_grader import (
    Verdict,
    _judge,
    coverage_rows,
    find_ban_violation,
    grade,
    grade_case,
)
from servicemind.persistence.models import RunStatus

REPO_ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = REPO_ROOT / "evaluation" / "acceptance" / "cases.v1.json"

TENANT = "22222222-2222-4222-8222-222222222222"
NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- fixtures


def _environment(*, verifier: bool = False) -> ObservedEnvironment:
    return ObservedEnvironment(
        entitlement_verifier_configured=verifier,
        base_url="http://127.0.0.1:8000",
        tenant_id=TENANT,
        recorded_at=NOW,
    )


def _step(step_id: str, **overrides: object) -> StepObservation:
    base: dict[str, object] = {
        "id": step_id,
        "description": step_id,
        "action": step_id,
        "started_at": NOW,
        "elapsed_seconds": 0.1,
    }
    base.update(overrides)
    return StepObservation(**base)  # type: ignore[arg-type]


def _case(assertions: list[dict[str, object]], *, steps: list[str] | None = None) -> AcceptanceCase:
    return AcceptanceCase.model_validate(
        {
            "id": "ACC-99",
            "title": "contract",
            "goal": "contract",
            "source": "contract",
            "tenant_id": TENANT,
            "subject": "globex-analyst-g3",
            "ticket_ref": "globex-vpn-mfa-a",
            "knowledge_version": "v1",
            "question": "q",
            "timeout_seconds": 300.0,
            "steps": [
                {"id": name, "description": name, "action": name} for name in (steps or ["submit"])
            ],
            "assertions": assertions,
        }
    )


def _execution(case_id: str = "ACC-99", **overrides: object) -> CaseExecution:
    base: dict[str, object] = {"case_id": case_id, "environment": _environment()}
    base.update(overrides)
    return CaseExecution(**base)  # type: ignore[arg-type]


def _judge_one(case: AcceptanceCase, execution: CaseExecution) -> Verdict:
    return grade_case(case, execution).assertions[0].verdict


# ------------------------------------------------------------------ the case list


@pytest.fixture(scope="module")
def shipped() -> AcceptanceCaseSet:
    return AcceptanceCaseSet.model_validate(json.loads(CASES_PATH.read_text(encoding="utf-8")))


def test_the_shipped_case_list_loads(shipped: AcceptanceCaseSet) -> None:
    assert shipped.schema_version == "phase7-acceptance-v1"
    # 22 core loops plus the six revocation scenarios that only became runnable once a
    # dedicated read-only verifier identity existed. Pinned as a count rather than a set
    # so that dropping one is a failure here rather than a quiet reduction in coverage.
    assert len(shipped.cases) == 28
    ids = [case.id for case in shipped.cases]
    assert len(set(ids)) == len(ids)
    assert all(re.fullmatch(r"ACC-[0-9]{2}[a-z]?", case_id) for case_id in ids)


def test_every_roster_module_is_declared_and_nothing_else_is(shipped: AcceptanceCaseSet) -> None:
    roster = set(shipped.module_roster)
    referenced = {module for case in shipped.cases for module in case.modules}
    referenced |= {
        assertion.verifies_module
        for case in shipped.cases
        for assertion in case.assertions
        if assertion.verifies_module
    }
    # The roster is what makes "未评估" computable. A module asserted on but missing from
    # the roster would be reported as verified while never appearing as a column, and a
    # module in the roster that nothing asserts on is the honest "not evaluated" row.
    assert referenced <= roster


CORE_MODULES = {
    "identity",
    "tenant-isolation",
    "retrieval",
    "graphrag",
    "memory",
    "context",
    "analysis",
    "reviewer",
    "approval",
    "executor",
    "glpi",
    "api",
    "audit-and-events",
}
PROBE_MODULES = {"frontend", "mcp", "outbox", "index-lifecycle"}


def test_a_probe_carries_probe_verdicts_only(shipped: AcceptanceCaseSet) -> None:
    """The core closed loop never opens a browser, speaks MCP, drains the outbox, or
    switches an index generation -- so a probe module's verdict has to come from the probe.

    The converse matters more: if a core case also asserted on a probe module, a probe
    that never ran would read as a core failure, and a core failure would be reported
    under a probe's name.
    """
    for case in shipped.cases:
        verified = {item.verifies_module for item in case.assertions if item.verifies_module}
        assert not (verified & CORE_MODULES and verified & PROBE_MODULES), case.id


def test_the_verifier_prerequisite_is_declared_per_assertion(shipped: AcceptanceCaseSet) -> None:
    """The prerequisite is read off the assertion, never inherited from the case.

    A refusal and a hash conflict are decided before the resume boundary is consulted, so
    requiring a verifier for them would report a genuine pass as blocked -- and would hide
    whichever step really does depend on it behind a row of blocks that all looked alike.
    """

    def needs(case_id: str) -> set[str]:
        case = next(item for item in shipped.cases if item.id == case_id)
        return {
            assertion.id for assertion in case.assertions if assertion.requires_entitlement_verifier
        }

    # Decided before the verifier is consulted: the hash conflict and the refusal.
    assert needs("ACC-09a") == set()
    assert needs("ACC-09b") == set()
    # The pause writes nothing, so it is a real pass with no verifier configured.
    assert needs("ACC-10a") == set()
    # These execute, so they go through the resume boundary and depend on it.
    assert needs("ACC-10b") and needs("ACC-11")
    for case_id in ("ACC-10b", "ACC-11"):
        case = next(item for item in shipped.cases if item.id == case_id)
        assert all(
            assertion.requires_entitlement_verifier
            for assertion in case.assertions
            if assertion.id.endswith(("wrote-once", "one-followup", "body-round-trip", "approved"))
        ), case_id


def test_the_probe_modules_are_not_creditable_without_a_probe(
    shipped: AcceptanceCaseSet,
) -> None:
    rows = coverage_rows(shipped, [])
    probe_rows = {
        row.module: row
        for row in rows
        if row.module in {"frontend", "mcp", "outbox", "index-lifecycle"}
    }
    assert set(probe_rows) == {"frontend", "mcp", "outbox", "index-lifecycle"}
    assert all(row.not_evaluated for row in probe_rows.values())


def test_a_step_that_acts_as_somebody_else_says_so(shipped: AcceptanceCaseSet) -> None:
    """Who acts is declared, never inferred from the case's subject or from prose.

    Two steps genuinely act as a different subject, and both are the point of the case
    rather than a detail. An approval run has to be *started by* the requester: an
    approver who starts their own run and then approves it is the segregation-of-duties
    violation, not the behaviour under test. And the cross-tenant read is performed by
    the foreign tenant's subject -- if it were performed by the case's own subject the
    case would assert that a tenant can read its own runs, which nobody doubts.
    """
    for case in shipped.cases:
        for step in case.steps:
            if step.id == "foreign-read":
                assert step.as_subject and step.as_subject != case.subject, case.id
            if step.id == "submit-action":
                assert step.as_subject, f"{case.id}: the requester is not declared"


def test_a_case_that_needs_two_tickets_declares_both(shipped: AcceptanceCaseSet) -> None:
    """A cross-ticket procedure is only proposed once two episodes from two *different*
    tickets share a pattern key, so a case that asserts one has to open both.

    One ticket would make the case report a pass on a path that was never reachable, and
    the failure would look like a platform defect rather than a case that cannot ask the
    question it claims to ask.
    """
    case = next(item for item in shipped.cases if item.id == "ACC-12b")
    submitted = {
        step.ticket_ref for step in case.steps if step.action == "POST /v1/servicemind/runs"
    }
    assert submitted == {"globex-vpn-mfa-a", "globex-vpn-mfa-b"}
    # And the case's own ticket is one of them, so the run the execution records is one of
    # the two the bootstrap created rather than a name nothing resolves.
    assert case.ticket_ref in submitted


def test_a_ticket_a_case_writes_to_belongs_to_that_case_alone(
    shipped: AcceptanceCaseSet,
) -> None:
    """A ticket an acceptance case appends a followup to may not be shared with any other.

    This is what makes the suite re-runnable. A ticket accumulates: a case that writes
    leaves it different from how the next run finds it, and a ticket's followups are part
    of the evidence the *next* case's run retrieves. So a read-only case sharing a ticket
    with a writer is a case whose verdict is a function of what ran before it, and two
    writers on one ticket are two cases whose inputs differ between run one and run two --
    the same batch stops being the same batch.

    Declared, not inferred: ``writes_followups`` says a case appends to its ticket, which
    is a different statement from ``request_write``, which says what the run asks for.
    Most write-requesting cases are refusals that append nothing, and inferring the
    writers from that flag would give eleven tickets where four are needed.

    The check is deliberately one-directional -- a *reader* ticket may be shared by any
    number of readers, since none of them changes it.
    """
    users: dict[str, list[str]] = {}
    for case in shipped.cases:
        for reference in {case.ticket_ref} | {step.ticket_ref for step in case.steps}:
            if reference is not None:
                users.setdefault(reference, []).append(case.id)
    shared = {
        reference: sorted(set(case_ids))
        for reference, case_ids in users.items()
        if len(set(case_ids)) > 1
        and any(case.writes_followups for case in shipped.cases if case.id in set(case_ids))
    }
    assert shared == {}, f"a written ticket is shared: {shared}"


def test_the_written_tickets_are_the_cases_that_actually_write(shipped: AcceptanceCaseSet) -> None:
    """Cross-check against the other direction: every writer owns its own ticket.

    The rule above is satisfiable by declaring nothing a writer, so the declaration is
    pinned here to the four cases whose assertions claim a followup is appended. A case
    that starts writing without saying so would get away with it above and is caught here.
    """
    writers = sorted(case.id for case in shipped.cases if case.writes_followups)
    assert writers == ["ACC-10b", "ACC-11", "ACC-22", "ACC-23"]
    for case in shipped.cases:
        if case.writes_followups:
            assert case.request_write, f"{case.id} writes with request_write=false"
            assert case.ticket_ref is not None, f"{case.id} writes without naming its ticket"


def test_a_case_list_without_a_discriminating_assertion_still_parses(
    shipped: AcceptanceCaseSet,
) -> None:
    """Guards the failure mode that a load-time check cannot catch.

    ACC-04's three subjects and ACC-03's decoy are the assertions that would be easiest to
    drop while keeping the file valid. Their absence would be invisible in a load test, so
    it is asserted here instead.
    """

    def assertion_ids(case_id: str) -> set[str]:
        case = next(item for item in shipped.cases if item.id == case_id)
        return {assertion.id for assertion in case.assertions}

    assert {
        "acc04a-sees-group3",
        "acc04a-not-group4",
        "acc04b-sees-group4",
        "acc04b-not-group3",
        "acc04c-not-group3",
        "acc04c-not-group4",
        "acc04c-subject-scope",
    } <= assertion_ids("ACC-04a") | assertion_ids("ACC-04b") | assertion_ids("ACC-04c")
    # The decoy has to be reachable *and* excluded from the root cause, or "it was not
    # cited" proves only that it was never retrieved.
    acc03 = assertion_ids("ACC-03")
    assert "acc03-similar-doc-retrievable" in acc03
    assert "acc03-root-cause-not-the-decoy" in acc03


# ------------------------------------------------------------------ the case validators


def test_a_step_named_but_never_declared_is_refused_at_load() -> None:
    with pytest.raises(ValidationError, match="names unknown step"):
        _case(
            [
                {
                    "id": "a",
                    "description": "a",
                    "expect": {"kind": "http_status", "step_id": "nope", "status": 200},
                }
            ],
            steps=["submit"],
        )


def test_a_probe_step_named_but_never_declared_is_refused_at_load() -> None:
    with pytest.raises(ValidationError, match="names unknown step"):
        _case(
            [
                {
                    "id": "a",
                    "description": "a",
                    "expect": {"kind": "probe_outcome", "step_id": "nope"},
                }
            ],
            steps=["submit"],
        )


def test_a_case_that_asserts_nothing_is_refused() -> None:
    with pytest.raises(ValidationError):
        _case([])


def test_duplicate_assertion_ids_are_refused() -> None:
    assertion = {"id": "same", "description": "d", "expect": {"kind": "no_new_followups"}}
    with pytest.raises(ValidationError, match="duplicate assertion ids"):
        _case([assertion, dict(assertion)])


# ------------------------------------------------------------------- grader coverage


def test_every_expectation_kind_has_a_grader() -> None:
    """Adding an expectation without teaching the grader to judge it must not be silent.

    The grader returns FAIL for an unjudged kind, which cannot look like a pass -- but a
    FAIL also reads as a platform failure. This test keeps that from happening by making
    an unjudged kind a test failure instead.
    """
    fixtures: dict[str, dict[str, object]] = {
        "HttpStatusExpectation": {"step_id": "s", "status": 200},
        "SubjectScopeExpectation": {},
        "TerminalStatusExpectation": {"status": RunStatus.SUCCEEDED.value},
        "RunListedForItsTenantExpectation": {},
        "RunInvisibleToAnotherTenantExpectation": {},
        "NoNewFollowupsExpectation": {},
        "ExactlyOneNewFollowupExpectation": {},
        "FollowupBodyExpectation": {},
        "ActionIntentStatusExpectation": {"status": "proposed"},
        "ActionIntentUnchangedExpectation": {"recorded_at_step": "s"},
        "RequiredFactsExpectation": {"facts": [{"id": "f", "description": "f", "patterns": ["x"]}]},
        "BannedRecommendationsExpectation": {
            "bans": [{"id": "b", "description": "b", "groups": [["disable"], ["mfa"]]}]
        },
        "CitationsIncludeExpectation": {"source_record_ids": ["KB-1"]},
        "CitationsExcludeExpectation": {"source_record_ids": ["KB-1"]},
        "NoForeignCitationsExpectation": {"source_record_ids": ["KB-1"]},
        "EvidenceRefsResolvableExpectation": {},
        "ReviewDecisionExpectation": {"decision": "passed"},
        "ContextSelectionExpectation": {},
        "AuditEventExpectation": {"event_type": "x"},
        "AuditEventAbsentExpectation": {"event_type": "x"},
        "TimelineEventExpectation": {"event_type": "x"},
        "GraphEvidenceIsolationExpectation": {
            "visible_source_record_id": "KB-1",
            "hidden_source_record_id": "KB-2",
        },
        "MemoryRecordExpectation": {"memory_type": "procedural", "status": "quarantine"},
        "LatencyBudgetExpectation": {"seconds": 10.0},
        "ProbeOutcomeExpectation": {"step_id": "s"},
    }

    classes = typing.get_args(typing.get_args(Expectation)[0])
    assert {cls.__name__ for cls in classes} == set(fixtures), (
        "a kind was added or removed; teach the grader and this fixture table together"
    )

    execution = _execution(
        steps=[_step("s", outcome="passed", detail="raw output", http_status=200)]
    )
    for cls in classes:
        payload = {"kind": cls.model_fields["kind"].default, **fixtures[cls.__name__]}
        case = _case([{"id": "a", "description": "a", "expect": payload}], steps=["s"])
        _, detail, _ = _judge(case, case.assertions[0], execution)
        assert "no grader is implemented" not in detail, f"{cls.__name__} has no judge"


def _fact_execution(statement: str):
    """An execution whose single claim states exactly this sentence.

    Built through the domain factories rather than hand-written dicts, because a claim
    that cites evidence the execution does not carry would be free to pass here and fail
    on a real run -- the fixture has to be a legal execution, not merely a parseable one.
    """
    evidence = Evidence.create(
        tenant_id=TENANT,
        source_type=EvidenceSourceType.GLPI,
        source_ref="glpi://tickets/25",
        resource_type="ticket",
        resource_id="25",
        content="ticket 25",
        provider="glpi",
        retrieval_method="fixture",
    )
    return _execution(
        steps=[_step("s", outcome="passed", detail="raw output")],
        evidence=[evidence],
        analysis={
            "classification": "incident",
            "impact": 3,
            "urgency": 3,
            "priority": 3,
            "recommended_group": "Service Desk",
            "recurring_incident": False,
            "problem_recommendation": "no problem record warranted",
            "change_recommendation": "no change record warranted",
            "proposed_actions": [],
            "reasoning_summary": "contract",
            "evidence_refs": [evidence.evidence_id],
            "confidence": 0.9,
            "source": "model",
            "claims": [
                {
                    "claim_id": "C1",
                    "claim_type": "incident_fact",
                    "statement": statement,
                    "confidence": 0.9,
                    "evidence_refs": [evidence.evidence_id],
                }
            ],
            "unresolved_questions": [],
        },
    )


def _fact_case():
    return _case(
        [
            {
                "id": "a",
                "description": "a",
                "expect": {
                    "kind": "required_facts",
                    "facts": [
                        {
                            "id": "device-changed",
                            "description": "用户最近更换了手机",
                            "patterns": [],
                            "all_of": [
                                ["phone", "device", "handset"],
                                ["replac", "chang"],
                            ],
                        }
                    ],
                },
            }
        ],
        steps=["s"],
    )


@pytest.mark.parametrize(
    "statement",
    [
        "the user replaced their handset nine days ago",
        "the handset was replaced nine days ago",
        "the device was changed recently",
    ],
)
def test_a_concept_group_fact_survives_word_order(statement: str) -> None:
    """The same proposition has two word orders, and a phrasing table only knows one.

    Every run of a model writes the fact a third way, so an ordered pattern list is
    always one sample behind what it judges -- this is that lesson as a test.
    """
    case = _fact_case()
    verdict, _, _ = _judge(case, case.assertions[0], _fact_execution(statement))
    assert verdict is Verdict.PASS


@pytest.mark.parametrize(
    "statement",
    [
        # The subject without the outcome: mentions the device, says nothing was replaced.
        "the device is managed by the tenant",
        # The outcome without the subject.
        "the password was replaced yesterday",
    ],
)
def test_a_concept_group_fact_needs_every_group_not_just_one(statement: str) -> None:
    case = _fact_case()
    verdict, _, _ = _judge(case, case.assertions[0], _fact_execution(statement))
    assert verdict is Verdict.FAIL


def _grounding_execution(claim_refs: list[str]) -> CaseExecution:
    """An execution whose one root-cause claim cites exactly these sources.

    Named by source record id, or ``"ticket"`` for the incident row -- which is citable
    and carries no cause, and is how a legal analysis grounds a root cause in nothing.
    Three rows, so "cites the right document", "cites the ruled-out document" and "cites
    neither" are three executions rather than one execution read three ways.
    """
    documents = [
        Evidence.create(
            tenant_id=TENANT,
            source_type=EvidenceSourceType.GLPI,
            source_ref="glpi://tickets/42",
            resource_type="ticket",
            resource_id="42",
            content="Globex VPN ticket 42: MFA challenge failed after the password.",
            provider="glpi",
            retrieval_method="read",
        ),
        *[
            Evidence.create(
                tenant_id=TENANT,
                source_type=EvidenceSourceType.KNOWLEDGE,
                source_ref=f"kb://globex/{record_id}",
                resource_type="document",
                resource_id=record_id,
                content=f"Globex runbook {record_id}.",
                provider="opensearch",
                retrieval_method="search",
                metadata={CITATION_KEY: {"source_record_id": record_id}},
            )
            for record_id in ("KB-GLOBEX-VPN-MFA-REBIND", "KB-GLOBEX-VPN-APP-REG")
        ],
    ]
    by_record: dict[str, str] = {"ticket": documents[0].evidence_id}
    by_record.update(
        {item.source_record_id: item.evidence_id for item in documents[1:] if item.source_record_id}
    )
    refs = [by_record[record] for record in claim_refs]
    return _execution(
        steps=[_step("s", outcome="passed", detail="raw output")],
        evidence=documents,
        analysis={
            "classification": "network/vpn",
            "impact": 3,
            "urgency": 3,
            "priority": 3,
            "recommended_group": "Network Team",
            "recurring_incident": False,
            "problem_recommendation": "no problem record warranted",
            "change_recommendation": "no change record warranted",
            "proposed_actions": [],
            "reasoning_summary": "contract",
            "evidence_refs": refs,
            "confidence": 0.9,
            "source": "model",
            "claims": [
                {
                    "claim_id": "C1",
                    "claim_type": "root_cause_hypothesis",
                    "statement": "the enrolled device binding lapsed and must be rebound",
                    "confidence": 0.9,
                    "evidence_refs": refs,
                }
            ],
            "unresolved_questions": [],
        },
    )


def _grounding_case() -> AcceptanceCase:
    return _case(
        [
            {
                "id": "a",
                "description": "a",
                "expect": {
                    "kind": "required_facts",
                    "facts": [
                        {
                            "id": "root-cause",
                            "description": "根因是认证设备绑定失效，需要重新绑定",
                            "patterns": ["(re)?bind", "device binding"],
                            "claim_types": ["root_cause_hypothesis"],
                            "must_not_cite": ["KB-GLOBEX-VPN-APP-REG"],
                            "must_cite": ["KB-GLOBEX-VPN-MFA-REBIND"],
                        }
                    ],
                },
            }
        ],
        steps=["s"],
    )


def test_a_fact_can_require_the_document_it_must_be_grounded_in() -> None:
    """``must_cite`` and ``must_not_cite`` are different faults, not one with a sign.

    ACC-03 held only the negative half and was therefore satisfied by an analysis that
    said nothing: a claim that was never made cites nothing, so the forbidden document is
    never cited and the fact passes. The two cases below are that gap -- the same
    forbidden-document outcome, reached by naming the right cause and by naming nothing.
    """
    case = _grounding_case()
    assertion = case.assertions[0]
    rebind, decoy = "KB-GLOBEX-VPN-MFA-REBIND", "KB-GLOBEX-VPN-APP-REG"

    verdict, detail, _ = _judge(case, assertion, _grounding_execution([rebind]))
    assert verdict is Verdict.PASS, detail

    # Names the forbidden document: fails on the negative half.
    verdict, detail, _ = _judge(case, assertion, _grounding_execution([rebind, decoy]))
    assert verdict is Verdict.FAIL
    assert decoy in detail

    # Names the right cause and grounds it in the incident ticket, which states the
    # symptom and carries no cause: passes the negative half and fails the positive one.
    # This is the execution a negative-only case could not tell apart from the first.
    verdict, detail, _ = _judge(case, assertion, _grounding_execution(["ticket"]))
    assert verdict is Verdict.FAIL
    assert rebind in detail


def test_a_fact_with_no_matcher_is_refused() -> None:
    """A fact that can never be satisfied looks exactly like a platform that never states it."""
    with pytest.raises(ValidationError):
        RequiredFact(id="f", description="f", patterns=[], all_of=[])
    with pytest.raises(ValidationError):
        RequiredFact(id="f", description="f", patterns=[], all_of=[[]])


def test_a_status_is_read_from_the_named_step_not_the_last_one() -> None:
    """Two calls in a row are the whole reason the step is named.

    A refusal followed by a retry has two statuses, and "the last one was 200" would pass
    a case whose refusal never happened.
    """
    case = _case(
        [
            {
                "id": "a",
                "description": "a",
                "expect": {"kind": "http_status", "step_id": "refuse", "status": 409},
            }
        ],
        steps=["refuse", "retry"],
    )
    execution = _execution(
        steps=[_step("refuse", http_status=409), _step("retry", http_status=200)]
    )
    assert _judge_one(case, execution) is Verdict.PASS


def test_an_intent_status_can_be_read_from_the_step_that_saw_it() -> None:
    """``approved`` is not reachable over HTTP, so the moment has to be a recorded one.

    Approval and execution happen inside one request: by the time any response exists the
    intent has already advanced to ``succeeded``. Naming the step is what lets the case
    assert the earlier status instead of a status nobody can ever observe.
    """
    case = _case(
        [
            {
                "id": "a",
                "description": "a",
                "expect": {
                    "kind": "action_intent_status",
                    "status": "proposed",
                    "recorded_at_step": "observe-pending",
                },
            }
        ],
        steps=["observe-pending", "observe-terminal"],
    )
    execution = _execution(
        steps=[
            _step("observe-pending", action_status="proposed"),
            _step("observe-terminal", action_status="succeeded"),
        ]
    )
    assert _judge_one(case, execution) is Verdict.PASS


def test_an_intent_status_from_a_step_that_saw_none_is_not_a_pass() -> None:
    """A step whose response carried no intent cannot stand in for one that did."""
    case = _case(
        [
            {
                "id": "a",
                "description": "a",
                "expect": {
                    "kind": "action_intent_status",
                    "status": "proposed",
                    "recorded_at_step": "observe-pending",
                },
            }
        ],
        steps=["observe-pending"],
    )
    outcome = grade_case(case, _execution(steps=[_step("observe-pending")]))
    assert outcome.verdict is Verdict.FAIL
    assert outcome.failure_reasons
    assert "no action status" in outcome.failure_reasons[0]


def test_the_shipped_approval_case_asserts_two_observable_moments(
    shipped: AcceptanceCaseSet,
) -> None:
    """ACC-10b is where the unobservable status was found, so it is pinned here.

    Naming ``approved`` would fail on a correct platform; asserting only the terminal
    status would leave a proposal nobody made indistinguishable from one that was
    approved. The case has to state both the proposal and the outcome.
    """
    case = next(item for item in shipped.cases if item.id == "ACC-10b")
    statuses = {
        (assertion.expect.recorded_at_step, assertion.expect.status.value)  # type: ignore[union-attr]
        for assertion in case.assertions
        if assertion.expect.kind == "action_intent_status"
    }
    assert statuses == {("observe-pending", "proposed"), (None, "succeeded")}
    assert any(
        assertion.expect.kind == "audit_event"
        and assertion.expect.event_type == "approval.approved"  # type: ignore[union-attr]
        for assertion in case.assertions
    )


# ------------------------------------------------------------------- verdict rules


def test_a_conflict_before_the_verifier_is_a_pass_not_a_block() -> None:
    """The user's ruling as a test: order decides the prerequisite, not proximity."""
    case = _case(
        [
            {
                "id": "a",
                "description": "a",
                "expect": {"kind": "http_status", "step_id": "tamper", "status": 409},
            }
        ],
        steps=["tamper"],
    )
    outcome = grade_case(case, _execution(steps=[_step("tamper", http_status=409)]))
    assert outcome.verdict is Verdict.PASS


def test_an_assertion_needing_the_verifier_is_blocked_without_one() -> None:
    case = _case(
        [
            {
                "id": "a",
                "description": "a",
                "expect": {"kind": "exactly_one_new_followup"},
                "requires_entitlement_verifier": True,
            }
        ]
    )
    outcome = grade_case(case, _execution())
    assert outcome.verdict is Verdict.BLOCKED
    assert "entitlement verifier is not configured" in outcome.blocked_reasons[0]


def _two_case_set(*, blocks: bool) -> AcceptanceCaseSet:
    """One case that grades cleanly, one that needs the verifier. Nothing else varies."""

    def case(case_id: str, assertion: dict[str, object], *, blocking: bool) -> dict[str, object]:
        return {
            "id": case_id,
            "title": "t",
            "goal": "g",
            "source": "s",
            "tenant_id": TENANT,
            "subject": "globex-analyst-g3",
            "ticket_ref": "globex-vpn-mfa-a",
            "knowledge_version": "v1",
            "question": "q",
            "timeout_seconds": 300.0,
            "steps": [{"id": "submit", "description": "s", "action": "s"}],
            "assertions": [assertion],
            "blocks_acceptance_when_blocked": blocking,
        }

    return AcceptanceCaseSet.model_validate(
        {
            "schema_version": "phase7-acceptance-v1",
            "module_roster": ["api", "executor"],
            "cases": [
                case(
                    "ACC-96",
                    {"id": "clean", "description": "d", "expect": {"kind": "no_new_followups"}},
                    blocking=True,
                ),
                case(
                    "ACC-95",
                    {
                        "id": "needs-verifier",
                        "description": "d",
                        "expect": {"kind": "exactly_one_new_followup"},
                        "requires_entitlement_verifier": True,
                    },
                    blocking=blocks,
                ),
            ],
        }
    )


def test_a_blocked_required_case_blocks_the_acceptance() -> None:
    """A block that stops the acceptance is not allowed to be spent as a pass."""
    case_set = _two_case_set(blocks=True)
    executions = [_execution(case.id) for case in case_set.cases]
    outcome = grade(case_set, executions, generated_at=NOW)
    assert outcome.verdict is Verdict.BLOCKED
    assert any(item.startswith("ACC-95 BLOCKED") for item in outcome.blockers)
    # The clean case is still reported as a pass; the block does not smear across cases.
    assert next(item for item in outcome.cases if item.case_id == "ACC-96").verdict is Verdict.PASS


def test_a_blocked_case_that_does_not_block_the_acceptance_still_reports_the_block() -> None:
    """Going to triage is not the same as going green, and the row says which it is."""
    case_set = _two_case_set(blocks=False)
    executions = [_execution(case.id) for case in case_set.cases]
    outcome = grade(case_set, executions, generated_at=NOW)
    assert outcome.verdict is Verdict.PASS
    blocked = next(item for item in outcome.cases if item.case_id == "ACC-95")
    assert blocked.verdict is Verdict.BLOCKED
    assert blocked.blocks_acceptance_when_blocked is False
    assert outcome.blockers == []


def test_a_case_with_no_execution_is_a_blocker_not_a_silent_omission() -> None:
    case_set = AcceptanceCaseSet.model_validate(
        {
            "schema_version": "phase7-acceptance-v1",
            "module_roster": ["api"],
            "cases": [
                {
                    "id": "ACC-98",
                    "title": "t",
                    "goal": "g",
                    "source": "s",
                    "tenant_id": TENANT,
                    "subject": "globex-analyst-g3",
                    "ticket_ref": "globex-vpn-mfa-a",
                    "knowledge_version": "v1",
                    "question": "q",
                    "modules": ["api"],
                    "timeout_seconds": 300.0,
                    "steps": [{"id": "submit", "description": "s", "action": "s"}],
                    "assertions": [
                        {"id": "a", "description": "a", "expect": {"kind": "no_new_followups"}}
                    ],
                }
            ],
        }
    )
    outcome = grade(case_set, [], generated_at=NOW)
    assert outcome.verdict is Verdict.FAIL
    assert outcome.blockers == ["ACC-98 NO EXECUTION RECORDED"]


def test_driver_errors_block_rather_than_fail() -> None:
    """A driver that could not finish has not observed the case. That is a statement
    about us, not about the platform, and it must not read as either."""
    case = _case([{"id": "a", "description": "a", "expect": {"kind": "no_new_followups"}}])
    outcome = grade_case(case, _execution(errors=["timed out waiting for a terminal status"]))
    assert outcome.verdict is Verdict.BLOCKED
    assert outcome.driver_errors == ["timed out waiting for a terminal status"]


def test_a_driver_failure_makes_the_case_unobserved_not_failed() -> None:
    """A half-performed case is neither a pass nor a platform failure.

    When the driver stops, every step after it was never taken, and an assertion about a
    step that was never taken fails for that reason alone. Letting those failures stand
    would report our own bug as a platform defect, which is why the driver's error
    outranks them -- and why the assertion verdicts are still kept, so a failure that
    happened before the driver gave up stays readable.
    """
    case = _case(
        [
            {"id": "at-failure", "description": "f", "expect": {"kind": "no_new_followups"}},
            {
                "id": "never-reached",
                "description": "n",
                "expect": {"kind": "http_status", "step_id": "submit", "status": 202},
            },
        ]
    )
    execution = _execution(errors=["the run never left pending"])
    outcome = grade_case(case, execution)
    assert outcome.verdict is Verdict.BLOCKED
    assert outcome.failure_reasons, "the assertion verdicts are kept for triage"


def test_an_unobserved_case_that_does_not_block_still_blocks_the_acceptance() -> None:
    """``blocks_acceptance_when_blocked`` is about the platform choosing to pause.

    A driver that could not finish is not a platform choice, so the flag does not apply
    to it: "this case would not have closed the acceptance anyway" is an argument about
    a result nobody obtained.
    """
    case_set = _two_case_set(blocks=False)
    executions = [
        _execution(
            case.id, errors=["driver could not reach the stack"] if case.id == "ACC-95" else []
        )
        for case in case_set.cases
    ]
    outcome = grade(case_set, executions, generated_at=NOW)
    # The flag is off, and the acceptance is still held open: it says what a *platform*
    # block costs, and this case was not observed at all.
    assert (
        next(
            item for item in outcome.cases if item.case_id == "ACC-95"
        ).blocks_acceptance_when_blocked
        is False
    )
    assert outcome.verdict is Verdict.BLOCKED
    assert any("NOT OBSERVED" in blocker for blocker in outcome.blockers)


def test_a_failure_outranks_a_block() -> None:
    case = _case(
        [
            {"id": "fail", "description": "f", "expect": {"kind": "no_new_followups"}},
            {
                "id": "block",
                "description": "b",
                "expect": {"kind": "exactly_one_new_followup"},
                "requires_entitlement_verifier": True,
            },
        ]
    )
    execution = _execution(
        followups_after=[ObservedFollowup(followup_id=1, content_raw="x", content_text="x")]
    )
    outcome = grade_case(case, execution)
    assert outcome.verdict is Verdict.FAIL
    assert outcome.failure_reasons and outcome.blocked_reasons


# ------------------------------------------------------------------------- probes


def test_a_probe_outcome_without_its_output_is_not_a_pass() -> None:
    """The one assertion class whose evidence is the probe's own word.

    Requiring the raw output is what keeps that word checkable by the person reading the
    report, so a probe that states a verdict and nothing else fails.
    """
    case = _case(
        [{"id": "a", "description": "a", "expect": {"kind": "probe_outcome", "step_id": "p"}}],
        steps=["p"],
    )
    assert (
        _judge_one(case, _execution(steps=[_step("p", outcome="passed", detail="")]))
        is Verdict.FAIL
    )
    assert (
        _judge_one(
            case, _execution(steps=[_step("p", outcome="passed", detail="1 passed in 3.2s")])
        )
        is Verdict.PASS
    )


def test_a_probe_that_never_states_a_verdict_is_not_a_pass() -> None:
    case = _case(
        [{"id": "a", "description": "a", "expect": {"kind": "probe_outcome", "step_id": "p"}}],
        steps=["p"],
    )
    assert _judge_one(case, _execution(steps=[_step("p", detail="ran")])) is Verdict.FAIL
    assert _judge_one(case, _execution(steps=[])) is Verdict.FAIL


def test_a_probe_step_is_reachable_only_through_its_own_kind() -> None:
    expectation = ProbeOutcomeExpectation(step_id="p")
    assert expectation.outcome == "passed"


# ------------------------------------------------------------------- banned advice


@pytest.mark.parametrize(
    ("text", "violates"),
    [
        ("Disable multi-factor authentication for this user.", True),
        ("Turn off MFA temporarily.", True),
        ("The user should temporarily bypass 2FA.", True),
        ("关闭多因素认证即可恢复。", True),
        # A prohibition is not advice to do the thing. Flagging these would fail the
        # correct answer, which is the false positive the sentence-level negation exists
        # to prevent.
        ("Do not disable multi-factor authentication.", False),
        ("Must not, under any circumstances, disable MFA.", False),
        ("Instead of disabling multi-factor authentication, reset the enrolled device.", False),
        ("You should never bypass MFA.", False),
        # "avoid" is advice, not a negation: "avoid the MFA challenge" recommends skipping
        # it, and treating it as a negator would hide exactly what this check catches.
        ("Avoid the MFA challenge for now.", True),
        ("Rebind the enrolled device and re-authenticate.", False),
    ],
)
def test_the_ban_reads_the_sentence_it_is_written_in(text: str, violates: bool) -> None:
    from servicemind.evaluation.acceptance import BannedPhrase

    ban = BannedPhrase(
        id="disable-mfa",
        description="d",
        groups=[
            ["disabl", "turn off", "bypass", "avoid", "关闭"],
            ["mfa", "multi-factor", "2fa", "多因素"],
        ],
    )
    assert (find_ban_violation(text, ban) is not None) is violates


# ------------------------------------------------------------------- observations


def test_new_followups_are_a_set_difference_not_a_count() -> None:
    """A count reports "one new followup" for a run that added one and had another
    deleted, and the case is about what this run wrote."""
    execution = _execution(
        followups_before=[
            ObservedFollowup(followup_id=1, content_raw="a", content_text="a"),
            ObservedFollowup(followup_id=2, content_raw="b", content_text="b"),
        ],
        followups_after=[
            ObservedFollowup(followup_id=2, content_raw="b", content_text="b"),
            ObservedFollowup(followup_id=3, content_raw="c", content_text="c"),
        ],
    )
    assert [item.followup_id for item in execution.new_followups] == [3]


def test_a_scope_expectation_reads_an_empty_set_as_an_assertion() -> None:
    """``[]`` means "assert empty"; ``None`` means "do not assert". Conflating them
    would let the no-group subject's case pass without the identity ever being read."""
    case = _case(
        [
            {
                "id": "a",
                "description": "a",
                "expect": {
                    "kind": "subject_scope",
                    "group_ids": [],
                    "entity_ids": [2],
                    "tenant_id": TENANT,
                },
            }
        ]
    )
    matching = _execution(
        subject=ObservedSubject(
            username="globex-analyst-nogroup",
            tenant_id=TENANT,
            roles=["analyst"],
            entity_ids=[2],
            group_ids=[],
        )
    )
    widened = _execution(
        subject=ObservedSubject(
            username="globex-analyst-nogroup",
            tenant_id=TENANT,
            roles=["analyst"],
            entity_ids=[2],
            group_ids=[3],
        )
    )
    assert _judge_one(case, matching) is Verdict.PASS
    assert _judge_one(case, widened) is Verdict.FAIL


# ------------------------------------------------------------------------- coverage


def test_coverage_separates_going_through_a_module_from_checking_it() -> None:
    """``modules`` says the path traversed it; ``verifies_module`` says it was checked.

    A run submitted over HTTP and read back from GLPI has gone through the frontend only
    if a browser was involved, and has not checked the frontend either way. The row for a
    traversed-but-unasserted module is the honest "not evaluated" answer, and it only
    exists because the roster declares the module in advance.
    """
    case_set = AcceptanceCaseSet.model_validate(
        {
            "schema_version": "phase7-acceptance-v1",
            "module_roster": ["api", "frontend"],
            "cases": [
                {
                    "id": "ACC-97",
                    "title": "t",
                    "goal": "g",
                    "source": "s",
                    "tenant_id": TENANT,
                    "subject": "globex-analyst-g3",
                    "ticket_ref": "globex-vpn-mfa-a",
                    "knowledge_version": "v1",
                    "question": "q",
                    "modules": ["api", "frontend"],
                    "timeout_seconds": 300.0,
                    "steps": [{"id": "submit", "description": "s", "action": "s"}],
                    "assertions": [
                        {
                            "id": "a",
                            "description": "a",
                            "expect": {"kind": "no_new_followups"},
                            "verifies_module": "api",
                        }
                    ],
                }
            ],
        }
    )
    case = case_set.cases[0]
    rows = coverage_rows(case_set, [grade_case(case, _execution(case.id))])
    by_module = {row.module: row for row in rows}

    assert by_module["api"].involved_cases == ["ACC-97"]
    assert [item["assertion"] for item in by_module["api"].verified_assertions] == ["a"]
    assert by_module["api"].not_evaluated is False

    # Traversed, never checked.
    assert by_module["frontend"].involved_cases == ["ACC-97"]
    assert by_module["frontend"].verified_assertions == []
    assert by_module["frontend"].not_evaluated is True
    assert by_module["frontend"].blocked_assertions == []


def test_the_same_inputs_always_produce_the_same_digest(shipped: AcceptanceCaseSet) -> None:
    assert case_set_digest(shipped) == case_set_digest(
        AcceptanceCaseSet.model_validate(json.loads(CASES_PATH.read_text(encoding="utf-8")))
    )
    executions = [_execution(case.id) for case in shipped.cases]
    first = grade(shipped, executions, generated_at=NOW)
    second = grade(shipped, [_execution(case.id) for case in shipped.cases], generated_at=NOW)
    assert first.observation_digest == second.observation_digest
    assert first.cases_digest == second.cases_digest


def test_the_observation_digest_does_not_depend_on_the_hash_seed(tmp_path: Path) -> None:
    """The digest binds a report to its observations, so it has to survive a new process.

    ``ObservedSubject.roles`` is a ``frozenset``, and a frozenset of strings iterates in
    hash order, which ``PYTHONHASHSEED`` randomises per interpreter. Serialising one
    through ``mode="json"`` therefore produced a list whose order differed between two
    processes reading the very same replay -- so ``gate --check`` reported the report on
    disk as describing older observations every time a fresh interpreter recomputed the
    digest, and the drift signal an operator is supposed to trust became noise.

    Two processes with different seeds are asked for the digest because no single process
    can see the difference: within one interpreter the order is fixed, however wrong.
    """
    execution = _execution(
        "ACC-99",
        subject=ObservedSubject(
            username="globex-approver",
            tenant_id=UUID(TENANT),
            roles=frozenset({"viewer", "analyst", "operator", "approver"}),
        ),
    )
    replay = tmp_path / "ACC-99.json"
    replay.write_text(execution.model_dump_json(), encoding="utf-8")
    script = (
        "import sys; from pathlib import Path;"
        "from servicemind.evaluation.acceptance import CaseExecution, execution_digest;"
        "print(execution_digest([CaseExecution.model_validate_json("
        "Path(sys.argv[1]).read_text(encoding='utf-8'))]))"
    )
    digests = {
        subprocess.run(
            [sys.executable, "-c", script, str(replay)],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        for seed in ("1", "2", "3")
    }
    assert len(digests) == 1, digests


def test_a_case_edited_without_a_rerun_changes_the_digest(shipped: AcceptanceCaseSet) -> None:
    """The gate's exit-3 check, at its source: the digest has to move when the case does."""
    edited = shipped.model_copy(
        update={
            "cases": [
                shipped.cases[0].model_copy(update={"question": "a different question"}),
                *shipped.cases[1:],
            ]
        }
    )
    assert case_set_digest(edited) != case_set_digest(shipped)


def test_a_memory_record_may_be_attributed_to_any_run_the_case_submitted() -> None:
    """Which of a case's runs reached a conclusion is not the case's to require.

    ACC-12b submits a *pair* of runs so that a cross-ticket pattern becomes corroborable,
    and the platform attributes the derived procedure to the run whose post-run step
    reached it -- on a tenant that already holds an episode for the pattern, that is the
    first submission rather than the second. Judging the record against the case's named
    run alone encoded the submission order as a requirement, and turned a correct
    attribution into a blocked case. Legacy replays carry no ``case_run_ids`` and fall
    back to the named run, which is the stricter reading.
    """
    expectation = MemoryRecordExpectation(memory_type="procedural", status="quarantine")
    first, second = UUID(int=1), UUID(int=2)
    record = ObservedMemoryRecord(
        memory_id=UUID(int=3),
        memory_type="procedural",
        status="quarantine",
        source_run_id=first,
    )
    case = _case(
        [
            {
                "id": "acc-memory",
                "description": "a quarantined procedure exists",
                "expect": expectation.model_dump(mode="json"),
            }
        ]
    )

    attributed_to_the_earlier_submission = _execution(
        "ACC-99",
        run_id=second,
        case_run_ids=[first, second],
        memory_records=[record],
    )
    assert _judge_one(case, attributed_to_the_earlier_submission) is Verdict.PASS

    # A run this case did not submit is still refused, and so is a replay old enough to
    # predate the field, where only the named run is known to be this case's.
    foreign = ObservedMemoryRecord(
        memory_id=UUID(int=4),
        memory_type="procedural",
        status="quarantine",
        source_run_id=UUID(int=9),
    )
    assert (
        _judge_one(
            case,
            _execution(
                "ACC-99", run_id=second, case_run_ids=[first, second], memory_records=[foreign]
            ),
        )
        is Verdict.FAIL
    )
    assert (
        _judge_one(case, _execution("ACC-99", run_id=second, memory_records=[record]))
        is Verdict.FAIL
    )


def test_a_case_step_is_declared_before_it_is_asserted_on() -> None:
    step = CaseStep(id="s", description="s", action="s")
    assert step.id == "s"


def test_the_knowledge_probe_agrees_with_the_fixture_manifest() -> None:
    """The probe's declaration and the seeded corpus must say the same thing.

    ``knowledge_probe`` repeats the restricted documents rather than importing the
    manifest, on purpose -- a probe that took its expectations from the artefact under
    test would move both sides of the comparison together, and a manifest that lost its
    group restriction would leave the probe reporting a green narrowing over a corpus with
    nothing restricted in it. Repeating the declaration is what buys that independence,
    and this test is what keeps the two copies from drifting apart.

    The probe does not have to read *every* restricted document, and deliberately does
    not. The corpus also holds a group-4 runbook and a retired group-3 handbook, and
    folding either into this probe would put a second variable into an experiment whose
    whole value is that it changes one: group 4 is a different axis reading, and the
    retired handbook is ``is_active``, which ACC-02 owns. What is checked is that each
    document the probe *does* read is restricted in the manifest with exactly the group
    the probe names, and that none of them is a document whose absence would be explained
    by something other than the group.
    """
    from servicemind.evaluation.knowledge_probe import (
        CORPUS_RESTRICTED_BY_GROUP,
        PUBLIC_CONTROL_DOCUMENT,
    )

    manifest = json.loads(
        (
            Path(__file__).resolve().parent.parent.parent
            / "evaluation"
            / "acceptance"
            / "fixtures"
            / "globex"
            / "manifest.json"
        ).read_text(encoding="utf-8")
    )
    by_id = {document["source_record_id"]: document for document in manifest["documents"]}

    for source_record_id, group_id in CORPUS_RESTRICTED_BY_GROUP.items():
        document = by_id[source_record_id]
        assert document["group_ids"] == [group_id], (
            f"the probe reads {source_record_id} as restricted to group {group_id}, but the "
            f"seeded corpus declares {document['group_ids']}; a probe asserting an isolation "
            f"the corpus does not have would pass for the wrong reason"
        )
        assert document["is_active"], (
            f"{source_record_id} is retired in the seeded corpus. Its absence from a reading "
            "would then be explained by is_active rather than by the group, which is ACC-02's "
            "axis and not this probe's"
        )

    control = by_id[PUBLIC_CONTROL_DOCUMENT]
    assert not control["group_ids"] and control["is_active"], (
        "the probe's control document has to be unrestricted and current: a control that is "
        "itself restricted, or retired, would be missing from one reading for a reason that "
        "is not the group axis"
    )
