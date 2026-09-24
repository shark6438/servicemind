"""Analysis citations must name evidence, and the model must be told so.

Live regression, 2026-09-22. GLPI ticket 4 ("VPN 客户端升级后无法连接") produced::

    Unknown analysis evidence references:
      ['memory:28543281-0dab-4865-a7d1-c8e91a9b2bd0', 'skill:vpn-mfa@1.0.0']

and finished ``cancelled`` -- the Analysis degraded, the Reviewer rejected with
``UNKNOWN_EVIDENCE_REFERENCE``, and the Supervisor had no path left to take.

The model had done nothing unreasonable. ``Phase5Governance.build_context`` puts
memory records into the Analysis context with ``item_id="memory:<id>"`` and
governed skills in with ``item_id="skill:<id>@<version>"``, so those are exactly
the ids it was shown; the gates, however, only accept ids from the joined
evidence set. Nothing in the prompt said which of the two it was looking at, so
any analysis that leaned on the memory or skill channels -- both of which the
governance layer deliberately switches on for Analysis -- was scored as citing
unknown evidence and the run was discarded.

The fix is the instruction, not the gate: memory and skills stay non-citable, and
the prompt and the revision feedback now both say which entries are.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

import servicemind.agents.analysis as analysis_module
from servicemind.agents.analysis import AnalysisAgent
from servicemind.domain.analysis import (
    AnalysisClaim,
    AnalysisResult,
    AnalysisStatus,
)
from servicemind.domain.evidence import Evidence, EvidenceSourceType, join_evidence
from servicemind.runtime.contracts import AgentInvocationContext, AgentRunStatus

TENANT = UUID("11111111-1111-4111-8111-111111111111")

#: The two ids the live model cited, verbatim from the captured feedback.
MEMORY_REF = "memory:28543281-0dab-4865-a7d1-c8e91a9b2bd0"
SKILL_REF = "skill:vpn-mfa@1.0.0"


def ticket_evidence() -> Evidence:
    return Evidence.create(
        tenant_id=TENANT,
        source_type=EvidenceSourceType.GLPI,
        source_ref="glpi://tickets/4",
        resource_type="ticket",
        resource_id="4",
        content="VPN 客户端升级后无法连接。办公室有线可用，家庭宽带失败。",
        provider="glpi",
        retrieval_method="api",
    )


def group_evidence() -> Evidence:
    """The quality gate requires the recommended group to be tenant-scoped evidence."""
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


def joined_evidence():
    return join_evidence(TENANT, [ticket_evidence(), group_evidence()])


def analysis_citing(refs: list[str]) -> AnalysisResult:
    """A well-formed analysis whose only fault is what it cited."""
    return AnalysisResult(
        classification="network/vpn",
        impact=3,
        urgency=4,
        priority=4,
        recommended_group="Network Team",
        recurring_incident=False,
        problem_recommendation="Collect recurrence evidence.",
        change_recommendation="No change is supported.",
        reasoning_summary="The ticket and the VPN skill together point at the client upgrade.",
        evidence_refs=refs,
        confidence=0.7,
        source="model",
        status=AnalysisStatus.MODEL,
        claims=[
            AnalysisClaim(
                claim_id="C1",
                claim_type="root_cause_hypothesis",
                statement="The upgraded client cannot reach the gateway.",
                evidence_refs=refs,
                confidence=0.6,
            )
        ],
    )


class RecordingRunnable:
    """Returns one fixed analysis and keeps the messages it was handed."""

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


def system_prompt(messages) -> str:
    return str(getattr(messages[0], "content", ""))


@pytest.mark.asyncio
async def test_citing_a_memory_or_skill_item_is_rejected_with_the_citable_rule(monkeypatch) -> None:
    """The live shape: a claim grounded in the memory or skill channel."""
    joined = joined_evidence()
    runnable = RecordingRunnable(analysis_citing([joined.evidence_refs[0], MEMORY_REF, SKILL_REF]))
    monkeypatch.setattr(analysis_module, "structured_output", lambda model, schema: runnable)

    result = await AnalysisAgent(model_factory=lambda: object()).run(
        invocation=invocation(),
        evidence=joined,
        goal="Analyze the VPN client upgrade",
        request_write=False,
        ticket_id=4,
    )

    assert result.status is AgentRunStatus.DEGRADED
    assert result.failure_code == "ANALYSIS_GROUNDING_FAILED"
    assert result.output.status is AnalysisStatus.DEGRADED
    feedback = " ".join(result.output.validation_feedback)
    # Both offending ids are named ...
    assert MEMORY_REF in feedback
    assert SKILL_REF in feedback
    # ... and the correction says what would have been citable, so the revision
    # round has something to act on instead of re-citing the same ids.
    assert "source is 'evidence'" in feedback


@pytest.mark.asyncio
async def test_citing_only_evidence_items_passes_the_same_gate(monkeypatch) -> None:
    """The control: the identical analysis, citing evidence, is accepted."""
    joined = joined_evidence()
    runnable = RecordingRunnable(analysis_citing(list(joined.evidence_refs)))
    monkeypatch.setattr(analysis_module, "structured_output", lambda model, schema: runnable)

    result = await AnalysisAgent(model_factory=lambda: object()).run(
        invocation=invocation(),
        evidence=joined,
        goal="Analyze the VPN client upgrade",
        request_write=False,
        ticket_id=4,
    )

    assert result.status is AgentRunStatus.SUCCEEDED
    assert result.output.evidence_refs == joined.evidence_refs


@pytest.mark.asyncio
async def test_the_analysis_prompt_marks_memory_and_skills_as_uncitable(monkeypatch) -> None:
    """The model can only obey a rule it was given."""
    joined = joined_evidence()
    runnable = RecordingRunnable(analysis_citing(list(joined.evidence_refs)))
    monkeypatch.setattr(analysis_module, "structured_output", lambda model, schema: runnable)

    await AnalysisAgent(model_factory=lambda: object()).run(
        invocation=invocation(),
        evidence=joined,
        goal="Analyze the VPN client upgrade",
        request_write=False,
        ticket_id=4,
    )

    prompt = system_prompt(runnable.messages[0])
    assert "governed_context" in prompt
    assert "'evidence'" in prompt
    assert "'memory'" in prompt
    assert "'skill'" in prompt
    assert "never put their item_id in evidence_refs" in prompt


@pytest.mark.asyncio
async def test_the_analysis_prompt_forbids_grounding_a_root_cause_in_what_it_ruled_out(
    monkeypatch,
) -> None:
    """A ruled-out document is not support for the cause that was named.

    ACC-03 measures this, and what it caught was not a wrong answer: the root-cause claim
    named the right condition and listed the ruled-out runbook among the refs meant to
    support it. The document says the opposite of the claim, so the claim was not entailed
    by what it cited -- the analysis was right for a reason it could not give. The reviewer
    grades the refs, so the rule has to be in the prompt the analyst reads; the case cannot
    fix it, and a case that only checked which document was *not* cited could not see it.
    """
    joined = joined_evidence()
    runnable = RecordingRunnable(analysis_citing(list(joined.evidence_refs)))
    monkeypatch.setattr(analysis_module, "structured_output", lambda model, schema: runnable)

    await AnalysisAgent(model_factory=lambda: object()).run(
        invocation=invocation(),
        evidence=joined,
        goal="Analyze the VPN client upgrade",
        request_write=False,
        ticket_id=4,
    )

    prompt = system_prompt(runnable.messages[0])
    assert "rules a candidate out is not evidence for the cause you do name" in prompt
    assert "must not appear among them" in prompt
    # And it says where the exclusion goes instead, so the model is not left with a rule
    # that forbids the only place it had to say what it considered.
    assert "in assumptions instead" in prompt


def test_the_citable_universe_is_the_joined_evidence_set() -> None:
    """Pin the asymmetry the prompt now describes, so it cannot drift silently."""
    joined = joined_evidence()
    citable = set(joined.evidence_refs)

    assert MEMORY_REF not in citable
    assert SKILL_REF not in citable
    for ref in citable:
        assert ref.startswith("ev-")
        assert len(ref) == len("ev-") + 16
        int(ref.removeprefix("ev-"), 16)
    # Content-addressed and therefore stable: an id the model was shown in one
    # round still resolves in the next, which is what makes "copy it verbatim"
    # a rule the model can actually follow.
    assert citable == set(joined_evidence().evidence_refs)
