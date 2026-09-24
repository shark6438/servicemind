"""Revert the cumulative policy feedback, and require the tests to notice.

The defect this locks is a control plane that is corrected one rule at a time and
oscillates between two of them. On ticket 26 (ACC-06, 2026-09-24) the supervisor
proposed ``dispatch`` -- refused, because after a replan the only legal actions were
``join_evidence`` and ``replan`` -- then ``join_evidence`` naming a task id, refused for
its argument, then ``dispatch`` again. The third attempt repeated the first exactly,
because by then the platform had replaced the first rejection with the second, so the
one correction it was shown came at the price of forgetting the earlier one. Three
attempts, two distinct rules, and the run ended in ``supervisor_policy_failure`` with
its plan, evidence and analysis discarded together.

Each mutation below removes one half of the fix: the rejections are no longer carried
across attempts, and the prompt no longer says that an earlier correction survives a
later one.
"""

import pathlib
from pathlib import Path

from mutation_harness import Mutation, run_mutations

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXPERIMENT = "supervisor_feedback"
WORKFLOW = ROOT / "src/servicemind/orchestration/supervisor_workflow.py"
AGENT = ROOT / "src/servicemind/agents/supervisor.py"

RUNTIME_TEST = "tests/servicemind/test_supervisor_runtime.py"
PROMPT_TEST = (
    f"{RUNTIME_TEST}::test_the_retry_prompt_carries_every_rejection_and_says_they_still_hold"
)

ACCUMULATED = (
    "                rejections.append(str(exc))\n"
    '                feedback = "\\n".join(\n'
    '                    f"{index}. {reason}" for index, reason in enumerate(rejections, 1)\n'
    "                )\n"
)
ONLY_THE_LATEST = "                feedback = str(exc)\n"

STILL_HOLDS = (
    '"Every rejection above still applies to the state you are looking at. Do not "\n'
    '            "repeat a decision that appears in this list, and do not repair one rejection "\n'
    '            "by undoing the correction made for an earlier one."'
)

MUTATIONS = [
    (
        "M01 a rejection is spent by the rejection that follows it",
        WORKFLOW,
        ACCUMULATED,
        ONLY_THE_LATEST,
        f"{RUNTIME_TEST}::test_a_rejection_is_not_spent_by_the_rejection_after_it",
    ),
    (
        "M02 the prompt lists the rejections without saying they still hold",
        AGENT,
        STILL_HOLDS,
        '"Earlier rejections are history and no longer apply."',
        PROMPT_TEST,
    ),
    (
        "M03 the prompt keeps only the newest rejection",
        AGENT,
        'f"them:\\n{policy_feedback}\\n"',
        'f"them:\\n{policy_feedback.splitlines()[-1]}\\n"',
        PROMPT_TEST,
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
