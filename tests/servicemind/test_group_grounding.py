"""Owning-team grounding: a name has to appear in what the run retrieved (2026-09-24).

The Analysis Agent and the Reviewer both have to reject an incident assignment the
run invented. Both used to answer that by searching the GLPI ``support_group`` rows
alone, which is a different question: the in-code runbook the Knowledge Agent serves
states that "Identity Team owns token enrollment faults", and the corpus documents
say "Owned by the Identity Team" and defer to "the Security Team's agreement", so a
recommendation taken from retrieved documentation was rejected as ungrounded. Over
the 2026-09-24 quality batch that fired on 16 of the 18 runs parked at
``waiting_review``.

Two directions are asserted here, because a check widened until nothing fails is not
a fix. A team named by a retrieved document passes; a team named by nothing is still
refused.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from servicemind.agents.analysis import AnalysisAgent
from servicemind.agents.reviewer import ReviewerAgent
from servicemind.domain.analysis import AnalysisClaim, AnalysisResult, AnalysisStatus
from servicemind.domain.evidence import (
    Evidence,
    EvidenceSourceType,
    group_is_grounded,
    join_evidence,
)
from servicemind.domain.review import ReviewDecision
from servicemind.runtime.contracts import AgentInvocationContext

TENANT = UUID("11111111-1111-4111-8111-111111111111")

#: Verbatim from ``agents/knowledge.py``'s ``rb-vpn-mfa`` runbook -- the text the
#: Knowledge Agent actually serves, and the reason "Identity Team" is a grounded
#: answer for a tenant whose GLPI directory does not contain it.
RUNBOOK = (
    "For VPN or MFA failures, verify identity-provider health, token clock skew, gateway "
    "reachability and recent authentication changes. Network Team owns gateway/connectivity "
    "faults; Identity Team owns token enrollment faults."
)


def evidence(
    source: EvidenceSourceType,
    resource_type: str,
    content: str,
    metadata: dict[str, object] | None = None,
) -> Evidence:
    return Evidence.create(
        tenant_id=TENANT,
        source_type=source,
        source_ref=f"{source.value}://test/{resource_type}",
        resource_type=resource_type,
        resource_id="2",
        content=content,
        provider="test",
        retrieval_method="fixture",
        metadata=metadata or {},
    )


def runbook_row() -> Evidence:
    """A row shaped like the one ``KnowledgeAgent`` really serves for ``rb-vpn-mfa``.

    ``degraded_rag`` is what tells the reviewer's citation gate that this knowledge is
    code-curated fallback text carrying no chunk citation by design, rather than
    enterprise RAG output that lost its citation.
    """
    return Evidence.create(
        tenant_id=TENANT,
        source_type=EvidenceSourceType.KNOWLEDGE,
        source_ref="runbook://rb-vpn-mfa",
        resource_type="runbook",
        resource_id="rb-vpn-mfa",
        content=f"VPN MFA incident triage: {RUNBOOK}",
        provider="phase3-baseline-runbooks",
        retrieval_method="deterministic_tag_match",
        confidence=1,
        metadata={"title": "VPN MFA incident triage", "degraded_rag": True},
    )


def directory_rows() -> list[Evidence]:
    return [
        evidence(EvidenceSourceType.GLPI, "support_group", "GLPI support group: Network Team"),
        evidence(EvidenceSourceType.GLPI, "support_group", "GLPI support group: Service Desk"),
    ]


def analysis_for(group: str, refs: list[str]) -> AnalysisResult:
    return AnalysisResult(
        classification="network/vpn",
        impact=3,
        urgency=3,
        priority=3,
        recommended_group=group,
        recurring_incident=False,
        problem_recommendation="Collect recurrence evidence.",
        change_recommendation="No change is supported.",
        reasoning_summary=f"{group} owns this class of fault.",
        evidence_refs=refs,
        confidence=0.85,
        source="model",
        status=AnalysisStatus.MODEL,
        claims=[
            AnalysisClaim(
                claim_id="C1",
                claim_type="assignment_reason",
                statement=f"{group} owns this class of fault.",
                evidence_refs=refs,
                confidence=0.85,
            )
        ],
    )


def invocation() -> AgentInvocationContext:
    return AgentInvocationContext(
        run_id=uuid4(),
        tenant_id=TENANT,
        user_id="analyst-1",
        task_id="T2",
        trace_id="trace-1",
        deadline=datetime.now(UTC) + timedelta(minutes=5),
        allowed_capabilities=frozenset({"glpi.read.ticket", "glpi.read.groups"}),
        max_model_calls=2,
        max_tool_calls=6,
        policy_version="test-policy-v2",
    )


# --- the predicate itself -------------------------------------------------


def test_a_name_is_matched_however_the_reader_rendered_it() -> None:
    assert group_is_grounded("Identity Team", [RUNBOOK])
    assert group_is_grounded("identity team", [RUNBOOK])
    assert group_is_grounded("Identity  Team", [RUNBOOK])
    assert group_is_grounded("Network Team", ["GLPI support group: Network Team"])


def test_a_name_nothing_retrieved_is_not_grounded() -> None:
    assert not group_is_grounded(
        "Facilities Team", [RUNBOOK, *(r.content for r in directory_rows())]
    )
    assert not group_is_grounded("", [RUNBOOK])
    assert not group_is_grounded("Identity Team", [])


def test_a_longer_name_is_not_grounded_by_a_shorter_one() -> None:
    """Matching is on the name as prose, so a name that merely contains the team is not it."""
    assert not group_is_grounded("Identity Team Lead", [RUNBOOK])


# --- the Analysis Agent's rule --------------------------------------------


def analysis_quality(group: str, items: list[Evidence]) -> AnalysisAgent:
    joined = join_evidence(TENANT, [evidence(EvidenceSourceType.GLPI, "ticket", "VPN"), *items])
    agent = AnalysisAgent(model_factory=lambda: object())
    return agent._quality(  # noqa: SLF001
        {
            "evidence": joined,
            "result": analysis_for(group, joined.evidence_refs),
            "request_write": False,
            "ticket_id": 2,
            "goal": "Analyze the VPN incident",
        }
    )


def test_analysis_accepts_a_team_a_retrieved_runbook_names() -> None:
    report = analysis_quality("Identity Team", [runbook_row()])
    assert report.passed, report.issues


def test_analysis_accepts_a_team_named_only_by_a_document_not_the_directory() -> None:
    """The measured failure: the directory was retrieved and did not contain the team."""
    report = analysis_quality(
        "Identity Team",
        [*directory_rows(), runbook_row()],
    )
    assert report.passed, report.issues


def test_analysis_still_rejects_a_team_no_evidence_names() -> None:
    report = analysis_quality(
        "Facilities Team",
        [*directory_rows(), runbook_row()],
    )
    assert not report.passed
    assert any("Recommended group" in issue for issue in report.issues)


def test_analysis_accepts_a_directory_team_when_the_directory_was_pruned() -> None:
    """``bounded_join`` ranks support_group rows last, so the directory may be absent.

    Retrieving widely used to make this rule fire on an empty string and reject every
    recommendation -- including the correct ones -- as the run's evidence grew.
    """
    report = analysis_quality(
        "Network Team",
        [runbook_row()],
    )
    assert report.passed, report.issues


# --- the Reviewer's mirror rule -------------------------------------------


#: The reviewer's coverage gate runs before its assignment gate, so every case here
#: has to carry the knowledge row that gate asks for -- otherwise the case would be
#: scoring the wrong rule.
KNOWLEDGE_ROW = runbook_row()


def reviewer_gate(group: str, items: list[Evidence], *, retrieval_round: int = 0):
    joined = join_evidence(
        TENANT, [evidence(EvidenceSourceType.GLPI, "ticket", "VPN"), KNOWLEDGE_ROW, *items]
    )
    return ReviewerAgent()._deterministic_gate(  # noqa: SLF001
        {
            "analysis": analysis_for(group, joined.evidence_refs),
            "evidence": joined,
            "request_write": False,
            "retrieval_round": retrieval_round,
            "replan_count": 0,
            "max_replans": 1,
        }
    )


def test_reviewer_passes_a_team_a_retrieved_runbook_names() -> None:
    result = reviewer_gate("Identity Team", [runbook_row()])
    assert result is None or result.decision is not ReviewDecision.RETRIEVE_MORE, (
        result.decision if result else None
    )


def test_reviewer_asks_for_more_evidence_when_no_evidence_names_the_team() -> None:
    result = reviewer_gate("Facilities Team", [*directory_rows()])
    assert result is not None
    assert result.decision is ReviewDecision.RETRIEVE_MORE
    assert result.findings[0].reason_code == "UNKNOWN_SUPPORT_GROUP"


def test_reviewer_escalates_once_the_retrieval_round_is_spent() -> None:
    result = reviewer_gate("Facilities Team", [*directory_rows()], retrieval_round=1)
    assert result is not None
    assert result.decision is ReviewDecision.ESCALATE
    assert result.findings[0].reason_code == "UNKNOWN_SUPPORT_GROUP"
