"""Regression: a usable semantic verdict must survive prose length and a missing rating.

Captured from a live DeepSeek ``deepseek-v4-flash`` response during a VPN incident run.
The model answered with well-formed JSON -- ``finish_reason: "stop"``, all the verdict
booleans and claim ids present -- but its ``feedback`` narrative ran to 1996 characters
against a 1500-character cap, so Pydantic raised ``string_too_long``; having spent the
response on the narrative it also never emitted ``confidence``, raising ``missing``.

The reviewer caught the resulting ``OutputParserException`` and degraded to
``SEMANTIC_REVIEW_UNAVAILABLE`` at risk HIGH, which escalates to a human. Every run of
that shape therefore ended in human review -- the semantic judge had never once parsed
in this deployment -- even though the verdict it produced was entirely usable.

The narrative text below is a stand-in of the captured length; the key set, the
overrun and the absent ``confidence`` are the captured shapes.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

import servicemind.agents.reviewer as reviewer_module
from servicemind.agents.reviewer import ReviewerAgent, SemanticReview
from servicemind.domain.analysis import (
    CLAIM_TYPE_BAR,
    CLAIM_TYPE_BAR_TEXT,
    AnalysisClaim,
    AnalysisResult,
    AnalysisStatus,
)
from servicemind.domain.evidence import Evidence, EvidenceSourceType, join_evidence
from servicemind.domain.knowledge import Citation
from servicemind.domain.review import ReviewDecision, RiskLevel
from servicemind.runtime.contracts import AgentInvocationContext, AgentRunStatus

TENANT = UUID("11111111-1111-4111-8111-111111111111")

#: The length the live judge produced, against the schema's 1500-character cap.
CAPTURED_FEEDBACK_LENGTH = 1996


def _captured_verdict() -> dict:
    """A verdict shaped exactly like the captured one: long narrative, no confidence."""
    sentence = (
        "C2 overreaches the cited evidence: the ticket states office wired access works "
        "while home broadband fails, but does not establish the home-side-to-gateway "
        "path as the fault domain. "
    )
    feedback = ""
    while len(feedback) < CAPTURED_FEEDBACK_LENGTH:
        feedback += sentence
    assert len(feedback) >= CAPTURED_FEEDBACK_LENGTH
    return {
        "claims_supported": False,
        "action_consistent": True,
        "prompt_injection_detected": False,
        "contradictions": [],
        "unsupported_claim_ids": ["C2", "C4", "C5", "C6", "C7", "C8", "C9"],
        "feedback": feedback[:CAPTURED_FEEDBACK_LENGTH],
    }


def digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _citation(content: str) -> Citation:
    document_id, parent_chunk_id = uuid4(), uuid4()
    content_hash = digest(content)
    source_uri = f"knowledge://semantic/{content_hash[:12]}"
    return Citation(
        citation_id="cite-"
        + hashlib.sha256(
            json.dumps(
                [str(document_id), str(parent_chunk_id), content_hash], separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()[:16],
        document_id=document_id,
        parent_chunk_id=parent_chunk_id,
        source="test-source",
        source_uri=source_uri,
        source_record_id=f"test://{content_hash[:12]}",
        source_version="v1",
        content_hash=content_hash,
        title="Fixture runbook",
    )


def knowledge_evidence(content: str) -> Evidence:
    citation = _citation(content)
    return Evidence.create(
        tenant_id=TENANT,
        source_type=EvidenceSourceType.KNOWLEDGE,
        source_ref=citation.source_uri,
        resource_type="knowledge_parent_chunk",
        resource_id=str(citation.parent_chunk_id),
        content=content,
        provider=citation.source,
        retrieval_method="dense_bm25_rrf_cross_encoder_parent",
        metadata={"citation": citation.model_dump(mode="json")},
    )


def glpi_ticket() -> Evidence:
    return Evidence.create(
        tenant_id=TENANT,
        source_type=EvidenceSourceType.GLPI,
        source_ref="glpi://tickets/2",
        resource_type="ticket",
        resource_id="2",
        content="VPN MFA outage with broad impact",
        provider="glpi",
        retrieval_method="api",
    )


def glpi_group() -> Evidence:
    return Evidence.create(
        tenant_id=TENANT,
        source_type=EvidenceSourceType.GLPI,
        source_ref="glpi://groups/5",
        resource_type="support_group",
        resource_id="5",
        content="GLPI support group: Network Team",
        provider="glpi",
        retrieval_method="api",
    )


def _joined():
    return join_evidence(
        TENANT, [glpi_ticket(), glpi_group(), knowledge_evidence("VPN gateway runbook")]
    )


def _analysis(*, evidence_refs: list[str]) -> AnalysisResult:
    return AnalysisResult(
        classification="network/vpn",
        impact=3,
        urgency=4,
        priority=4,
        recommended_group="Network Team",
        recurring_incident=False,
        problem_recommendation="Collect recurrence evidence.",
        change_recommendation="No change supported.",
        proposed_actions=[],
        reasoning_summary="Ticket and runbook support Network Team.",
        evidence_refs=evidence_refs,
        confidence=0.8,
        source="test",
        status=AnalysisStatus.MODEL,
        claims=[
            AnalysisClaim(
                claim_id="C1",
                claim_type="root_cause_hypothesis",
                statement="The outage is caused by the VPN gateway.",
                evidence_refs=evidence_refs[:1],
                confidence=0.6,
            ),
            AnalysisClaim(
                claim_id="C2",
                claim_type="priority_reason",
                statement="Broad impact justifies urgent priority.",
                evidence_refs=evidence_refs[:1],
                confidence=0.7,
            ),
        ],
    )


class FakeRunnable:
    """Stands in for the governed gateway, which validates before returning."""

    def __init__(self, raw: dict | SemanticReview) -> None:
        self.raw = raw
        self.messages: list[list[object]] = []

    async def ainvoke(self, messages):
        self.messages.append(list(messages))
        return SemanticReview.model_validate(self.raw)


class SequencedRunnable:
    """Answers with a different verdict per call, then repeats the last one."""

    def __init__(self, *raws: dict | SemanticReview) -> None:
        self.raws = raws
        self.messages: list[list[object]] = []

    async def ainvoke(self, messages):
        self.messages.append(list(messages))
        index = min(len(self.messages) - 1, len(self.raws) - 1)
        return SemanticReview.model_validate(self.raws[index])


def _semantic_reviewer(monkeypatch, raw: dict | SemanticReview) -> ReviewerAgent:
    reviewer, _ = _recording_semantic_reviewer(monkeypatch, raw)
    return reviewer


def _recording_semantic_reviewer(
    monkeypatch, raw: dict | SemanticReview
) -> tuple[ReviewerAgent, FakeRunnable]:
    reviewer = ReviewerAgent(enable_semantic_review=True, model_factory=lambda: object())
    runnable = FakeRunnable(raw)
    monkeypatch.setattr(reviewer_module, "structured_output", lambda model, schema: runnable)
    return reviewer, runnable


def _sequenced_semantic_reviewer(
    monkeypatch, *raws: dict | SemanticReview
) -> tuple[ReviewerAgent, SequencedRunnable]:
    reviewer = ReviewerAgent(enable_semantic_review=True, model_factory=lambda: object())
    runnable = SequencedRunnable(*raws)
    monkeypatch.setattr(reviewer_module, "structured_output", lambda model, schema: runnable)
    return reviewer, runnable


def _invocation(max_model_calls: int) -> AgentInvocationContext:
    return AgentInvocationContext(
        run_id=uuid4(),
        tenant_id=TENANT,
        user_id="analyst@acme",
        task_id="T5",
        trace_id="trace-semantic-retry",
        deadline=datetime.now(UTC) + timedelta(minutes=5),
        max_model_calls=max_model_calls,
    )


def _clean(*, confidence: float | None = None) -> dict:
    """A verdict with nothing wrong except, optionally, the self-rating."""
    verdict = _captured_verdict() | {
        "claims_supported": True,
        "unsupported_claim_ids": [],
        "feedback": "Every cited claim is entailed by the evidence.",
    }
    verdict.pop("confidence", None)
    if confidence is not None:
        verdict["confidence"] = confidence
    return verdict


# ===================================================== A) the parse boundary


def test_captured_overlong_verdict_is_accepted_with_a_floor_confidence() -> None:
    verdict = SemanticReview.model_validate(_captured_verdict())

    assert len(verdict.feedback) == 1500
    assert verdict.feedback.startswith("C2 overreaches the cited evidence")
    assert verdict.feedback.endswith("…[truncated]")
    # Absent self-rating scores the floor, never a pass -- and is flagged as absent,
    # so the escalation can report "the judge did not answer" rather than "low".
    assert verdict.confidence == 0.0
    assert verdict.rating_supplied is False
    # The verdict itself is preserved verbatim.
    assert verdict.claims_supported is False
    assert verdict.unsupported_claim_ids == ["C2", "C4", "C5", "C6", "C7", "C8", "C9"]


def test_a_narrative_within_the_cap_is_not_touched() -> None:
    verdict = _captured_verdict() | {"feedback": "Short and precise.", "confidence": 0.8}

    parsed = SemanticReview.model_validate(verdict)

    assert parsed.feedback == "Short and precise."
    assert parsed.confidence == 0.8
    assert parsed.rating_supplied is True


def test_tolerance_does_not_extend_to_a_verdict_that_changes_meaning() -> None:
    # An unknown field, or a missing one that carries judgement rather than a
    # self-rating, still fails closed.
    with pytest.raises(ValidationError):
        SemanticReview.model_validate(_captured_verdict() | {"verdict": "PASSED"})
    with pytest.raises(ValidationError):
        SemanticReview.model_validate(
            {k: v for k, v in _captured_verdict().items() if k != "claims_supported"}
        )


# ===================================================== B) the adjudicator


@pytest.mark.asyncio
async def test_absent_confidence_escalates_but_keeps_the_judges_findings(monkeypatch) -> None:
    joined = _joined()
    verdict = _captured_verdict() | {
        "claims_supported": True,
        "unsupported_claim_ids": [],
        "feedback": "Every cited claim is entailed by the evidence.",
    }
    assert "confidence" not in verdict
    reviewer = _semantic_reviewer(monkeypatch, verdict)

    result = await reviewer.review(
        analysis=_analysis(evidence_refs=joined.evidence_refs),
        evidence=joined,
        request_write=False,
        retrieval_round=0,
        replan_count=0,
        max_replans=2,
    )

    assert result.degraded is False
    assert result.decision is ReviewDecision.ESCALATE
    assert result.risk_level is RiskLevel.HIGH
    assert result.confidence == 0.0
    # The judge never rated itself, so the reason code says so. Reporting LOW here
    # would tell the operator the judge was unconvinced when in fact it said nothing --
    # the run-41a18d8a shape, where every claim was supported and the escalation still
    # read as a low-confidence verdict.
    assert [finding.reason_code for finding in result.findings] == ["SEMANTIC_CONFIDENCE_MISSING"]


@pytest.mark.asyncio
async def test_a_supplied_rating_below_the_floor_is_reported_as_low_not_missing(
    monkeypatch,
) -> None:
    """The two failures share an outcome but not a cause, and must not share a code."""
    joined = _joined()
    verdict = _captured_verdict() | {
        "claims_supported": True,
        "unsupported_claim_ids": [],
        "feedback": "Every cited claim is entailed by the evidence.",
        "confidence": 0.2,
    }
    reviewer = _semantic_reviewer(monkeypatch, verdict)

    result = await reviewer.review(
        analysis=_analysis(evidence_refs=joined.evidence_refs),
        evidence=joined,
        request_write=False,
        retrieval_round=0,
        replan_count=0,
        max_replans=2,
    )

    assert result.decision is ReviewDecision.ESCALATE
    assert result.confidence == 0.2
    assert [finding.reason_code for finding in result.findings] == ["SEMANTIC_CONFIDENCE_LOW"]


# ===================================================== D) the missing-rating repair


@pytest.mark.asyncio
async def test_a_missing_rating_is_asked_for_again(monkeypatch) -> None:
    """One omission must not cost a human a run the judge actually approved.

    Live reproduction, 2026-09-22: at the shape of ticket 17's joined evidence (11 rows,
    ~19k characters) the judge returned a complete verdict -- every claim id, the summary
    boolean, the narrative -- but no ``confidence`` in 1 of 6 samples. Both live runs of
    ticket 17 land in that bucket, and each escalated to a human as
    SEMANTIC_CONFIDENCE_MISSING while their feedback read "All six claims are backed by
    their cited evidence". The judge is asked once more before the verdict is taken at
    face value.
    """
    joined = _joined()
    reviewer, runnable = _sequenced_semantic_reviewer(monkeypatch, _clean(), _clean(confidence=0.9))

    result = await reviewer.review(
        analysis=_analysis(evidence_refs=joined.evidence_refs),
        evidence=joined,
        request_write=False,
        retrieval_round=0,
        replan_count=0,
        max_replans=2,
    )

    assert len(runnable.messages) == 2, "the judge must be asked exactly once more"
    assert reviewer_module._SEMANTIC_RATING_RETRY in str(runnable.messages[1][-1].content)
    # The second verdict decides: the run proceeds instead of escalating on a field the
    # model forgot to type.
    assert result.decision is ReviewDecision.PASSED
    assert result.confidence == 0.9
    assert [finding.reason_code for finding in result.findings] == []


@pytest.mark.asyncio
async def test_a_second_omission_still_fails_closed(monkeypatch) -> None:
    joined = _joined()
    reviewer, runnable = _sequenced_semantic_reviewer(monkeypatch, _clean(), _clean())

    result = await reviewer.review(
        analysis=_analysis(evidence_refs=joined.evidence_refs),
        evidence=joined,
        request_write=False,
        retrieval_round=0,
        replan_count=0,
        max_replans=2,
    )

    assert len(runnable.messages) == 2
    assert result.decision is ReviewDecision.ESCALATE
    assert [finding.reason_code for finding in result.findings] == ["SEMANTIC_CONFIDENCE_MISSING"]


@pytest.mark.asyncio
async def test_the_retry_is_not_spent_when_one_model_call_remains(monkeypatch) -> None:
    """The repair is bounded by the reviewer's declared budget, never beyond it."""
    joined = _joined()
    reviewer, runnable = _sequenced_semantic_reviewer(monkeypatch, _clean(), _clean(confidence=0.9))

    envelope = await reviewer.run(
        invocation=_invocation(max_model_calls=1),
        analysis=_analysis(evidence_refs=joined.evidence_refs),
        evidence=joined,
        request_write=False,
        retrieval_round=0,
        replan_count=0,
        max_replans=2,
    )

    assert len(runnable.messages) == 1, "a one-call budget must not be overdrawn"
    assert envelope.metrics.model_calls == 1
    assert envelope.status is AgentRunStatus.SUCCEEDED
    assert [finding.reason_code for finding in envelope.output.findings] == [
        "SEMANTIC_CONFIDENCE_MISSING"
    ]


