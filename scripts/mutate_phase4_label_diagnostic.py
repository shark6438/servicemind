"""Revert each decision the label diagnostic rests on, and require the tests to notice.

The diagnostic's output is a published number (0.8714) plus a claim that sits on top of it
(a §4.1 threshold is above that ceiling and therefore unreachable by ranking better). Both
are readings of arithmetic -- Jaccard over word sets, a 1-based rank, a rate divided by all
queries rather than by the hits -- and every one of those has a plausible-looking wrong
version that produces a number instead of an error. A rate divided by the hits inflates the
ceiling; a non-strict comparison turns ties into "the first hit is closer" and inverts the
split; a 0-based rank moves the top-5 count by one in whichever direction the data happens
to point.

Each mutation below removes one of those decisions. The tests they name are the ones in
``tests/servicemind/test_phase4_label_diagnostic.py`` and
``tests/servicemind/test_rag_quality_state.py``, which drive the helpers with synthetic rows
and so need none of the gitignored inputs.
"""

import pathlib
from pathlib import Path

from mutation_harness import Mutation, run_mutations

ROOT = pathlib.Path(__file__).resolve().parent.parent
#: Stable across runs: an interrupted run journals under this name, and a rename would
#: orphan the journal at a path the next run never looks at.
EXPERIMENT = "phase4_label_diagnostic"
DIAGNOSTIC = ROOT / "scripts/diagnose_phase4_label_diagnostic.py"
AUDIT = ROOT / "scripts/audit_rag_quality_state.py"

DIAGNOSTIC_TEST = "tests/servicemind/test_phase4_label_diagnostic.py"
AUDIT_TEST = "tests/servicemind/test_rag_quality_state.py"


def _test(name: str) -> str:
    return f"{DIAGNOSTIC_TEST}::{name}"


def _audit_test(name: str) -> str:
    return f"{AUDIT_TEST}::{name}"


