"""Analysis degradation under provider throttling (audit finding D5, 2026-09-24).

A throttle is the provider saying *when*, not *no*, but the gateway's retry loop is
bounded by the *call's* timeout -- seconds -- and a throttle window is routinely longer.
Exhausting there has a consequence far from the cause: the analysis falls back to a
deterministic draft, the reviewer escalates on ``DEGRADED_ANALYSIS``, and the run parks in
a human queue whose only answers are "accept a degraded analysis" or "cancel".

The fix moves the wait to the run's clock, which is measured in minutes, and bounds it by
three things: the provider's own hint, a ceiling on the total wait, and the run's deadline
minus the room the later stages need. These tests hold each of those bounds independently,
because a wait that is unbounded, or that spends the deadline the reviewer still needs, is
a worse failure than the one it replaces.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

import servicemind.agents.analysis as analysis_module
from servicemind.agents.analysis import (
    _ANALYSIS_DEADLINE_HEADROOM_SECONDS,
    _ANALYSIS_THROTTLE_ATTEMPTS,
    _ANALYSIS_THROTTLE_MAX_WAIT_SECONDS,
    AnalysisAgent,
)
from servicemind.domain.analysis import AnalysisClaim, AnalysisResult, AnalysisStatus
from servicemind.domain.evidence import Evidence, EvidenceSourceType, join_evidence
from servicemind.model_gateway import throttle_wait_seconds
from servicemind.runtime.contracts import AgentInvocationContext, AgentRunStatus

TENANT = UUID("11111111-1111-4111-8111-111111111111")


class RateLimitError(Exception):
    """A 429 as a provider library surfaces it: a name, and maybe a ``Retry-After``."""

    def __init__(self, message: str = "rate limited", *, retry_after: object = None) -> None:
        super().__init__(message)
        headers = {} if retry_after is None else {"retry-after": str(retry_after)}
        self.response = SimpleNamespace(headers=headers)


class TransportCrash(Exception):
    """A failure with no timing in it: re-asking the identical question is not the fix."""


class OutputParserException(Exception):
    """Langchain's parser error, shaped as the gateway catches it.

    ``llm_output`` is what the model actually sent -- ``None`` when it sent nothing at all,
    which is the case this file distinguishes from a schema violation.
    """

    def __init__(self, message: str, *, llm_output: object = None) -> None:
        super().__init__(message)
        self.llm_output = llm_output


#: A script entry meaning "the model answers with a valid analysis".
GOOD = object()


class ScriptedRunnable:
    """Answers the model call from a script; the last entry repeats forever."""

    def __init__(self, *script: object) -> None:
        self.script = list(script)
        self.calls = 0

    async def ainvoke(self, messages: object) -> object:
        outcome = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class Slept:
    """Records the waits instead of taking them, so a bounded wait is measured, not felt."""

    def __init__(self) -> None:
        self.seconds: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.seconds.append(seconds)


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
    """A draft that satisfies the quality gate, so the throttle path is the only variable."""
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


def joined_evidence():
    return join_evidence(
        TENANT,
        [
            evidence(EvidenceSourceType.GLPI, "ticket", "VPN outage"),
            evidence(EvidenceSourceType.GLPI, "support_group", "Network Team"),
        ],
    )


def invocation(*, deadline_seconds: float = 300.0) -> AgentInvocationContext:
    return AgentInvocationContext(
        run_id=uuid4(),
        tenant_id=TENANT,
        user_id="analyst-1",
        task_id="T2",
        trace_id="trace-1",
        deadline=datetime.now(UTC) + timedelta(seconds=deadline_seconds),
        allowed_capabilities=frozenset({"glpi.read.ticket", "glpi.read.groups"}),
        max_model_calls=2,
        max_tool_calls=6,
        policy_version="test-policy-v2",
    )


def install(monkeypatch, runnable: ScriptedRunnable) -> Slept:
    def replacement(model: object, schema: object) -> ScriptedRunnable:
        return runnable  # a stand-in for the provider client

    monkeypatch.setattr(analysis_module, "structured_output", replacement)
    slept = Slept()
    monkeypatch.setattr(asyncio, "sleep", slept)
    return slept


async def analyze(monkeypatch, runnable: ScriptedRunnable, *, deadline_seconds: float = 300.0):
    joined = joined_evidence()
    runnable.script = [
        model_analysis(joined.evidence_refs) if item is GOOD else item for item in runnable.script
    ]
    slept = install(monkeypatch, runnable)
    result = await AnalysisAgent(model_factory=lambda: object()).run(
        invocation=invocation(deadline_seconds=deadline_seconds),
        evidence=joined,
        goal="Analyze VPN incident",
        request_write=False,
        ticket_id=2,
    )
    return result, slept


# --- the wait is only offered for a throttle, and it is the provider's when it speaks ---


def test_a_failure_with_no_timing_in_it_is_not_waited_for() -> None:
    assert throttle_wait_seconds(TransportCrash("connection reset"), 0) is None
    assert throttle_wait_seconds(RuntimeError("OutputParserException"), 0) is None


def test_the_schedule_bounds_the_wait_when_the_provider_gives_no_hint() -> None:
    waits = [throttle_wait_seconds(RateLimitError(), attempt) for attempt in range(5)]
    assert waits == [1.0, 2.0, 4.0, 8.0, 8.0]


def test_the_providers_own_hint_wins_over_the_schedule() -> None:
    assert throttle_wait_seconds(RateLimitError(retry_after=30), 0) == 30.0
    assert throttle_wait_seconds(RateLimitError(retry_after=30), 5) == 30.0


def test_a_nil_hint_is_honoured_as_an_immediate_retry() -> None:
    """``Retry-After: 0`` means re-ask now, and must not read as "not a throttle"."""
    wait = throttle_wait_seconds(RateLimitError(retry_after=0), 0)
    assert wait == 0.0
    assert wait is not None


def test_a_header_that_is_not_a_delay_falls_back_to_the_schedule() -> None:
    """Read at an attempt where the schedule is not 1.0, so "fell back" is distinguishable.

    An unparseable header that produced exactly the schedule's *first* value would be
    indistinguishable from a header that was read and honoured, because 1.0 is both.
    """
    past = RateLimitError(retry_after="Wed, 21 Oct 2015 07:28:00 GMT")
    assert throttle_wait_seconds(past, 2) == 4.0
    assert throttle_wait_seconds(RateLimitError(retry_after="nan"), 3) == 8.0


# --- and at the run level, the wait actually rescues the answer -------------------------


@pytest.mark.asyncio
async def test_a_throttled_draft_that_answers_on_the_retry_is_not_degraded(monkeypatch) -> None:
    runnable = ScriptedRunnable(RateLimitError(retry_after=7), GOOD)
    result, slept = await analyze(monkeypatch, runnable)
    assert slept.seconds == [7.0]
    assert runnable.calls == 2
    assert result.status is AgentRunStatus.SUCCEEDED
    assert result.output.status is AnalysisStatus.MODEL
    assert result.failure_code is None
    assert result.metrics.model_calls == 1


@pytest.mark.asyncio
async def test_a_throttled_revision_is_waited_out_like_the_draft(monkeypatch) -> None:
    """The revision is the same call to the same provider; it gets the same wait."""
    joined = joined_evidence()
    unascribed = model_analysis(joined.evidence_refs).model_copy(update={"claims": []})
    runnable = ScriptedRunnable(unascribed, RateLimitError(retry_after=2), GOOD)
    result, slept = await analyze(monkeypatch, runnable)
    assert slept.seconds == [2.0]
    assert runnable.calls == 3
    assert result.status is AgentRunStatus.SUCCEEDED
    assert result.failure_code is None


@pytest.mark.asyncio
async def test_a_throttle_that_outlasts_the_wait_degrades_as_rate_limited(monkeypatch) -> None:
    runnable = ScriptedRunnable(RateLimitError(retry_after=7))
    result, slept = await analyze(monkeypatch, runnable)
    assert slept.seconds == [7.0] * (_ANALYSIS_THROTTLE_ATTEMPTS - 1)
    assert runnable.calls == _ANALYSIS_THROTTLE_ATTEMPTS
    assert result.status is AgentRunStatus.DEGRADED
    assert result.output.status is AnalysisStatus.DEGRADED
    assert result.failure_code == "ANALYSIS_RATE_LIMITED"


@pytest.mark.asyncio
async def test_a_broken_model_path_degrades_without_waiting(monkeypatch) -> None:
    """The wait is for throttling. A transport fault is not made better by being repeated."""
    runnable = ScriptedRunnable(TransportCrash("connection reset"))
    result, slept = await analyze(monkeypatch, runnable)
    assert slept.seconds == []
    assert runnable.calls == 1
    assert result.status is AgentRunStatus.DEGRADED
    assert result.failure_code == "ANALYSIS_MODEL_FAILURE"


# --- an empty completion is re-asked, without a wait and without feedback -----------------


def test_an_empty_completion_is_told_apart_from_a_schema_violation() -> None:
    """Both arrive as the same exception class; only one of them had an answer to fix."""
    from servicemind.model_gateway import model_returned_nothing

    assert model_returned_nothing(
        OutputParserException("Failed to parse AnalysisResult from completion null.")
    )
    assert model_returned_nothing(OutputParserException("no content", llm_output=""))
    assert not model_returned_nothing(
        OutputParserException("bad field", llm_output='{"classification": "x"}')
    )
    assert not model_returned_nothing(TransportCrash("connection reset"))


@pytest.mark.asyncio
async def test_a_draft_that_answers_after_one_empty_completion_is_not_degraded(monkeypatch) -> None:
    """Measured on the 2026-09-24 quality batch, case Q-194.

    The model sent nothing, the gateway's own retry and its schema-repair retry both
    replayed the question and got nothing again, and the run parked at ``waiting_review``
    with the analysis degraded. The same case answered normally on the next two runs. A
    provider that sends no answer has not rejected the question, so it gets asked again.
    """
    runnable = ScriptedRunnable(OutputParserException("completion null"), GOOD)
    result, slept = await analyze(monkeypatch, runnable)
    assert runnable.calls == 2
    assert slept.seconds == [], "there is no window to outlast, only a response that did not arrive"
    assert result.status is AgentRunStatus.SUCCEEDED
    assert result.output.status is AnalysisStatus.MODEL


@pytest.mark.asyncio
async def test_empty_completions_are_bounded_like_every_other_attempt(monkeypatch) -> None:
    """The retry is a second chance, not a loop: a provider sending nothing forever still
    has to reach a terminal answer, and the analyst's is the deterministic fallback."""
    runnable = ScriptedRunnable(OutputParserException("completion null"))
    result, _ = await analyze(monkeypatch, runnable)
    assert runnable.calls == _ANALYSIS_THROTTLE_ATTEMPTS
    assert result.status is AgentRunStatus.DEGRADED


