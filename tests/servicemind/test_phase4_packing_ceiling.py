"""Contract tests for the packing-ceiling derivation's pure helpers.

The script itself reads two artefacts that are not in git (the corpus archive and the
production report), so an end-to-end test could only run where both exist -- and a check
that cannot run in CI is not a check. These tests therefore drive the two pure functions
with synthetic sizes, which is where the published ``attributed_to`` verdict is decided.

The verdict is the part worth testing: it is a claim that a *rule* caused an observed
number, and the wrong version of it is indistinguishable from the right one unless the
inputs are chosen so that the two branches disagree.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "diagnose_phase4_packing_ceiling", ROOT / "scripts/diagnose_phase4_packing_ceiling.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


BUDGET = 8000
CAP = 4
FINAL_K = 24
#: Two sources by default, because that is the only regime in which the source ceiling is
#: in force at all -- the tests below that assert a cap verdict need a pool where the cap
#: could have fired. ``test_a_single_source_corpus_...`` is the other regime.
SOURCES = 2


def _stops(
    sizes: list[int],
    packed_max: int,
    *,
    cap: int = CAP,
    final_k: int = FINAL_K,
    sources: int = SOURCES,
) -> dict:
    """The regime is an input, not something the script re-derives.

    ``source_ceiling_enforced`` is what the *run* reported about its own candidate pool --
    a report produced before the ceiling was restricted to competing sources would be
    described by the wrong rule if this module reconstructed it. The tests drive both
    regimes explicitly for the same reason the verdict needs both: the wrong answer is
    invisible unless the inputs make the branches disagree.
    """
    return MODULE.who_stops_the_pack(sizes, packed_max, BUDGET, cap, final_k, sources > 1, sources)


# ------------------------------------------------------------------ size_distribution


def test_size_distribution_reports_observed_quantiles() -> None:
    """Nearest rank over the sorted sizes: no interpolation, because these are integers."""
    distribution = MODULE.size_distribution([100, 200, 300, 400])
    assert distribution["parents"] == 4
    assert distribution["tokens"] == {"p50": 300, "p90": 400, "p99": 400, "max": 400}
    assert distribution["mean"] == 250.0


def test_size_distribution_of_nothing_is_not_a_division_error() -> None:
    assert MODULE.size_distribution([]) == {"parents": 0}


def test_size_distribution_is_order_independent() -> None:
    ascending = MODULE.size_distribution([10, 20, 30, 40, 50])
    descending = MODULE.size_distribution([50, 40, 30, 20, 10])
    assert ascending == descending


# ---------------------------------------------------------------- who_stops_the_pack


def test_the_cap_is_the_verdict_when_the_budget_had_room() -> None:
    """Four 1100-token parents leave 3600 of the budget free: a fifth would have fitted.

    The pack stopped at exactly the per-source cap anyway, and the budget cannot be what
    stopped it, because the next parent of typical size is far smaller than the room left.
    """
    verdict = _stops([1100] * 10, 4)
    assert verdict["budget_would_admit_another_parent"] is True
    assert verdict["at_the_per_source_ceiling"] is True
    assert verdict["attributed_to"] == "the per-source ceiling"
    assert verdict["next_parent_headroom_tokens"]["p50"] == BUDGET - 4 * 1100


def test_the_budget_is_the_verdict_when_one_more_would_not_have_fitted() -> None:
    """Four 1900-token parents leave 400 tokens, less than one more parent costs.

    Here the observed maximum of 4 is consistent with the budget alone, so attributing it
    to the cap would credit a rule with a cut it did not make.
    """
    verdict = _stops([1900] * 10, 4)
    assert verdict["next_parent_headroom_tokens"]["p50"] == BUDGET - 4 * 1900
    assert verdict["budget_would_admit_another_parent"] is False
    assert verdict["attributed_to"] == "the token budget"


def test_a_short_list_is_not_attributed_to_either_ceiling() -> None:
    """A maximum of 3 under a cap of 4 with room to spare came from the candidate list.

    This is the branch that keeps the verdict from being a two-way choice: without it,
    every short list would be blamed on whichever of the two rules happened to be first.
    """
    verdict = _stops([500] * 10, 3)
    assert verdict["at_the_per_source_ceiling"] is False
    assert verdict["budget_would_admit_another_parent"] is True
    assert verdict["attributed_to"] == "neither ceiling: the candidate list ran out"


def test_a_list_that_stopped_at_final_k_names_that_depth() -> None:
    """``final_k`` is a depth, not a ceiling, and the verdict may not conflate the two."""
    verdict = _stops([500] * 10, 4, cap=99, final_k=4)
    assert verdict["final_k_is_reachable"] is False
    assert verdict["attributed_to"] == "final_k"


def test_the_cap_comparison_is_inclusive() -> None:
    """A maximum *equal to* the cap has reached the ceiling; one below it has not."""
    assert _stops([500] * 10, 4)["at_the_per_source_ceiling"] is True
    assert _stops([500] * 10, 3)["at_the_per_source_ceiling"] is False


def test_a_single_source_corpus_never_attributes_the_cut_to_the_source_ceiling() -> None:
    """The production corpus's exact shape: one source, room in the budget, a pack of four.

    Identical inputs to ``test_the_cap_is_the_verdict_when_the_budget_had_room`` except for
    the source count, and the verdict has to come out the other way. The ceiling is a bound
    between sources; with one source the packer does not apply it, so a maximum of four is
    a coincidence of where the candidate list ended and crediting the ceiling would credit
    a rule that never ran. This is the case that made the production prompt stop at four
    documents with more than a third of its token budget unspent.
    """
    verdict = _stops([1100] * 10, 4, sources=1)
    assert verdict["source_ceiling_enforced"] is False
    assert verdict["at_the_per_source_ceiling"] is False
    assert verdict["distinct_sources"] == 1
    assert verdict["budget_would_admit_another_parent"] is True
    assert verdict["attributed_to"] == "neither ceiling: the candidate list ran out"


def test_the_source_count_the_rule_turns_on_is_carried_into_the_verdict() -> None:
    """One source is not in force and two are, with everything else held fixed."""
    assert _stops([1100] * 10, 4, sources=1)["source_ceiling_enforced"] is False
    assert _stops([1100] * 10, 4, sources=2)["source_ceiling_enforced"] is True


def test_the_headroom_is_the_budget_less_the_parents_actually_packed() -> None:
    """Not less a fixed four: the term is the observed maximum, and it has to move with it."""
    verdict = _stops([600] * 10, 3)
    assert verdict["next_parent_headroom_tokens"]["p50"] == BUDGET - 3 * 600
    assert verdict["share_of_parents_that_still_fit"]["p50"] == 1.0


def test_a_parent_too_large_for_its_scenario_is_not_counted_as_fitting() -> None:
    """The share is over the observed sizes, so an oversized fifth lowers it.

    Four parents at the median here leave 2000 tokens, and 4 of the 10 observed parents
    are larger than that -- so the answer to "would a fifth parent have fitted?" is not
    the same for every parent, which is why the published table carries all three
    quantiles rather than one verdict.
    """
    sizes = [1500] * 6 + [2500] * 4
    verdict = _stops(sizes, 4)
    assert verdict["median_parent_tokens"] == 1500
    assert verdict["next_parent_headroom_tokens"]["p50"] == BUDGET - 4 * 1500
    assert verdict["share_of_parents_that_still_fit"]["p50"] == 0.6


def test_a_parent_exactly_the_size_of_the_headroom_counts_as_fitting() -> None:
    """``used + count > budget`` is strict, so a parent of exactly the headroom fits.

    One parent here is exactly as large as the room left by four median ones; counting it
    as refused would understate the share by the boundary case the budget check itself
    treats as allowed.
    """
    sizes = [1000] * 10 + [4000] * 2
    verdict = _stops(sizes, 4)
    assert verdict["next_parent_headroom_tokens"]["p50"] == 4000
    assert verdict["share_of_parents_that_still_fit"]["p50"] == 1.0


def test_a_scenario_that_could_not_have_packed_at_all_shows_as_no_headroom() -> None:
    """The strictest quantile can contradict the observation, and it must show that way.

    Four p90 parents here cost more than the whole budget, so a run that packed four could
    not have been drawing p90-sized parents. The row is kept rather than suppressed: it is
    what says the conclusion is a function of the assumed size, and the negative headroom
    is how a reader sees that this scenario is not the one that happened.
    """
    sizes = [1500] * 6 + [2500] * 4
    verdict = _stops(sizes, 4)
    assert verdict["next_parent_headroom_tokens"]["p90"] < 0
    assert verdict["share_of_parents_that_still_fit"]["p90"] == 0.0


def test_a_missing_input_is_refused_rather_than_skipped(monkeypatch, tmp_path) -> None:
    """Both inputs are outside git, so "not there" must be an error, not an empty report.

    A run that wrote a report from a missing corpus would publish a size table computed
    over nothing, and the ``--check`` guard would happily keep it fresh.
    """
    monkeypatch.setattr(MODULE, "CORPUS", tmp_path / "absent-corpus.zip")
    with pytest.raises(FileNotFoundError, match="re-run the production evaluation"):
        MODULE.build()
