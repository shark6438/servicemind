"""Revert each decision the recall-cutoff derivation rests on, and require the tests to notice.

``recall_at_k`` counts gold inside the first ``k`` items an arm returned, so an arm that
packed at most ``n`` documents answers every cutoff above ``n`` with the same number. That
identity is what the audit now reports, and every part of it has a plausible-looking wrong
version that still produces a number: a strict comparison drops the one boundary case where
the list exactly reaches the cutoff; the collapse list shifted by one names the wrong
cutoffs; a predicate that always says "distinguished" empties the finding while a predicate
that always says "not distinguished" fills it with a bar that a second arm can decide.

Two of the mutations below are wiring rather than arithmetic -- the section is dropped from
the state, and the derivation is dropped from the section -- because a derivation the state
never carries is indistinguishable from one that was never computed.
"""

import pathlib
from pathlib import Path

from mutation_harness import Mutation, run_mutations

ROOT = pathlib.Path(__file__).resolve().parent.parent
#: Stable across runs: an interrupted run journals under this name, and a rename would
#: orphan the journal at a path the next run never looks at.
EXPERIMENT = "recall_cutoffs"
AUDIT = ROOT / "scripts/audit_rag_quality_state.py"
TESTS = "tests/servicemind/test_rag_quality_state.py"


def _test(name: str) -> str:
    return f"{TESTS}::{name}"


BOUNDARY = "            if deepest <= narrow and recalls[narrow] != recalls[wide]:"
COLLAPSE = "[k for k in RECALL_CUTOFFS if k >= deepest]"
PREDICATE = '    return recalls[name] != recalls[f"recall_at_{max(narrower)}"]'
ONE_MEASUREMENT = '            "one_measurement": len(set(recalls.values())) == 1,'

MUTATIONS = [
    # ---- the contradiction the guard exists to refuse ----------------------------------
    (
        "M01 the comparison goes strict, so a list that exactly reaches the cutoff is "
        "allowed to report two different numbers either side of it",
        AUDIT,
        BOUNDARY,
        "            if deepest < narrow and recalls[narrow] != recalls[wide]:",
        _test("test_the_guard_fires_on_the_last_document_that_still_collapses"),
    ),
    (
        "M02 the comparison is inverted, so the guard refuses exactly the reports that are "
        "consistent and waves through the ones that are not",
        AUDIT,
        BOUNDARY,
        "            if deepest <= narrow and recalls[narrow] == recalls[wide]:",
        _test("test_a_pack_shorter_than_the_cutoffs_is_named_as_one_measurement"),
    ),
    (
        "M03 the pack depth is read from the shallowest query instead of the deepest, so "
        "one long list stops the whole arm being reported as cut short",
        AUDIT,
        '        deepest = packed[arm]["max"]',
        '        deepest = packed[arm]["min"]',
        _test("test_the_quality_state_carries_the_cutoffs_the_production_path_can_measure"),
    ),
    # ---- what the collapse list names --------------------------------------------------
    (
        "M04 the comparison goes strict, so the cutoff the list exactly reaches is reported "
        "as a window rather than as the whole pack",
        AUDIT,
        COLLAPSE,
        "[k for k in RECALL_CUTOFFS if k > deepest]",
        _test("test_a_pack_that_stops_at_the_cutoff_still_counts_the_whole_pack_there"),
    ),
    (
        "M05 the cutoffs are off by one, so the shallowest collapsed cutoff is left out "
        "of the finding",
        AUDIT,
        COLLAPSE,
        "[k for k in RECALL_CUTOFFS if k >= deepest + 1]",
        _test("test_a_pack_that_stops_at_the_cutoff_still_counts_the_whole_pack_there"),
    ),
    (
        "M06 the cutoff set is trimmed, so a cutoff the §4.1 clauses are stated at is "
        "never examined",
        AUDIT,
        "RECALL_CUTOFFS = (5, 10, 20)",
        "RECALL_CUTOFFS = (5, 10)",
        _test("test_a_pack_shorter_than_the_cutoffs_is_named_as_one_measurement"),
    ),
    # ---- whether a bar is its own measurement ------------------------------------------
    (
        "M07 every threshold is called distinguished, so a bar the pack decides for itself "
        "is reported as a measurement of its own",
        AUDIT,
        PREDICATE,
        "    return True",
        _test("test_a_pack_shorter_than_the_cutoffs_is_named_as_one_measurement"),
    ),
    (
        "M08 no threshold is ever distinguished, so a bar a second arm can decide is "
        "reported as unmeasured",
        AUDIT,
        PREDICATE,
        "    return False",
        _test("test_a_pack_deep_enough_to_separate_the_cutoffs_names_no_collapsed_threshold"),
    ),
    (
        # Written first as ``<= 1``, which the harness reported GREEN -- and rightly, since
        # a three-entry set can never be empty, so ``<= 1`` and ``== 1`` are the same
        # predicate and there is no test that could tell them apart. An equivalent mutant
        # is deleted rather than given a test with no teeth; the inverted one below is what
        # actually distinguishes "the cutoffs agree" from "the cutoffs differ".
        "M09 an arm is called collapsed when its cutoffs all *differ*, which reports the "
        "arms that separate them as the ones that cannot",
        AUDIT,
        ONE_MEASUREMENT,
        '            "one_measurement": len(set(recalls.values())) == len(recalls),',
        _test("test_a_pack_deep_enough_to_separate_the_cutoffs_names_no_collapsed_threshold"),
    ),
    # ---- how deep a metric's window had to be ------------------------------------------
    (
        "M12 the depth is read off the last character, so every metric above 9 is treated "
        "as stated at a one-digit cutoff",
        AUDIT,
        '    return int(name.rsplit("_", 1)[1])',
        "    return int(name[-1])",
        _test("test_a_metric_the_pack_never_reached_is_named_beside_the_pack_depth"),
    ),
    (
        "M13 a metric is called unmeasurable if *any* arm was short, so one truncated arm "
        "hides a window a second arm did fill",
        AUDIT,
        '            if all(_cutoff_of(name) > block["packed_max"] for block in arms.values())',
        '            if any(_cutoff_of(name) > block["packed_max"] for block in arms.values())',
        _test("test_one_arm_deep_enough_clears_the_metric_for_the_whole_report"),
    ),
    (
        "M14 the depth comparison is inverted, so only metrics below the pack are named as "
        "too short for it",
        AUDIT,
        "                name for name in TARGETS if _cutoff_of(name) > deepest",
        "                name for name in TARGETS if _cutoff_of(name) < deepest",
        _test("test_a_metric_the_pack_never_reached_is_named_beside_the_pack_depth"),
    ),
    # ---- the wiring: a derivation nothing carries is a derivation that was not made -----
    (
        "M10 the derivation is dropped from the section, so the state names the report "
        "without carrying what was read out of it",
        AUDIT,
        "        **check_the_recall_cutoffs_match_the_pack(report),",
        "        **{},",
        _test("test_the_quality_state_carries_the_cutoffs_the_production_path_can_measure"),
    ),
    (
        "M11 the section is dropped from the state, so nothing downstream can see it",
        AUDIT,
        '        "production_path": production_path_section,\n',
        "",
        _test("test_the_quality_state_carries_the_cutoffs_the_production_path_can_measure"),
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
