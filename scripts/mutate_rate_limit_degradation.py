"""Revert each part of the throttle wait, and require the tests to notice.

The defect this locks is a run that a provider's own answer condemned. A 429 inside the
gateway's retry loop exhausted against the *call's* timeout, the analysis fell back to a
deterministic draft, the reviewer escalated on ``DEGRADED_ANALYSIS``, and the run parked
in a queue whose only exits were "accept a degraded analysis" or "cancel" -- for a
provider that had said *when*, not *no*. Measured on the 2026-09-23 baseline: nine
throttled ``MODEL_RATE_LIMITED`` rows, zero successful retries, nine runs terminated
``waiting_review``.

Every mutation below removes one of the things that make the wait correct: that it is only
offered for a throttle, that a nil hint is an instruction rather than an absence, that it
is bounded by the provider's own clock, by a ceiling, and by the deadline the later stages
still need. Each must be caught -- an unbounded wait and a wait that spends the run's
whole deadline are both worse than the degradation they replace.
"""

import pathlib
from pathlib import Path

from mutation_harness import Mutation, run_mutations

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXPERIMENT = "rate_limit_degradation"
GATEWAY = ROOT / "src/servicemind/model_gateway/gateway.py"
ANALYSIS = ROOT / "src/servicemind/agents/analysis.py"
REVIEWER = ROOT / "src/servicemind/agents/reviewer.py"

TEST = "tests/servicemind/test_phase7_rate_limit_degradation.py"
#: The verdict half of the same defect lives with the gate it decides, and M15/M16 are
#: the two ways to get it wrong: hand a repairable runtime failure to a person, or let
#: the re-derivation run without the bound that makes it safe.
REVIEWER_TEST = "tests/servicemind/test_phase4_reviewer_citations.py"

REDERIVE = '            rederive = state["replan_count"] < state["max_replans"]\n'

WAIT_IS_NONE = "                if wait is None or attempt + 1 >= _ANALYSIS_THROTTLE_ATTEMPTS:\n"
#: The whole bound, guard included. Deleting only the body would leave ``if deadline is
#: not None:`` empty -- a syntax error, and a syntax error exits non-zero, so the harness
#: would have called it detected while no assertion had run. The replacement keeps the
#: module parseable and removes the bound, which is the mutation that has to be caught.
DEADLINE_CLAMP = (
    "                if deadline is not None:\n"
    "                    remaining = (deadline - datetime.now(UTC)).total_seconds()\n"
    "                    wait = min(wait, remaining - _ANALYSIS_DEADLINE_HEADROOM_SECONDS)\n"
)
DEADLINE_CLAMP_REMOVED = "                if False:\n                    pass\n"
TOTAL_CLAMP = "                wait = min(wait, _ANALYSIS_THROTTLE_MAX_WAIT_SECONDS - waited)\n"


def _test(name: str) -> str:
    return f"{TEST}::{name}"


