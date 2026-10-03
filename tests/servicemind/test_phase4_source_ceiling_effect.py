"""Contract tests for the before/after comparison's refusals.

Both inputs to ``scripts/report_phase4_source_ceiling_effect.py`` are untracked, so the
tests drive the comparison with hand-built reports. What is worth testing is not the
arithmetic -- it is the guards: a before/after is only a comparison if the two runs differ
in exactly one thing, and the wrong version of this report is one that subtracts two
numbers produced under different conditions and attributes the difference to the fix.

Every guard therefore has a test that trips it, and a test that the same input is accepted
when the guard's condition holds -- otherwise a guard that rejected everything would pass.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "report_phase4_source_ceiling_effect",
    ROOT / "scripts/report_phase4_source_ceiling_effect.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

ARMS = ("dense", "bm25", "hybrid", "hybrid_rerank")


def _report(*, packed_max: int, enforced: bool, recall: float, sources: int = 1) -> dict:
    """A report shaped like the production run's, with only the fields this script reads."""
    return {
        "corpus": {"documents_indexed": 28481, "totals": {"parents": 46691}},
        "queries": {"total": 400, "answerable": 280, "unanswerable": 120},
        "models": {"embedding_revision": "aaa", "reranker_revision": "bbb"},
        "packing": {
            "context_token_budget": 8000,
            "max_parents_per_source": 4,
            "max_parents_per_document": 2,
            "distinct_sources": sources,
            "source_ceiling_enforced": enforced,
            "final_k": 24,
            "documents_packed_per_answerable_query": {
                arm: {"mean": 3.8, "min": 2, "max": packed_max} for arm in ARMS
            },
        },
        "results": {
            arm: {
                "recall_at_5": recall,
                "recall_at_10": recall,
                "recall_at_20": recall,
                "mrr_at_10": 0.5,
                "ndcg_at_10": 0.5,
            }
            for arm in ARMS
        },
    }


def _before() -> dict:
    """The pre-fix regime: the ceiling is in force and the pack stops at it."""
    return _report(packed_max=4, enforced=True, recall=0.6)


def _after() -> dict:
    """The post-fix regime: the ceiling is out of force and the pack runs past it."""
    return _report(packed_max=8, enforced=False, recall=0.7)


# ------------------------------------------------------------------- what it computes


def test_the_headline_carries_the_packed_maximum_each_side() -> None:
    comparison = MODULE.compare(_before(), _after())
    assert comparison["headline"]["before_packed_max"] == 4
    assert comparison["headline"]["after_packed_max"] == 8
    assert comparison["headline"]["before_arms_stopped_at_the_ceiling"] == sorted(ARMS)


def test_the_cutoff_identity_is_reported_per_arm_and_not_assumed() -> None:
    """The identity is the finding, so it is measured on both sides rather than stated."""
    assert MODULE.compare(_before(), _after())["headline"]["before_cutoffs_are_one_measurement"]

    split = _after()
    split["results"]["dense"]["recall_at_10"] = 0.75
    comparison = MODULE.compare(_before(), split)
    assert comparison["headline"]["after_cutoffs_are_one_measurement"] is False
    assert comparison["arms"]["dense"]["after"]["cutoffs_are_one_measurement"] is False
    # The other arms are untouched: the flag is per arm, so one arm losing the identity
    # cannot make the whole report say the identity broke everywhere.
    assert comparison["arms"]["bm25"]["after"]["cutoffs_are_one_measurement"] is True


def test_every_arm_carries_both_sides_and_their_difference() -> None:
    row = MODULE.compare(_before(), _after())["arms"]["dense"]
    assert row["before"]["recall_at_5"] == 0.6
    assert row["after"]["recall_at_5"] == 0.7
    assert row["delta"]["recall_at_5"] == 0.1
    assert row["delta"]["packed_max"] == 4


# ------------------------------------------------------------------------ the refusals


def test_a_before_after_across_a_different_corpus_is_refused() -> None:
    after = _after()
    after["corpus"]["documents_indexed"] = 100
    with pytest.raises(ValueError, match="differ in"):
        MODULE.compare(_before(), after)


def test_a_before_after_across_a_different_query_set_is_refused() -> None:
    """Same corpus, different labels: the recall columns would be averages of different
    questions, and the difference between them would be mostly the questions."""
    after = _after()
    after["queries"]["answerable"] = 279
    with pytest.raises(ValueError, match="answerable"):
        MODULE.compare(_before(), after)


def test_a_before_after_across_a_different_model_is_refused() -> None:
    after = _after()
    after["models"]["reranker_revision"] = "ccc"
    with pytest.raises(ValueError, match="models"):
        MODULE.compare(_before(), after)


def test_a_multi_source_corpus_is_refused_as_out_of_scope() -> None:
    """The fix changes nothing there, so the report would be about a difference of zero."""
    after = _after()
    after["packing"]["distinct_sources"] = 3
    with pytest.raises(ValueError, match="single-source case"):
        MODULE.compare(_before(), after)


def test_a_before_run_that_already_carries_the_fix_is_refused() -> None:
    """Then the two columns were produced by the same rule and differ for another reason.

    A pre-fix run from before the predicate existed does not carry the field at all, and
    that absence is read as *in force* -- so the only way to land here is a report that
    states the ceiling was out of force, which is a report that already contains the fix.
    """
    before = _before()
    before["packing"]["source_ceiling_enforced"] = False
    with pytest.raises(ValueError, match="already contains the fix"):
        MODULE.compare(before, _after())


def test_a_before_run_the_ceiling_did_not_bound_is_refused() -> None:
    """The field is the run's own account of itself; this reads what the packer did.

    A run that says the ceiling was in force while packing past it is describing a rule
    that did not do anything, and subtracting its column would attribute the difference to
    a fix that was not the difference.
    """
    before = _before()
    before["packing"]["documents_packed_per_answerable_query"] = {
        arm: {"mean": 7.0, "min": 2, "max": 8} for arm in ARMS
    }
    with pytest.raises(ValueError, match="not what the before side"):
        MODULE.compare(before, _after())


def test_an_after_run_still_inside_the_ceiling_is_refused() -> None:
    """After the fix the pack has to get past the cap, or there is nothing to compare."""
    after = _after()
    after["packing"]["documents_packed_per_answerable_query"] = {
        arm: {"mean": 3.8, "min": 2, "max": 4} for arm in ARMS
    }
    with pytest.raises(ValueError, match="not what the after side"):
        MODULE.compare(_before(), after)


def test_an_after_run_that_did_not_pick_up_the_fix_is_refused() -> None:
    """A report can be regenerated from a stale process; that is not a before/after."""
    after = _after()
    after["packing"]["source_ceiling_enforced"] = True
    with pytest.raises(ValueError, match="did not take effect"):
        MODULE.compare(_before(), after)


def test_a_missing_input_is_refused_rather_than_skipped(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(MODULE, "BEFORE", tmp_path / "absent-before.json")
    with pytest.raises(FileNotFoundError, match="preserved by hand"):
        MODULE.build()
