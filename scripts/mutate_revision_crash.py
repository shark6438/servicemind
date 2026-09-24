"""Revert the revision-crash handling, and require the tests to notice (audit finding D4).

The defect this locks is a run that reported the wrong failure and kept the object that
caused it. When the *revision* model call crashed, the node kept the draft it had been
asked to repair -- a draft already known to violate the quality rules, since that is why
it was sent back -- and relabeled the result DEGRADED. Everything downstream then reasoned
about that draft: its ``evidence_refs`` are what the reviewer's citation gate resolves and
what the acceptance assertions read, so a schema failure was reported as a grounding
failure and the status check written to handle exactly this case was never reached.
Measured on the 2026-09-23 baseline (ACC-03): the one revision raised
``OutputParserException``, the unrepaired draft kept ``ev-8cec7c70e10c7b56``, and the
reviewer rejected the run for citing evidence that does not exist.

Each mutation below removes one of the two things the fix provides: the draft is not
replaced by evidence-drawn fallback output, and the crash is not reported as a revision
failure with the exception preserved.
"""

import pathlib
from pathlib import Path

from mutation_harness import Mutation, run_mutations

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXPERIMENT = "revision_crash"
ANALYSIS = ROOT / "src/servicemind/agents/analysis.py"

TEST = "tests/servicemind/test_analysis_revision_failure.py"

DRAFT_IS_NOT_REPLACED = (
    '                "result": self._fallback_evidence(\n'
    '                    state["evidence"],\n'
    '                    ticket_id=state["ticket_id"],\n'
    '                    request_write=state["request_write"],\n'
    '                    validation_feedback=[*state["quality"].issues, type(exc).__name__],\n'
    "                ),\n"
)
FEEDBACK_KEEPS_THE_EXCEPTION = (
    '                    validation_feedback=[*state["quality"].issues, type(exc).__name__],\n'
)


def _test(name: str) -> str:
    return f"{TEST}::{name}"


MUTATIONS = [
    (
        "M01 the crashed revision leaves the draft it was repairing in place",
        ANALYSIS,
        DRAFT_IS_NOT_REPLACED,
        '                "result": state["result"],\n',
        _test("test_a_crashed_revision_does_not_publish_the_draft_it_was_repairing"),
    ),
    (
        "M02 the crashed revision is relabeled as a grounding failure",
        ANALYSIS,
        '                "failure_code": "ANALYSIS_REVISION_FAILURE",\n',
        '                "failure_code": "ANALYSIS_GROUNDING_FAILED",\n',
        _test("test_revision_model_crash_is_reported_as_revision_failure"),
    ),
    (
        "M03 the crash is recorded without saying what crashed",
        ANALYSIS,
        FEEDBACK_KEEPS_THE_EXCEPTION,
        '                    validation_feedback=list(state["quality"].issues),\n',
        _test("test_revision_model_crash_is_reported_as_revision_failure"),
    ),
    (
        "M04 the crashed revision still counts against the model budget it never spent",
        ANALYSIS,
        '                "model_calls": state.get("model_calls", 0) + 1,\n'
        '                "failure_code": "ANALYSIS_REVISION_FAILURE",\n',
        '                "failure_code": "ANALYSIS_REVISION_FAILURE",\n',
        _test("test_revision_model_crash_is_reported_as_revision_failure"),
    ),
]


if __name__ == "__main__":
    raise SystemExit(
        run_mutations(
            root=ROOT,
            experiment=EXPERIMENT,
            mutations=[
                Mutation(name=name, path=Path(path), old=old, new=new, tests=(tests,))
                for name, path, old, new, tests in MUTATIONS
            ],
        )
    )