@pytest.mark.asyncio
async def test_longest_admissible_narrative_survives_the_review_contracts(monkeypatch) -> None:
    """A 1500-character narrative must not crash the 1000/2000-character downstream caps."""
    joined = _joined()
    verdict = _captured_verdict() | {"confidence": 0.9}
    reviewer = _semantic_reviewer(monkeypatch, verdict)

    result = await reviewer.review(
        analysis=_analysis(evidence_refs=joined.evidence_refs),
        evidence=joined,
        request_write=False,
        retrieval_round=0,
        replan_count=0,
        max_replans=2,
    )

    assert result.degraded is False
    assert result.decision is ReviewDecision.RETRIEVE_MORE
    assert result.unsupported_claims == ["C2", "C4", "C5", "C6", "C7", "C8", "C9"]
    assert len(result.feedback) <= 2000
    assert result.findings
    for finding in result.findings:
        assert len(finding.explanation) <= 1000


# ===================================================== C) the bar per claim type


@pytest.mark.asyncio
async def test_the_judge_is_told_that_the_bar_depends_on_the_claim_type(monkeypatch) -> None:
    """Advisory claims cannot be judged by the informational claims' bar.

    Live regression, 2026-09-22, GLPI ticket 17. Two rounds of the judge rejected the
    analysis with reasons that all reduced to one rule -- "the evidence does not state
    this" -- applied to ``priority_reason``, ``assignment_reason`` and
    ``recommended_action``. But ``AnalysisClaim.claim_type`` *is* that enumeration: the
    domain requires the Analyst to emit advisory claims, so a verbatim-entailment bar
    makes every correctly-formed analysis unsupportable, and the run can only abstain.
    The judge was never told the two kinds apart.
    """
    joined = _joined()
    verdict = _captured_verdict() | {"confidence": 0.9, "unsupported_claim_ids": []}
    reviewer, runnable = _recording_semantic_reviewer(monkeypatch, verdict)

    await reviewer.review(
        analysis=_analysis(evidence_refs=joined.evidence_refs),
        evidence=joined,
        request_write=False,
        retrieval_round=0,
        replan_count=0,
        max_replans=2,
    )

    prompt = str(getattr(runnable.messages[0][0], "content", ""))
    # The judge reads the text the Analyst is shown, not a paraphrase of it. Pinning the
    # literal sentence -- which this test used to do -- made every rewording of a bar a
    # failure here, so the bar could only be corrected by editing the judge's copy too,
    # which is the drift the shared text exists to prevent.
    assert CLAIM_TYPE_BAR_TEXT in prompt
    # Facts keep the strict bar ...
    assert "states it for this incident" in CLAIM_TYPE_BAR["incident_fact"]
    assert "cause or a mechanism" in CLAIM_TYPE_BAR["root_cause_hypothesis"]
    # ... and advice gets the bar advice can meet -- the strict "the evidence states it"
    # replaced by "the evidence supports following it", each type saying so in its own
    # terms -- without becoming unjudged.
    relaxations = {
        "priority_reason": "not that it repeats the recommendation",
        "assignment_reason": "need not prove the ownership",
        "recommended_action": "not that it states the recommendation",
    }
    for advisory, relaxation in relaxations.items():
        assert "supports following the advice" in CLAIM_TYPE_BAR[advisory]
        assert relaxation in CLAIM_TYPE_BAR[advisory], advisory
    # A relaxed bar is still a bar: the floor under each advice type stays.
    assert "no supporting basis is still unsupported" in CLAIM_TYPE_BAR["recommended_action"]
    assert "on its own shows the group exists" in CLAIM_TYPE_BAR["assignment_reason"]