MUTATIONS = [
    (
        "M01 every failure is waited for, not just a throttle",
        GATEWAY,
        '    if model_error_code(error) != "MODEL_RATE_LIMITED":\n        return None\n',
        "",
        _test("test_a_failure_with_no_timing_in_it_is_not_waited_for"),
    ),
    (
        "M02 a nil hint is read as 'not a throttle'",
        GATEWAY,
        '    if model_error_code(error) != "MODEL_RATE_LIMITED":\n        return None\n'
        "    return _backoff_seconds(error, attempt)\n",
        '    if model_error_code(error) != "MODEL_RATE_LIMITED":\n        return None\n'
        "    return _backoff_seconds(error, attempt) or None\n",
        _test("test_a_nil_hint_is_honoured_as_an_immediate_retry"),
    ),
    (
        "M03 a nil hint is collapsed into the not-a-throttle answer",
        GATEWAY,
        "    return max(0.0, seconds) if seconds == seconds else None  # reject NaN\n",
        "    return (max(0.0, seconds) if seconds == seconds else None) or None  # reject NaN\n",
        _test("test_a_nil_hint_is_honoured_as_an_immediate_retry"),
    ),
    (
        "M04 the provider's own hint is ignored in favour of the schedule",
        GATEWAY,
        "        hinted = _retry_after_seconds(error)\n",
        "        hinted = None\n",
        _test("test_the_providers_own_hint_wins_over_the_schedule"),
    ),
    (
        "M05 a header that is not a delay is parsed as one",
        GATEWAY,
        "    except (TypeError, ValueError):\n        return None\n",
        "    except (TypeError, ValueError):\n        return 1.0\n",
        _test("test_a_header_that_is_not_a_delay_falls_back_to_the_schedule"),
    ),
    (
        "M06 the schedule has no ceiling",
        GATEWAY,
        "            else min(\n"
        "                _RATE_LIMIT_BACKOFF_BASE_SECONDS * (2**retry),\n"
        "                _RATE_LIMIT_BACKOFF_CEILING_SECONDS,\n"
        "            )\n",
        "            else _RATE_LIMIT_BACKOFF_BASE_SECONDS * (2**retry)\n",
        _test("test_the_schedule_bounds_the_wait_when_the_provider_gives_no_hint"),
    ),
    (
        "M07 the throttle is never re-asked at all -- the old behaviour",
        ANALYSIS,
        WAIT_IS_NONE,
        "                if True:\n",
        _test("test_a_throttled_draft_that_answers_on_the_retry_is_not_degraded"),
    ),
    (
        "M08 a nil hint is refused at the call site",
        ANALYSIS,
        WAIT_IS_NONE,
        "                if wait is None or wait <= 0.0 or attempt + 1 >= _ANALYSIS_THROTTLE_ATTEMPTS:\n",
        _test("test_the_throttle_wait_is_not_read_as_a_non_throttle_anywhere"),
    ),
    (
        "M09 the wait is unbounded in total",
        ANALYSIS,
        TOTAL_CLAMP,
        "",
        _test("test_the_total_wait_is_capped"),
    ),
    (
        "M10 the wait ignores the run's deadline",
        ANALYSIS,
        DEADLINE_CLAMP,
        DEADLINE_CLAMP_REMOVED,
        _test("test_the_wait_is_not_taken_when_the_run_is_already_at_its_deadline"),
    ),
    (
        "M11 the deadline is spent down to zero instead of leaving the later stages room",
        ANALYSIS,
        "                    wait = min(wait, remaining - _ANALYSIS_DEADLINE_HEADROOM_SECONDS)\n",
        "                    wait = min(wait, remaining)\n",
        _test("test_the_wait_leaves_the_later_stages_their_room"),
    ),
    (
        "M12 a throttle is re-asked indefinitely instead of a bounded number of times",
        ANALYSIS,
        "                if wait is None or attempt + 1 >= _ANALYSIS_THROTTLE_ATTEMPTS:\n",
        "                if wait is None:\n",
        _test("test_a_throttle_that_outlasts_the_wait_degrades_as_rate_limited"),
    ),
    (
        "M13 a throttle that survived the wait is reported as a model defect",
        ANALYSIS,
        '                    "ANALYSIS_RATE_LIMITED"\n'
        '                    if model_error_code(exc) == "MODEL_RATE_LIMITED"\n'
        '                    else "ANALYSIS_MODEL_FAILURE"\n',
        '                    "ANALYSIS_MODEL_FAILURE"\n'
        '                    if model_error_code(exc) == "MODEL_RATE_LIMITED"\n'
        '                    else "ANALYSIS_MODEL_FAILURE"\n',
        _test("test_a_throttle_that_outlasts_the_wait_degrades_as_rate_limited"),
    ),
    (
        "M14 the revision call gives up on a throttle the draft path would have waited out",
        ANALYSIS,
        '            result = await self._model_analysis_resilient(state, feedback=state["quality"].issues)\n',
        '            result = await self._model_analysis(state, feedback=state["quality"].issues)\n',
        _test("test_a_throttled_revision_is_waited_out_like_the_draft"),
    ),
    (
        "M15 a runtime failure is handed to a person even though a replan is owed",
        REVIEWER,
        REDERIVE,
        "            rederive = False\n",
        f"{REVIEWER_TEST}::test_reviewer_reports_a_degraded_analysis_as_a_runtime_failure",
    ),
    (
        "M16 the re-derivation is not bounded by the replan budget",
        REVIEWER,
        REDERIVE,
        "            rederive = True\n",
        f"{REVIEWER_TEST}::test_a_degraded_analysis_reaches_a_person_once_the_replans_are_spent",
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