MUTATIONS = [
    # ---- the word set the whole overlap table is computed over ----------------------
    (
        "M01 the stop list is dropped, so IBM alone puts every document in every question",
        DIAGNOSTIC,
        "        if word not in STOPWORDS and len(word) > 1 and not word.isdigit()",
        "        if len(word) > 1 and not word.isdigit()",
        _test("test_words_drops_stopwords_digits_and_single_characters"),
    ),
    (
        "M02 digits are kept, and release numbers outrank the subject",
        DIAGNOSTIC,
        "        if word not in STOPWORDS and len(word) > 1 and not word.isdigit()",
        "        if word not in STOPWORDS and len(word) > 1",
        _test("test_words_drops_stopwords_digits_and_single_characters"),
    ),
    (
        "M03 one-character tokens are kept",
        DIAGNOSTIC,
        "        if word not in STOPWORDS and len(word) > 1 and not word.isdigit()",
        "        if word not in STOPWORDS and not word.isdigit()",
        _test("test_words_keeps_two_letter_acronyms"),
    ),
    (
        "M04 an empty pair of word sets divides by zero instead of reading as no overlap",
        DIAGNOSTIC,
        "    return len(left & right) / len(union) if union else 0.0",
        "    return len(left & right) / len(union)",
        _test("test_jaccard_is_zero_when_both_sides_are_empty"),
    ),
    # ---- the rank the ceiling and the top-5 count are read off ----------------------
    (
        "M05 ranks are zero-based, so a document at position 6 counts as top-5",
        DIAGNOSTIC,
        "    return next((rank for rank, key in enumerate(row[arm], start=1) if key in relevant), None)",
        "    return next((rank for rank, key in enumerate(row[arm]) if key in relevant), None)",
        _test("test_rank_of_is_one_based_and_none_when_absent"),
    ),
    (
        "M06 the last relevant hit wins instead of the first",
        DIAGNOSTIC,
        "    return next((rank for rank, key in enumerate(row[arm], start=1) if key in relevant), None)",
        "    ranks = [rank for rank, key in enumerate(row[arm], start=1) if key in relevant]\n"
        "    return ranks[-1] if ranks else None",
        _test("test_rank_of_takes_the_earliest_match_when_several_are_relevant"),
    ),
    (
        "M07 the rate is divided by the queries that hit, which is what makes it a ceiling "
        "and not a share",
        DIAGNOSTIC,
        '        "contains_gold_rate": round(len(present) / len(rows), 4) if rows else 0.0,',
        '        "contains_gold_rate": 1.0 if present else 0.0,',
        _test("test_arm_summary_divides_by_all_queries_not_by_the_hits"),
    ),
    (
        "M08 the pool depth is the first list's length, so the ceiling is stated for a "
        "shallower pool than the one measured",
        DIAGNOSTIC,
        "    depth = max((len(row[arm]) for row in rows), default=0)",
        "    depth = len(rows[0][arm]) if rows else 0",
        _test("test_arm_summary_reports_the_deepest_list_and_every_distinct_length"),
    ),
    # ---- the split that carries the finding -----------------------------------------
    (
        "M09 a tie is counted as the first hit being closer, which inverts the in-pool group",
        DIAGNOSTIC,
        "        if first > gold:",
        "        if first >= gold:",
        _test("test_title_overlap_counts_a_tie_as_the_label_winning"),
    ),
    (
        "M10 a query whose arm returned nothing counts as the label winning",
        DIAGNOSTIC,
        "        listed = row[arm]\n        if not listed:\n            continue\n",
        '        listed = row[arm] or row["relevant"][:1]\n',
        _test("test_title_overlap_skips_queries_whose_arm_returned_nothing"),
    ),
    (
        "M11 the examples are allowed to include the cases that argue the other way",
        DIAGNOSTIC,
        "        if advantage > 0\n",
        "",
        _test("test_examples_exclude_queries_where_the_label_is_the_closer_one"),
    ),
    (
        "M12 the examples are taken from the shallow end of the sort",
        DIAGNOSTIC,
        "    scored.sort(key=lambda item: item[0], reverse=True)",
        "    scored.sort(key=lambda item: item[0])",
        _test("test_examples_are_ordered_by_the_first_hits_advantage"),
    ),
    (
        "M13 the pool flag is read off the dense arm rather than the deciding arm",
        DIAGNOSTIC,
        '            "gold_in_the_100_deep_pool": _rank_of(row, DECIDING_ARM) is not None,',
        '            "gold_in_the_100_deep_pool": _rank_of(row, "dense") is not None,',
        _test("test_examples_report_pool_membership_from_the_deciding_arm"),
    ),
    (
        "M14 the title split runs past the body, so the house-styled text sets the subject",
        DIAGNOSTIC,
        '            titles[Path(name).name] = body.split("Text:", 1)[0].replace("Title:", "").strip()',
        '            titles[Path(name).name] = body.replace("Title:", "").strip()',
        _test("test_corpus_titles_reads_the_header_block_and_not_the_body"),
    ),
    # ---- the derivation that turns the ceiling into a verdict -----------------------
    (
        "M15 every threshold is compared to the ceiling, including the ratios that are not "
        "recall@k",
        AUDIT,
        '        if name.startswith("recall_at_") and threshold > ceiling',
        "        if threshold > ceiling",
        # At the real ceiling the two ratio thresholds sit below it, so a version that
        # compared every threshold is indistinguishable there; the second test drives the
        # ceiling to zero, where it is not.
        f"{_audit_test('test_only_recall_at_k_can_be_measured_against_the_ceiling')} "
        f"{_audit_test('test_the_unreachable_thresholds_are_derived_from_the_ceiling')}",
    ),
    (
        "M16 the unreachable list is a literal, so a pool that covers everything still "
        "reports a threshold above it",
        AUDIT,
        '    ceiling = report["candidate_funnel"]["contains_gold_rate"]\n'
        "    unreachable = sorted(\n"
        "        name\n"
        "        for name, threshold in TARGETS.items()\n"
        '        if name.startswith("recall_at_") and threshold > ceiling\n'
        "    )",
        '    ceiling = report["candidate_funnel"]["contains_gold_rate"]\n'
        '    unreachable = ["recall_at_10"]',
        _audit_test("test_a_funnel_that_covers_everything_leaves_no_unreachable_threshold"),
    ),
    (
        "M17 the ceiling is restated from the threshold table instead of carried from the "
        "report it was derived from",
        AUDIT,
        '        "candidate_pool": report["candidate_funnel"],',
        '        "candidate_pool": {"contains_gold_rate": max(TARGETS.values()), "arm": "?", "depth": 0},',
        _audit_test("test_the_unreachable_thresholds_are_derived_from_the_ceiling"),
    ),
    (
        "M18 the loss split is dropped from the status file, leaving the prose behind it "
        "with nothing to read",
        AUDIT,
        '        "title_overlap": {group: block["title_overlap"] for group, block in split.items()},',
        '        "title_overlap": {},',
        _audit_test("test_the_label_diagnostic_section_carries_the_split_it_claims"),
    ),
]


if __name__ == "__main__":
    raise SystemExit(
        run_mutations(
            root=ROOT,
            experiment=EXPERIMENT,
            mutations=[
                # ``split()`` because a mutation may name more than one test -- the ones
                # whose teeth live in the same decision. Passing them as a single id is how
                # a mutation grades as UNRUN instead of as detected.
                Mutation(name=name, path=Path(path), old=old, new=new, tests=tuple(tests.split()))
                for name, path, old, new, tests in MUTATIONS
            ],
        )
    )
