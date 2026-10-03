"""Revert each guard the before/after comparison rests on, and require the tests to notice.

The report's whole value is that it refuses to subtract two numbers produced under
different conditions. Every one of those refusals is a line that a plausible-looking
version of the script would not have: comparing across a corpus, across a model revision,
across a report whose run did not actually exercise the change. Each mutant here removes
one, and the named test is the one that has to go red -- a guard with no test is a comment.

The per-arm cutoff identity is here for the same reason: it is the finding the whole
workstream turns on, and a version that hardcoded it would report the conclusion without
measuring it.
"""

import pathlib
from pathlib import Path

from mutation_harness import Mutation, run_mutations

ROOT = pathlib.Path(__file__).resolve().parent.parent
#: Stable across runs: an interrupted run journals under this name, and a rename would
#: orphan the journal at a path the next run never looks at.
EXPERIMENT = "phase4_source_ceiling_effect"
EFFECT = ROOT / "scripts/report_phase4_source_ceiling_effect.py"
TEST = "tests/servicemind/test_phase4_source_ceiling_effect.py"


def _test(name: str) -> str:
    return f"{TEST}::{name}"


MUTATIONS = [
    # ---- the two runs have to be the same run ------------------------------------------
    (
        "M01 the run context is never compared, so any two runs may be subtracted",
        EFFECT,
        "    differing = sorted(key for key in left if left[key] != right[key])",
        "    differing = []",
        _test("test_a_before_after_across_a_different_corpus_is_refused"),
    ),
    (
        "M02 the model revisions are left out of the comparison, so two models compare as one",
        EFFECT,
        '        "models": report["models"],',
        "",
        _test("test_a_before_after_across_a_different_model_is_refused"),
    ),
    (
        "M03 the query counts are left out, so two label sets compare as one",
        EFFECT,
        '        "answerable": report["queries"]["answerable"],',
        "",
        _test("test_a_before_after_across_a_different_query_set_is_refused"),
    ),
    # ---- the runs have to be about the case the fix changes -----------------------------
    (
        "M04 a multi-source corpus is accepted, where the fix changes nothing",
        EFFECT,
        '        if packing["distinct_sources"] != 1:',
        "        if False:",
        _test("test_a_multi_source_corpus_is_refused_as_out_of_scope"),
    ),
    (
        "M05 the before run is not checked for predating the fix, so a run that already "
        "contains it is published as the column it is measured against",
        EFFECT,
        '    if before["packing"].get("source_ceiling_enforced", True) is not True:',
        "    if False:",
        _test("test_a_before_run_that_already_carries_the_fix_is_refused"),
    ),
    (
        "M06 the after run is not checked for having picked the fix up",
        EFFECT,
        '    if after["packing"].get("source_ceiling_enforced") is not False:',
        "    if False:",
        _test("test_an_after_run_that_did_not_pick_up_the_fix_is_refused"),
    ),
    # ---- the regime is read off the pack, not just off the run's own field --------------
    (
        "M09 the pack the two runs observed is not compared against the ceiling at all, so "
        "a run that says it was bound but packed past the cap is accepted",
        EFFECT,
        "        if (observed <= cap) is not expect_binding:",
        "        if False:",
        _test("test_a_before_run_the_ceiling_did_not_bound_is_refused"),
    ),
    (
        "M10 both sides are required to be bound by the ceiling, so the acceptance case "
        "this report exists to describe is refused",
        EFFECT,
        '        ("after", after, False),',
        '        ("after", after, True),',
        _test("test_the_headline_carries_the_packed_maximum_each_side"),
    ),
    (
        "M11 the comparison against the cap goes the wrong way, so being past the ceiling "
        "reads as being inside it",
        EFFECT,
        "        if (observed <= cap) is not expect_binding:",
        "        if (observed >= cap) is not expect_binding:",
        _test("test_the_headline_carries_the_packed_maximum_each_side"),
    ),
    # ---- the finding is measured, not asserted ------------------------------------------
    (
        "M07 the cutoff identity is asserted rather than measured, so it survives a counterexample",
        EFFECT,
        '            "cutoffs_are_one_measurement": len(\n'
        '                {round(results[arm][f"recall_at_{k}"], 6) for k in CUTOFFS}\n'
        "            )\n"
        "            == 1,",
        '            "cutoffs_are_one_measurement": True,',
        _test("test_the_cutoff_identity_is_reported_per_arm_and_not_assumed"),
    ),
    (
        "M08 the delta is taken the wrong way round, inverting every change",
        EFFECT,
        "                **{m: round(right[arm][m] - left[arm][m], 4) for m in METRICS},",
        "                **{m: round(left[arm][m] - right[arm][m], 4) for m in METRICS},",
        _test("test_every_arm_carries_both_sides_and_their_difference"),
    ),
]


if __name__ == "__main__":
    raise SystemExit(
        run_mutations(
            root=ROOT,
            experiment=EXPERIMENT,
            mutations=[
                Mutation(name=name, path=Path(path), old=old, new=new, tests=tuple(tests.split()))
                for name, path, old, new, tests in MUTATIONS
            ],
        )
    )
