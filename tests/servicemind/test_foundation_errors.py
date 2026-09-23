"""The ledger's error text has to keep the end that says what went wrong.

Live regression, 2026-09-23, ACC-07. The analysis model returned a completion that did
not satisfy ``AnalysisResult``; the agent degraded, the Reviewer escalated on
``DEGRADED_ANALYSIS``, and the case failed. The recorded ``failure_detail`` was::

    OutputParserException: Failed to parse AnalysisResult from completion {"classification":
    "VPN MFA failure after handset change (MFA device rebind)", "impact": 3, ... "The
    knowledge article states MFA is bound to a device secret that does not migrate when the
    phone is replaced, and that the three confirming facts are a successful password step, a
    failed challenge step, and

-- cut mid-word at 1000 characters. ``OutputParserException`` puts the entire completion
first and the schema violation last, so the clip threw away the only line that named the
cause. The run could not be diagnosed from its own record.
"""

from servicemind.foundation.errors import bounded_error_text


def test_a_bounded_error_keeps_the_end_that_states_the_cause() -> None:
    completion = '{"classification": "VPN MFA failure", "reasoning_summary": "' + "x" * 4000 + '"}'
    detail = (
        f"Failed to parse AnalysisResult from completion {completion}\n"
        "1 validation error for AnalysisResult\n"
        "claims.11.claim_type\n"
        "  Input should be 'incident_fact', 'root_cause_hypothesis', ..."
    )

    text = bounded_error_text(detail, limit=1000)

    assert len(text) <= 1000
    assert text.startswith("Failed to parse AnalysisResult from completion"), (
        "the exception's own opening is still the first thing an operator reads"
    )
    assert "1 validation error for AnalysisResult" in text[-400:], (
        "the schema violation is the line that names the cause; a head-only clip drops it"
    )
    assert len(completion) > len(text), "the completion is what got clipped, not the verdict"
    assert "characters elided" in text, "the cut has to say it cut"


def test_a_bounded_error_reports_the_exception_type_and_leaves_short_text_alone() -> None:
    assert bounded_error_text("plain message") == "plain message"
    assert bounded_error_text(ValueError("bad value")) == "ValueError: bad value"
    # Exactly at the limit is not a cut, so nothing is marked elided.
    at_limit = "y" * 1000
    assert bounded_error_text(at_limit) == at_limit


def test_a_bounded_error_is_the_one_clip_the_orchestration_layer_uses() -> None:
    """One clipper, imported rather than re-derived.

    ``supervisor_workflow`` had its own private copy of this both-ends clip while
    ``agents/analysis.py`` clipped the head -- so the same failure was diagnosable from a
    planner rejection and undiagnosable from an analysis degradation.
    """
    import inspect

    from servicemind.agents import analysis
    from servicemind.orchestration import supervisor_workflow

    workflow_source = inspect.getsource(supervisor_workflow)
    assert "bounded_error_text" in workflow_source
    assert "_REJECTION_MARKER_BUDGET" not in workflow_source, (
        "the private copy is gone, not shadowed"
    )

    analysis_source = inspect.getsource(analysis)
    assert "bounded_error_text(exc)" in analysis_source, (
        "both analysis failure paths -- draft and revision -- record the bounded message"
    )
    assert "[:1000]" not in analysis_source, (
        "a head-only slice records the model's completion and hides the validation error"
    )
    assert workflow_source.count("bounded_error_text(str(exc))") == 3, (
        "every planner-rejection detail goes through the shared clip; a raw str(exc)[:n] "
        "would record the completion and drop the cause"
    )
