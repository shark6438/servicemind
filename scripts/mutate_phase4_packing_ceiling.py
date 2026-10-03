"""Revert each decision the packing-ceiling verdict rests on, and require the tests to notice.

The published output of this diagnostic is one sentence -- which ceiling stopped the
production prompt at four documents -- and it is a claim about *cause*. Every part of it
has a plausible-looking wrong version that still produces a sentence: a headroom computed
against a hardcoded four instead of the observed maximum, a "would one more have fitted?"
tested with ``> 0`` instead of against a parent's size, a cap comparison that excludes the
boundary. Each of those returns a verdict, just not the one the data supports, which is
why the tests drive the two branches apart with inputs chosen to disagree.

Every mutation is reverted and the tests named in it are the ones that must go red. They
live in ``tests/servicemind/test_phase4_packing_ceiling.py`` and
``tests/servicemind/test_rag_quality_state.py``, and need neither the corpus archive nor
the production report: the first drives the helpers with synthetic sizes, the second the
audit's copy of the verdict.
"""

import pathlib
from pathlib import Path

from mutation_harness import Mutation, run_mutations

ROOT = pathlib.Path(__file__).resolve().parent.parent
#: Stable across runs: an interrupted run journals under this name, and a rename would
#: orphan the journal at a path the next run never looks at.
EXPERIMENT = "phase4_packing_ceiling"
DIAGNOSTIC = ROOT / "scripts/diagnose_phase4_packing_ceiling.py"
AUDIT = ROOT / "scripts/audit_rag_quality_state.py"
TEST = "tests/servicemind/test_phase4_packing_ceiling.py"
AUDIT_TEST = "tests/servicemind/test_rag_quality_state.py"


def _test(name: str) -> str:
    return f"{TEST}::{name}"


def _audit_test(name: str) -> str:
    return f"{AUDIT_TEST}::{name}"