@pytest.mark.asyncio
async def test_a_schema_violation_with_a_completion_is_not_blindly_replayed(monkeypatch) -> None:
    """The gateway already re-asks those with the violation fed back, which is a new question.

    Replaying the identical messages here would be the mistake ``_SCHEMA_REPAIR`` exists to
    prevent, so this path must not take the empty-completion branch just because the
    exception is the same class.
    """
    runnable = ScriptedRunnable(OutputParserException("bad field", llm_output='{"a": 1}'))
    result, _ = await analyze(monkeypatch, runnable)
    assert runnable.calls == 1
    assert result.status is AgentRunStatus.DEGRADED


# --- the three bounds on the wait -------------------------------------------------------


@pytest.mark.asyncio
async def test_the_wait_is_not_taken_when_the_run_is_already_at_its_deadline(monkeypatch) -> None:
    """Waiting here would hand the reviewer a run that expired while it waited."""
    runnable = ScriptedRunnable(RateLimitError(retry_after=7))
    result, slept = await analyze(
        monkeypatch, runnable, deadline_seconds=_ANALYSIS_DEADLINE_HEADROOM_SECONDS - 5.0
    )
    assert slept.seconds == []
    assert runnable.calls == 1
    assert result.failure_code == "ANALYSIS_RATE_LIMITED"


