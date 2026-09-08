"""Analysis Agent revision-crash accounting (audit regression, 2026-09-08).

A crash inside the *revision* model call must surface as
``ANALYSIS_REVISION_FAILURE`` with the exception name preserved -- it is not a
grounding failure. Previously the unconditional ``revise -> check`` back-edge
re-ran the quality check on the degraded draft and relabeled the outcome as a
generic ``ANALYSIS_GROUNDING_FAILED``, dropping the failure signal.
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
from servicemind.security.auth import TenantContext

TENANT = UUID("11111111-1111-4111-8111-111111111111")


def evidence(source: EvidenceSourceType, resource_type: str, content: str) -> Evidence:
    return Evidence.create(
        tenant_id=TENANT,
        source_type=source,
        source_ref=f"{source.value}://test/{resource_type}",
        resource_type=resource_type,
        resource_id="2",
        content=content,
        provider="test",
        retrieval_method="fixture",
    )


def model_analysis(refs: list[str]) -> AnalysisResult:
    return AnalysisResult(
        classification="network/vpn",
        impact=3,
        urgency=3,
        priority=3,
        recommended_group="Network Team",
        recurring_incident=False,
        problem_recommendation="Collect recurrence evidence.",
        change_recommendation="No change is supported.",
        reasoning_summary="Cited ticket and runbook support Network Team.",
        evidence_refs=refs,
        confidence=0.85,
        source="model",
        status=AnalysisStatus.MODEL,
        claims=[
            AnalysisClaim(
                claim_id="C1",
                claim_type="assignment_reason",
                statement="Network Team owns VPN incidents.",
                evidence_refs=refs,
                confidence=0.85,
            )
        ],
    )


class CrashAfterFirst:
    """Succeeds once (the draft), then raises on the revision call."""

    def __init__(self, first) -> None:
        self.first = first
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        if self.calls == 1:
            return self.first
        raise RuntimeError("revision model transport crash")


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


def tenant_context() -> TenantContext:
    return TenantContext(
        tenant_id=TENANT,
        user_id="analyst-1",
        username="analyst",
        roles={"analyst"},
        allowed_glpi_entity_ids={1},
    )


@pytest.mark.asyncio
async def test_revision_model_crash_is_reported_as_revision_failure(monkeypatch) -> None:
    joined = join_evidence(
        TENANT,
        [
            evidence(EvidenceSourceType.GLPI, "ticket", "VPN outage"),
            evidence(EvidenceSourceType.GLPI, "support_group", "Network Team"),
        ],
    )
    draft = model_analysis(joined.evidence_refs).model_copy(update={"claims": []})
    runnable = CrashAfterFirst(draft)
    monkeypatch.setattr(
        analysis_module, "structured_output", lambda model, schema: runnable
    )
    result = await AnalysisAgent(model_factory=lambda: object()).run(
        invocation=invocation(),
        evidence=joined,
        goal="Analyze VPN incident",
        request_write=False,
        ticket_id=2,
    )
    assert result.status is AgentRunStatus.DEGRADED
    assert result.failure_code == "ANALYSIS_REVISION_FAILURE"
    assert result.output.status is AnalysisStatus.DEGRADED
    assert any(
        "RuntimeError" in item for item in result.output.validation_feedback
    )
    assert result.metrics.model_calls == 2  # draft + one revision attempt
