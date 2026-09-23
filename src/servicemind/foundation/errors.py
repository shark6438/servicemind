"""Bounded error text for ledgers, events and model corrections.

Any ledger that stores an error message has to cap it, and a cap that keeps only the
head keeps the wrong end. ``OutputParserException`` -- the exception every governed
structured call raises when the model's JSON does not satisfy the schema -- opens with
the *entire model completion* and states the schema violation only after it. A head-only
clip therefore records "Failed to parse AnalysisResult from completion {...}" and hides
the one line naming what was actually wrong, which is the only line that lets the run be
diagnosed without reproducing it by hand.

Those completions are long enough to hit the cap routinely, so this is not a corner case.
On ACC-07 (2026-09-23) the analysis model's completion was cut mid-sentence at 1000
characters; the run went ``degraded``, the Reviewer escalated on ``DEGRADED_ANALYSIS``,
the case failed, and the validation error that caused all of it was recorded nowhere.
"""

from __future__ import annotations

#: Room reserved for the elision marker, wide enough for any character count a single
#: message can quote.
_ELISION_MARKER_BUDGET = 40


def bounded_error_text(error: object, limit: int = 1000) -> str:
    """Serialise ``error`` to at most ``limit`` characters, keeping both ends.

    An exception object is rendered as ``ExceptionType: message``; a string is taken as
    the message. What is dropped goes from the middle, and the marker says how much.
    """
    text = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
    if len(text) <= limit:
        return text
    body = limit - _ELISION_MARKER_BUDGET
    head = body // 2
    tail = body - head
    marker = f" …[{len(text) - head - tail} characters elided] …"
    return text[:head] + marker + text[-tail:]