@pytest.mark.asyncio
async def test_the_wait_leaves_the_later_stages_their_room(monkeypatch) -> None:
    """With less deadline than the hint asks for, the wait shrinks to what is left over."""
    runnable = ScriptedRunnable(RateLimitError(retry_after=45))
    _, slept = await analyze(monkeypatch, runnable, deadline_seconds=20.0)
    assert len(slept.seconds) == _ANALYSIS_THROTTLE_ATTEMPTS - 1
    for wait in slept.seconds:
        assert 0.0 < wait <= 20.0 - _ANALYSIS_DEADLINE_HEADROOM_SECONDS + 1.0


@pytest.mark.asyncio
async def test_the_total_wait_is_capped(monkeypatch) -> None:
    """A provider having a bad hour must not turn one run into a stall."""
    runnable = ScriptedRunnable(RateLimitError(retry_after=45))
    _, slept = await analyze(monkeypatch, runnable)
    assert sum(slept.seconds) <= _ANALYSIS_THROTTLE_MAX_WAIT_SECONDS
    assert slept.seconds == [45.0, _ANALYSIS_THROTTLE_MAX_WAIT_SECONDS - 45.0]


@pytest.mark.asyncio
async def test_the_deadline_bound_is_not_the_providers_bound(monkeypatch) -> None:
    """Both bounds are live at once, and the tighter one decides."""
    runnable = ScriptedRunnable(RateLimitError(retry_after=3))
    _, slept = await analyze(monkeypatch, runnable)
    assert slept.seconds == [3.0, 3.0]
    assert sum(slept.seconds) < _ANALYSIS_THROTTLE_MAX_WAIT_SECONDS


def test_the_throttle_wait_is_not_read_as_a_non_throttle_anywhere(monkeypatch) -> None:
    """Source-level: the caller must branch on ``None``, not on ``<= 0.0``.

    ``0.0`` is a legitimate answer -- a provider that sends ``Retry-After: 0`` -- so a
    truthiness or sign test at the call site silently converts that throttle into a
    terminal degradation, which is the whole defect this module exists to prevent.
    """
    source = analysis_module.__file__
    assert source is not None
    text = open(source, encoding="utf-8").read()
    assert "if wait is None or attempt + 1 >= _ANALYSIS_THROTTLE_ATTEMPTS:" in text
    assert "if wait <= 0.0 or attempt + 1" not in text