@pytest.mark.asyncio
async def test_the_judge_prompt_is_where_the_schema_the_judge_returns_lives(monkeypatch) -> None:
    """The judge's only copy of its own output shape, now that the envelope has none.

    The governed envelope used to carry a required 1586-token ``output-schema`` item into
    the reviewer's budget -- 15% of a 10720-token envelope. It has been removed, and the
    reason it was removable is the reason this assertion has to exist: what it carried was
    ``ReviewResult``, which no model is ever asked to produce. The reviewer's one
    structured call asks for ``SemanticReview`` (``reviewer.py:632``) and the
    ``ReviewResult`` it returns is assembled in Python (``reviewer.py:259``).

    So the prompt is the only place this schema can live, and if it leaves the prompt the
    judge is asked for a shape nothing has described to it.
    """
    joined = _joined()
    verdict = _captured_verdict() | {"confidence": 0.9, "unsupported_claim_ids": []}
    reviewer, runnable = _recording_semantic_reviewer(monkeypatch, verdict)

    await reviewer.review(
        analysis=_analysis(evidence_refs=joined.evidence_refs),
        evidence=joined,
        request_write=False,
        retrieval_round=0,
        replan_count=0,
        max_replans=2,
    )

    prompt = str(getattr(runnable.messages[0][0], "content", ""))
    assert json.dumps(SemanticReview.model_json_schema()) in prompt