MUTATIONS = [
    # ---- the size table the whole verdict is read off ---------------------------------
    (
        "M01 the quantile is taken over a span of n-1, moving every reported size down",
        DIAGNOSTIC,
        "    return sorted_sizes[min(len(sorted_sizes) - 1, int(fraction * len(sorted_sizes)))]",
        "    return sorted_sizes[min(len(sorted_sizes) - 1, int(fraction * (len(sorted_sizes) - 1)))]",
        _test("test_size_distribution_reports_observed_quantiles"),
    ),
    (
        "M02 the fraction is ignored, so every quantile reports the maximum",
        DIAGNOSTIC,
        "    return sorted_sizes[min(len(sorted_sizes) - 1, int(fraction * len(sorted_sizes)))]",
        "    return sorted_sizes[-1]",
        _test("test_size_distribution_reports_observed_quantiles"),
    ),
    (
        "M03 the mean is reported as a quantile, so it stops being an average",
        DIAGNOSTIC,
        '        "mean": round(sum(ordered) / len(ordered), 1),',
        '        "mean": float(ordered[-1]),',
        _test("test_size_distribution_reports_observed_quantiles"),
    ),
    (
        "M04 an empty sample divides by zero instead of reporting that it measured nothing",
        DIAGNOSTIC,
        '    if not sizes:\n        return {"parents": 0}\n',
        "",
        _test("test_size_distribution_of_nothing_is_not_a_division_error"),
    ),
    # ---- the headroom the budget left --------------------------------------------------
    (
        "M05 the headroom subtracts a hardcoded four instead of the parents actually packed",
        DIAGNOSTIC,
        "        headroom[label] = budget - packed_max * at",
        "        headroom[label] = budget - 4 * at",
        _test("test_the_headroom_is_the_budget_less_the_parents_actually_packed"),
    ),
    (
        "M06 the headroom forgets the packed parents entirely, so it is the whole budget",
        DIAGNOSTIC,
        "        headroom[label] = budget - packed_max * at",
        "        headroom[label] = budget - at",
        _test("test_the_cap_is_the_verdict_when_the_budget_had_room"),
    ),
    (
        "M07 any positive headroom counts as room for another parent, however small",
        DIAGNOSTIC,
        '    budget_allows_one_more = headroom["p50"] >= median',
        '    budget_allows_one_more = headroom["p50"] > 0',
        _test("test_the_budget_is_the_verdict_when_one_more_would_not_have_fitted"),
    ),
    (
        "M08 the median is read from the strictest quantile instead of the middle",
        DIAGNOSTIC,
        "    median = _quantile(ordered, 0.50)",
        "    median = _quantile(ordered, 0.99)",
        _test("test_a_parent_too_large_for_its_scenario_is_not_counted_as_fitting"),
    ),
    (
        "M09 the share counts only parents strictly smaller than the headroom",
        DIAGNOSTIC,
        "        label: sum(1 for size in ordered if size <= room) / len(ordered)",
        "        label: sum(1 for size in ordered if size < room) / len(ordered)",
        _test("test_a_parent_exactly_the_size_of_the_headroom_counts_as_fitting"),
    ),
    (
        "M10 the share becomes a flag, so one fitting parent reads as all of them fitting",
        DIAGNOSTIC,
        "        label: sum(1 for size in ordered if size <= room) / len(ordered)",
        "        label: 1.0 if any(size <= room for size in ordered) else 0.0",
        _test("test_a_parent_too_large_for_its_scenario_is_not_counted_as_fitting"),
    ),
    # ---- the comparison against the cap ------------------------------------------------
    (
        "M11 reaching the cap exactly is not counted as reaching it",
        DIAGNOSTIC,
        "    at_the_cap = source_ceiling_enforced and packed_max >= per_source_cap",
        "    at_the_cap = source_ceiling_enforced and packed_max > per_source_cap",
        _test("test_the_cap_comparison_is_inclusive"),
    ),
    (
        "M11b the regime the run reported is dropped, so a ceiling that was never in force "
        "is credited with the cut",
        DIAGNOSTIC,
        "    at_the_cap = source_ceiling_enforced and packed_max >= per_source_cap",
        "    at_the_cap = packed_max >= per_source_cap",
        _test("test_a_single_source_corpus_never_attributes_the_cut_to_the_source_ceiling"),
    ),
    (
        "M12 final_k is treated as reachable when the list stopped exactly at it",
        DIAGNOSTIC,
        '        "final_k_is_reachable": final_k > packed_max,',
        '        "final_k_is_reachable": final_k >= packed_max,',
        _test("test_a_list_that_stopped_at_final_k_names_that_depth"),
    ),
    (
        "M13 the third answer is folded into the second, blaming a rule that never fired",
        DIAGNOSTIC,
        '        attributed = "neither ceiling: the candidate list ran out"',
        '        attributed = "the token budget"',
        _test("test_a_short_list_is_not_attributed_to_either_ceiling"),
    ),
    (
        "M14 the cap case falls through to the budget, inverting the published verdict",
        DIAGNOSTIC,
        '    elif at_the_cap:\n        attributed = "the per-source ceiling"',
        '    elif at_the_cap:\n        attributed = "the token budget"',
        _test("test_the_cap_is_the_verdict_when_the_budget_had_room"),
    ),
    # ---- the audit's copy of the verdict, and the guard over it ------------------------
    (
        "M15 the guard stops requiring room in the budget, so a cap verdict is accepted "
        "even when the budget had none",
        AUDIT,
        '    if verdict == "the per-source ceiling" and not (room and at_cap):',
        '    if verdict == "the per-source ceiling" and not at_cap:',
        _audit_test("test_a_packing_verdict_its_own_numbers_exclude_is_refused"),
    ),
    (
        "M16 the guard's budget branch is inverted, so it refuses the verdict the data supports",
        AUDIT,
        '    if verdict == "the token budget" and room:',
        '    if verdict == "the token budget" and not room:',
        _audit_test("test_a_packing_verdict_its_own_numbers_exclude_is_refused"),
    ),
    (
        "M17 the section drops the headroom, leaving the verdict with nothing behind it",
        AUDIT,
        '        "next_parent_headroom_tokens": derivation["next_parent_headroom_tokens"],',
        '        "next_parent_headroom_tokens": {},',
        _audit_test("test_the_packing_section_carries_the_verdict_it_names"),
    ),
    (
        'M19 the "neither ceiling" branch is accepted even when a ceiling was reached',
        AUDIT,
        '    if verdict.startswith("neither ceiling") and (at_cap or not room):',
        "    if False:",
        _audit_test("test_a_packing_verdict_naming_a_cause_the_numbers_exclude_is_refused"),
    ),
    (
        "M20 the final_k branch is accepted even when the list stopped short of that depth",
        AUDIT,
        '    if verdict == "final_k" and reachable:',
        "    if False:",
        _audit_test("test_a_packing_verdict_naming_a_cause_the_numbers_exclude_is_refused"),
    ),
    (
        "M21 the section drops whether the ceiling was in force, leaving the verdict's rule "
        "unstated",
        AUDIT,
        '        "source_ceiling_enforced": derivation["source_ceiling_enforced"],',
        '        "source_ceiling_enforced": True,',
        _audit_test("test_the_packing_section_carries_the_verdict_it_names"),
    ),
    (
        "M18 the section counts the arms instead of the sources the ceiling divides between",
        AUDIT,
        '        "distinct_sources": sampled["distinct_sources"],',
        '        "distinct_sources": len(report["observed_packing"]["maxima"]),',
        _audit_test("test_the_packing_section_carries_the_verdict_it_names"),
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
