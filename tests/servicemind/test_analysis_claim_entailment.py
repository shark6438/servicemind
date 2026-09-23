"""The Analyst is told to claim only what its evidence entails.

Live regression, 2026-09-22, GLPI ticket 17 (VPN gateway certificate 12 days from
expiry). Two retrieval rounds and eleven evidence items produced an analysis the
Reviewer could not pass::

    C6 overreaches: the runbook says Network Team owns gateway/connectivity faults,
    but it does not establish that certificate renewal is a Network Team
    responsibility, and the group evidence only shows Network Team exists (id=1) --
    it does not validate the assignment.

    C8 asserts the knowledge example is 'generic' and conflicts with a tenant 45-day
    threshold policy -- no evidence states a tenant policy exists.

    C10's 'no recurring pattern' conclusion is an absence-of-evidence inference, not
    entailed.

Every one of those was the analysis obeying its instruction to "create claim-level
citations for classification, priority, assignment and actions" without being told
what makes a claim citable in the first place. The Reviewer's contract is entailment;
the Analyst was never given it, so it produced coverage instead of grounding -- and
the run abstained with a correct answer nobody could rely on.

The fix is the instruction, not the gate: claims that a group directory alone cannot
support are dropped, and the gap moves to ``assumptions``/``unresolved_questions``,
which is exactly what those fields are for.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import get_args
from uuid import UUID, uuid4

import pytest

import servicemind.agents.analysis as analysis_module
from servicemind.agents.analysis import AnalysisAgent
from servicemind.domain.analysis import (
    CLAIM_TYPE_BAR,
    CLAIM_TYPE_BAR_TEXT,
    AnalysisClaim,
    AnalysisResult,
    AnalysisStatus,
)
from servicemind.domain.evidence import Evidence, EvidenceSourceType, join_evidence
from servicemind.runtime.contracts import AgentInvocationContext

TENANT = UUID("11111111-1111-4111-8111-111111111111")


def joined_evidence():
    items = [
        Evidence.create(
            tenant_id=TENANT,
            source_type=EvidenceSourceType.GLPI,
            source_ref="glpi://tickets/17",
            resource_type="ticket",
            resource_id="17",
            content="VPN 网关证书剩余有效期 12 天，低于 45 天告警阈值。",
            provider="glpi",
            retrieval_method="api",
        ),
        Evidence.create(
            tenant_id=TENANT,
            source_type=EvidenceSourceType.GLPI,
            source_ref="glpi://groups/1",
            resource_type="support_group",
            resource_id="1",
            content="GLPI support group: Network Team",
            provider="glpi",
            retrieval_method="api",
        ),
    ]
    return join_evidence(TENANT, items)


def analysis_citing_all() -> AnalysisResult:
    refs = list(joined_evidence().evidence_refs)
    return AnalysisResult(
        classification="security/certificate",
        impact=3,
        urgency=4,
        priority=4,
        recommended_group="Network Team",
        recurring_incident=False,
        problem_recommendation="No problem record is warranted.",
        change_recommendation="Renew the gateway certificate through the automation.",
        reasoning_summary="The certificate is inside the alert window; renewal is due.",
        evidence_refs=refs,
        confidence=0.7,
        source="model",
        status=AnalysisStatus.MODEL,
        claims=[
            AnalysisClaim(
                claim_id="C1",
                claim_type="incident_fact",
                statement="The gateway certificate has 12 days of validity left.",
                evidence_refs=refs,
                confidence=0.7,
            )
        ],
    )


class RecordingRunnable:
    def __init__(self, result: AnalysisResult) -> None:
        self.result = result
        self.messages: list[list[object]] = []

    async def ainvoke(self, messages):
        self.messages.append(list(messages))
        return self.result


def invocation() -> AgentInvocationContext:
    return AgentInvocationContext(
        run_id=uuid4(),
        tenant_id=TENANT,
        user_id="analyst-1",
        task_id="T4",
        trace_id="trace-1",
        deadline=datetime.now(UTC) + timedelta(minutes=5),
        allowed_capabilities=frozenset({"glpi.read.ticket"}),
        max_model_calls=2,
        max_tool_calls=6,
        policy_version="test-policy-v2",
    )


@pytest.mark.asyncio
async def test_the_analysis_prompt_states_the_entailment_contract(monkeypatch) -> None:
    """The judge rejects unsupported claims; the analyst has to be told that rule."""
    runnable = RecordingRunnable(analysis_citing_all())
    monkeypatch.setattr(analysis_module, "structured_output", lambda model, schema: runnable)

    await AnalysisAgent(model_factory=lambda: object()).run(
        invocation=invocation(),
        evidence=joined_evidence(),
        goal="Assess the VPN gateway certificate expiry",
        request_write=False,
        ticket_id=17,
    )

    prompt = str(getattr(runnable.messages[0][0], "content", ""))
    assert "Every claim must be entailed by the evidence it cites" in prompt
    # The three live failure shapes, each named so a rewrite cannot quietly drop it.
    assert "not that it owns this work" in prompt
    assert "Do not attribute a rule or threshold to this tenant" in prompt
    assert "Absence of evidence is never itself a claim" in prompt


@pytest.mark.asyncio
async def test_the_analysis_prompt_asks_for_the_claim_type_the_reviewer_grades(monkeypatch) -> None:
    """A claim type the Reviewer holds a bar for must be one the Analyst was asked for.

    Live regression, 2026-09-23, ACC-03: the question asked for a root cause, the
    correct manual was retrieved and cited, and the analysis still contained no
    ``root_cause_hypothesis`` claim. The prompt enumerated classification, priority,
    assignment and actions -- every dimension but the one the run was asked about --
    while the Reviewer separately graded that type against a cause-or-mechanism bar.
    The platform defined a claim it never requested, so the run answered the wrong
    question and the Reviewer, having no such claim to judge, passed it.

    Both halves are pinned: the instruction, and the fallback that keeps it honest.
    """
    runnable = RecordingRunnable(analysis_citing_all())
    monkeypatch.setattr(analysis_module, "structured_output", lambda model, schema: runnable)

    await AnalysisAgent(model_factory=lambda: object()).run(
        invocation=invocation(),
        evidence=joined_evidence(),
        goal="用户报告 VPN 连接失败：密码被接受之后，多因素认证这一环节失败。请判断根因。",
        request_write=False,
        ticket_id=17,
    )

    prompt = str(getattr(runnable.messages[0][0], "content", ""))
    assert "root_cause_hypothesis" in prompt
    # "Always claim a cause" would trade a silent omission for an invented one, which
    # the Reviewer rejects outright. The escape hatch is what makes the ask safe.
    assert "unresolved_questions" in prompt
    assert "instead of asserting a cause" in prompt


@pytest.mark.asyncio
async def test_the_schema_handed_to_the_analysis_model_defines_each_claim_type(monkeypatch) -> None:
    """The Analyst picks a claim type from the name unless the schema says what it means.

    Live regression, 2026-09-23, ACC-07: the Reviewer's judge marked a claim unsupported
    whose statement quotes a cited graph row word for word and keeps the attribution
    ("...and the graph states this same-CI correlation points at a shared root cause").
    Two roles, one five-word vocabulary, two private readings of it -- and the judge's
    reading won, because a listed claim is terminal. The Analyst is shown this schema on
    every run, so the reading belongs there.
    """
    runnable = RecordingRunnable(analysis_citing_all())
    monkeypatch.setattr(analysis_module, "structured_output", lambda model, schema: runnable)

    await AnalysisAgent(model_factory=lambda: object()).run(
        invocation=invocation(),
        evidence=joined_evidence(),
        goal="Assess the VPN gateway certificate expiry",
        request_write=False,
        ticket_id=17,
    )

    prompt = str(getattr(runnable.messages[0][0], "content", ""))
    # The schema reaches the model as JSON, so the bar's own quoted examples arrive
    # escaped; the bar is present exactly when its escaped form is.
    assert json.dumps(CLAIM_TYPE_BAR_TEXT)[1:-1] in prompt


@pytest.mark.asyncio
async def test_the_analysis_prompt_is_where_the_output_schema_lives(monkeypatch) -> None:
    """The prompt is the Analyst's only copy, so it has to be a complete one.

    The governed envelope used to carry a second, byte-identical copy of this schema as a
    required 2985-token ``output-schema`` item -- 28% of a 10720-token envelope. It was
    removed because the prompt already ends with it (see
    ``test_phase5_governance.py::test_no_governed_envelope_restates_a_schema_its_prompt_already_carries``,
    which pins the envelope half for both roles).

    That removal is only safe while this assertion holds: if the schema leaves the prompt,
    the Analyst is asked for ``AnalysisResult`` by a parser that will not accept anything
    else and is shown no description of it anywhere. There is no fallback -- no governed
    envelope carries an ``output-schema`` item any more, for this role or any other.
    """
    runnable = RecordingRunnable(analysis_citing_all())
    monkeypatch.setattr(analysis_module, "structured_output", lambda model, schema: runnable)

    await AnalysisAgent(model_factory=lambda: object()).run(
        invocation=invocation(),
        evidence=joined_evidence(),
        goal="Assess the VPN gateway certificate expiry",
        request_write=False,
        ticket_id=17,
    )

    prompt = str(getattr(runnable.messages[0][0], "content", ""))
    assert json.dumps(AnalysisResult.model_json_schema()) in prompt


def test_the_assignment_bar_does_not_turn_the_disclosure_it_demands_into_the_deficit() -> None:
    """A bar that punishes the hedge it asks for can only be satisfied by not hedging.

    Live regression, 2026-09-23, ACC-01. The Analyst is told (``analysis.py``) that a
    support-group directory shows a group exists, not that it owns the work, and to state
    an assignment as a recommendation with the residual uncertainty in ``assumptions``.
    Claim C9 of run ``89a6842f`` did precisely that, in the statement *and* in an
    assumption -- and the judge listed it unsupported, reasoning that "the claim itself
    concedes the directory does not establish ownership, so the assignment_reason is not
    backed by its cited evidence". The disclosure the bar demands was the conviction.

    Two readings had to go with it. "The ticket's own fields" read as the ticket's
    *assignment* field, which is the one field this scenario leaves empty, so a ticket
    that records the issue reached the service desk supported nothing; and the bar never
    said the recommendation is supported by evidence pointing at the group. What stays is
    the floor: a directory on its own is still not enough.
    """
    bar = CLAIM_TYPE_BAR["assignment_reason"]

    assert "the ticket's own record" in bar, (
        "a ticket that records where the work went states a basis"
    )
    assert "not a defect" in bar, "hedging as instructed must not itself be the deficit"
    assert "on its own shows the group exists" in bar, "the directory-only floor must survive"


def test_the_incident_fact_bar_reads_another_tickets_record_as_evidence() -> None:
    """The strictness of ``incident_fact`` is deliberate; its ambiguity was not.

    Live regression, 2026-09-23, ACC-03, run 16a6ba03. The claim the judge convicted was::

        C12  "The graph states that ticket 26 affects the same CI Globex VPN gateway and
              that 1 sibling incident hit the same CI, namely glpi://ticket/25, and that
              same-CI correlation points at a shared root cause rather than an isolated
              fault."

    and the record it cites (``ev-6a253a81b967f79e``) reads, in full: "glpi://ticket/26
    ... affects CI Globex VPN gateway ...; 1 sibling incident(s) hit the same CI:
    glpi://ticket/25 .... Same-CI correlation points at a shared root cause rather than an
    isolated fault." The claim is that record quoted verbatim with its subject named, and
    it is attributed. The judge listed it unsupported anyway, reasoning that the run is
    about ticket 25 and the cited record is ticket 26's.

    Both readings are in the bar. "For this incident" is the strict clause, and the
    attribution clause says a claim reporting what a record says is stated by that record
    -- which clause governs a cross-record claim was left to the reader. This test pins
    the resolution, not a relaxation: the strict clause and the floor under it both stay.

    The judge's reading of any *live* claim is a model's, and no assertion here can hold
    it; what is testable is that the text it is handed no longer admits both readings.
    """
    bar = CLAIM_TYPE_BAR["incident_fact"]

    assert "states it for this incident" in bar, "the strict clause is the point of this bar"
    assert "provided the claim attributes it" in bar, "attribution is what makes it citable"
    assert "may be another ticket's or a runbook's" in bar, (
        "a cited record other than this ticket's own must be readable as evidence"
    )
    assert '"for this incident" asks what the record is about' in bar, (
        "the clause that decides which reading governs has to be the stated one"
    )
    assert "does not carry is unsupported" in bar, "the floor under the bar must survive"


def test_every_claim_type_the_analysis_may_emit_has_a_stated_bar() -> None:
    """A type added to the literal without a bar would be graded by a rule nobody wrote."""
    declared = set(get_args(AnalysisClaim.model_fields["claim_type"].annotation))

    assert declared == set(CLAIM_TYPE_BAR), (
        "every claim type needs exactly one bar entry: a type the Reviewer grades by a "
        "rule the Analyst was never shown is the disagreement this contract exists to stop"
    )