@pytest.mark.asyncio
async def test_the_judge_must_read_a_claim_against_the_text_before_calling_it_inverted(
    monkeypatch,
) -> None:
    """An alleged inversion has to survive being read against the passage it quotes.

    Live regression, 2026-09-23, ACC-23. The analysis restated the rebound article's own
    closing sentence -- "it states that an accepted password proves the credential is
    intact and says nothing about the factor, and that it is the factor that is failing"
    against "It says nothing about the factor, and it is the factor that is failing" --
    and the judge listed the claim for misstating the article, quoting that faithful
    clause back as the evidence of the inversion. A listed claim is terminal for the run,
    so the run abstained and never reached the write the case exists to measure: two of
    two runs wrote nothing.

    The rule is stated for every claim type rather than argued one bar at a time, because
    the failure is not about what a claim_type means -- it is about the judge reporting a
    contradiction it never checked.
    """
    joined = _joined()
    verdict = _captured_verdict() | {"confidence": 0.9, "unsupported_claim_ids": []}
    reviewer, runnable = _recording_semantic_reviewer(monkeypatch, verdict)

    await reviewer.review(
        analysis=_analysis(evidence_refs=joined.evidence_refs),
        evidence=joined,
        request_write=False,
        retrieval_round=0,
        replan_count=0,
        max_replans=2,
    )

    prompt = str(getattr(runnable.messages[0][0], "content", ""))
    assert "quote the passage that contradicts it" in prompt
    assert "the faithful one governs" in prompt
