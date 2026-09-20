"""Measurement-layer contracts for `scripts/evaluate_phase4_proxy_release.py`.

These guard the three defects found in the published proxy report:

1. the reranker's probabilities were passed through a second sigmoid, compressing
   every reported score into (0.5, 0.731];
2. the abstention gate was scored as a retrieval-score cut without publishing how
   little answerability signal that score carries;
3. the §4.1 tenant-domain closure gate was rendered as PASS/FAIL against a public
   silver set it was never scoped to;
4. the cross-encoder was pinned to a 512-token window while production inherits the
   checkpoint's own window (8192), so the harness truncated passages production scores
   in full - and the window was not part of the score cache key either.

The script is a standalone harness rather than an importable package module, so it
is loaded by path the same way the operators run it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "evaluate_phase4_proxy_release.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("proxy_release_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve `cls.__module__` through sys.modules during exec.
    sys.modules["proxy_release_script"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def proxy():
    return _load_module()


def _outcome(top_score: float) -> SimpleNamespace:
    return SimpleNamespace(top_score=top_score)


def _split(answerable_scores: list[float], impossible_scores: list[float]):
    answerable = [_outcome(score) for score in answerable_scores]
    impossible = [_outcome(score) for score in impossible_scores]
    return answerable, impossible


# --- defect 1: double sigmoid -------------------------------------------------


@pytest.mark.parametrize("value", [0.0, 0.0025, 0.5, 0.731054, 0.9975, 1.0])
def test_reranker_probabilities_are_not_rescaled(proxy, value):
    """A probability must survive unchanged; sigmoid(sigmoid(x)) would not."""
    result = proxy._reranker_probabilities(np.asarray([value]))
    assert result[0] == pytest.approx(value, abs=0.0)


def test_reranker_probabilities_reject_scores_outside_unit_range(proxy):
    """Logits would mean the CrossEncoder activation assumption broke."""
    with pytest.raises(RuntimeError, match=r"outside \[0, 1\]"):
        proxy._reranker_probabilities(np.asarray([-3.2, 7.5]))


def test_reranker_probabilities_tolerate_empty_input(proxy):
    assert proxy._reranker_probabilities(np.asarray([], dtype=np.float64)).size == 0


def test_reported_top_score_is_the_model_probability(proxy):
    """The published bug compressed a confident 0.99 into 0.7311."""
    confident = proxy._reranker_probabilities(np.asarray([0.99, 0.2]))
    assert confident.max() == pytest.approx(0.99)
    assert confident.max() > 0.7311


# --- production blend: the harness must measure what production runs -----------


def test_blend_reproduces_the_production_formula(proxy):
    """rerank_weight * rerank + (1 - rerank_weight) * minmax(rrf), hand-computed."""
    ranking = ("a", "b", "c")
    rerank = {"a": 0.20, "b": 0.90, "c": 0.50}
    rrf = {"a": 0.10, "b": 0.05, "c": 0.00}
    # minmax(rrf) -> a=1.00, b=0.50, c=0.00
    # final      -> a=0.32, b=0.84, c=0.425
    assert proxy._blend_ranking(ranking, rerank, rrf, rerank_weight=0.85, depth=3) == (
        "b",
        "c",
        "a",
    )


def test_blend_reorders_instead_of_rescaling_the_rerank_score(proxy):
    """Regression guard: an earlier experiment blended the rerank score into itself
    and concluded, wrongly, that the production blend is a no-op."""
    ranking = ("a", "b", "c")
    rerank = {"a": 0.10, "b": 0.15, "c": 0.14}
    rrf = {"a": 0.10, "b": 0.00, "c": 0.09}
    pure = tuple(sorted(ranking, key=lambda doc: -rerank[doc]))
    blended = proxy._blend_ranking(ranking, rerank, rrf, rerank_weight=0.85, depth=3)
    assert pure == ("b", "c", "a")
    assert blended == ("c", "a", "b")
    assert blended != pure


def test_blend_honours_the_rerank_weight_bounds(proxy):
    ranking = ("a", "b")
    rerank = {"a": 0.10, "b": 0.90}
    rrf = {"a": 0.90, "b": 0.10}
    assert proxy._blend_ranking(ranking, rerank, rrf, rerank_weight=1.0, depth=2) == ("b", "a")
    assert proxy._blend_ranking(ranking, rerank, rrf, rerank_weight=0.0, depth=2) == ("a", "b")


def test_blend_limits_the_window_to_the_reranked_depth(proxy):
    """`c` ranks last but is excluded by depth, and the minmax span is over the window."""
    ranking = ("a", "b", "c")
    rerank = {"a": 0.10, "b": 0.20, "c": 0.30}
    rrf = {"a": 0.30, "b": 0.20, "c": 0.10}
    assert proxy._blend_ranking(ranking, rerank, rrf, rerank_weight=0.0, depth=2) == ("a", "b")
    assert proxy._blend_ranking(ranking, rerank, rrf, rerank_weight=0.0, depth=3) == ("a", "b", "c")


def test_blend_survives_a_degenerate_retrieval_span(proxy):
    """A flat RRF score would divide by zero; production falls back to 0.5."""
    ranking = ("a", "b")
    rerank = {"a": 0.10, "b": 0.90}
    rrf = {"a": 0.5, "b": 0.5}
    assert proxy._blend_ranking(ranking, rerank, rrf, rerank_weight=0.85, depth=2) == ("b", "a")


def test_rrf_scores_use_the_production_rank_constant(proxy):
    assert proxy.RRF_RANK_CONSTANT == 60
    scores = proxy._rrf_scores(["a", "b"], ["b", "c"])
    assert scores["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert scores["a"] == pytest.approx(1 / 61)
    assert scores["c"] == pytest.approx(1 / 62)
    # An arm that did not return a document contributes nothing for it.
    assert scores["a"] < scores["b"]


def test_rrf_ranking_is_the_score_order_truncated_to_k(proxy):
    left = [f"d{index}" for index in range(10)]
    right = [f"d{index}" for index in range(5, 15)]
    scores = proxy._rrf_scores(left, right)
    expected = sorted(scores, key=lambda doc: (-scores[doc], doc))[:3]
    assert proxy._rrf(left, right, k=3) == expected


# --- defect 2: abstention signal ceiling --------------------------------------


def test_abstention_reports_signal_separability(proxy):
    """The ceiling must be published, not just the calibrated operating point."""
    answerable, impossible = _split([0.9] * 280, [0.1] * 120)
    metrics = proxy._abstention_metrics(answerable, impossible)
    assert metrics["top_score_roc_auc"] == pytest.approx(1.0)
    assert metrics["best_case_balanced_accuracy"] == pytest.approx(1.0)


def test_abstention_ceiling_is_chance_when_scores_carry_no_signal(proxy):
    rng = np.random.default_rng(7)
    answerable = [_outcome(float(score)) for score in rng.uniform(0.5, 0.73, 240)]
    impossible = [_outcome(float(score)) for score in rng.uniform(0.5, 0.73, 100)]
    metrics = proxy._abstention_metrics(answerable, impossible)
    assert 0.35 <= metrics["top_score_roc_auc"] <= 0.65
    assert metrics["best_case_balanced_accuracy"] < 0.90


def test_abstention_holds_out_calibration_queries(proxy):
    """40/20 calibrate the threshold; 240/100 are scored."""
    answerable = [_outcome(0.9)] * 40 + [_outcome(0.1)] * 240
    impossible = [_outcome(0.2)] * 20 + [_outcome(0.9)] * 100
    metrics = proxy._abstention_metrics(answerable, impossible)
    assert metrics["calibration"] == {"answerable": 40, "impossible": 20}
    assert metrics["held_out"] == {"answerable": 240, "impossible": 100}
    # The held-out split is the one that must abstain on hard impossibles.
    assert metrics["answerable_answer_rate"] == pytest.approx(0.0)
    assert metrics["impossible_abstention_rate"] == pytest.approx(0.0)


# --- defect 3: gate scope -----------------------------------------------------


def _gates(proxy, reranked_at_10: float, abstention: dict, recall_at_10: float | None = None):
    reranked = {
        "recall_at_5": reranked_at_10,
        "recall_at_10": reranked_at_10 if recall_at_10 is None else recall_at_10,
        "mrr_at_10": reranked_at_10,
        "ndcg_at_10": reranked_at_10,
    }
    return proxy._reference_gates(reranked, abstention)


def test_reference_gates_are_never_a_verdict(proxy):
    """Even metrics that clear the threshold must not render as PASS here."""
    gates = _gates(
        proxy,
        0.99,
        {
            "impossible_abstention_rate": 0.99,
            "answerable_answer_rate": 0.99,
            "top_score_roc_auc": 0.99,
            "best_case_balanced_accuracy": 0.99,
        },
    )
    assert set(gates) == {
        "recall_at_5",
        "recall_at_10",
        "mrr_at_10",
        "ndcg_at_10",
        "impossible_abstention_rate",
        "answerable_answer_rate",
    }
    for name, gate in gates.items():
        assert gate["applicable"] is False, name
        assert gate["passed"] is None, name
        assert gate["actual"] == pytest.approx(0.99), name
        assert "not evaluated" in gate["reason"], name


def test_reference_gates_keep_the_documented_thresholds(proxy):
    gates = _gates(
        proxy,
        0.0,
        {
            "impossible_abstention_rate": 0.0,
            "answerable_answer_rate": 0.0,
            "top_score_roc_auc": 0.5,
            "best_case_balanced_accuracy": 0.5,
        },
    )
    assert gates["recall_at_5"]["threshold"] == 0.85
    assert gates["recall_at_10"]["threshold"] == 0.90
    assert gates["mrr_at_10"]["threshold"] == 0.75
    assert gates["ndcg_at_10"]["threshold"] == 0.80


def test_abstention_gate_records_the_measured_ceiling(proxy):
    gates = _gates(
        proxy,
        0.5,
        {
            "impossible_abstention_rate": 0.78,
            "answerable_answer_rate": 0.3333,
            "top_score_roc_auc": 0.6019,
            "best_case_balanced_accuracy": 0.5929,
        },
    )
    reason = gates["impossible_abstention_rate"]["reason"]
    assert "0.6019" in reason
    assert "0.5929" in reason
    assert "semantic ABSTAIN" in reason
    assert gates["answerable_answer_rate"]["reason"] == reason


# --- §6.4 non-regression baseline --------------------------------------------


SOURCE = {"dataset": "nvidia/TechQA-RAG-Eval", "revision": "deadbeef"}


def test_proxy_regression_creates_the_baseline_once(proxy, tmp_path):
    path = tmp_path / "baseline.json"
    metrics = {"recall_at_10": 0.7286, "mrr_at_10": 0.5699}
    comparison, status, tolerance = proxy._proxy_regression(
        metrics, SOURCE, update=False, baseline_path=path
    )
    assert status == "regression_baseline_created"
    assert tolerance == 0.0
    assert path.exists()
    assert json.loads(path.read_text())["metrics"] == metrics
    assert all(item["passed"] for item in comparison.values())


def test_proxy_regression_passes_at_the_baseline(proxy, tmp_path):
    path = tmp_path / "baseline.json"
    metrics = {"recall_at_10": 0.7286}
    proxy._proxy_regression(metrics, SOURCE, update=False, baseline_path=path)
    comparison, status, _ = proxy._proxy_regression(
        metrics, SOURCE, update=False, baseline_path=path
    )
    assert status == "regression_passed"
    assert comparison["recall_at_10"]["passed"] is True


def test_proxy_regression_detects_a_drop_beyond_tolerance(proxy, tmp_path):
    path = tmp_path / "baseline.json"
    proxy._proxy_regression({"recall_at_10": 0.7286}, SOURCE, update=False, baseline_path=path)
    comparison, status, tolerance = proxy._proxy_regression(
        {"recall_at_10": 0.7010}, SOURCE, update=False, baseline_path=path
    )
    assert status == "regression_failed"
    assert comparison["recall_at_10"]["passed"] is False
    assert comparison["recall_at_10"]["baseline"] == 0.7286
    assert comparison["recall_at_10"]["actual"] == 0.7010
    assert tolerance == 0.005


def test_proxy_regression_absorbs_nondeterminism_inside_tolerance(proxy, tmp_path):
    path = tmp_path / "baseline.json"
    proxy._proxy_regression({"recall_at_10": 0.7286}, SOURCE, update=False, baseline_path=path)
    _, status, _ = proxy._proxy_regression(
        {"recall_at_10": 0.7256}, SOURCE, update=False, baseline_path=path
    )
    assert status == "regression_passed"


def test_proxy_regression_refuses_a_baseline_from_other_revisions(proxy, tmp_path):
    path = tmp_path / "baseline.json"
    proxy._proxy_regression({"recall_at_10": 0.7286}, SOURCE, update=False, baseline_path=path)
    with pytest.raises(RuntimeError, match="frozen under a different"):
        proxy._proxy_regression(
            {"recall_at_10": 0.9},
            {**SOURCE, "revision": "cafebabe"},
            update=False,
            baseline_path=path,
        )


def test_update_flag_refreezes_the_baseline(proxy, tmp_path):
    path = tmp_path / "baseline.json"
    proxy._proxy_regression({"recall_at_10": 0.7286}, SOURCE, update=False, baseline_path=path)
    _, status, tolerance = proxy._proxy_regression(
        {"recall_at_10": 0.6400}, SOURCE, update=True, baseline_path=path
    )
    assert status == "regression_baseline_created"
    assert tolerance == 0.0
    assert json.loads(path.read_text())["metrics"] == {"recall_at_10": 0.6400}


# --- defect 4: the reranker sequence window ----------------------------------
#
# `BgeM3Reranker` builds `CrossEncoder` without a `max_length`, so production inherits the
# pinned tokenizer's `model_max_length` (8192 for bge-reranker-v2-m3). The harness used to
# hardcode 512. The window is now an explicit argument that defaults to "do not override",
# and it is returned so it can key the score cache: without that, scores computed under one
# window would be silently reused for the other.

RERANKER_GLOB = "data/phase4/models/reranker/models--BAAI--bge-reranker-v2-m3/snapshots"


def _fake_reranker_root(tmp_path, snapshot: str = "rev123") -> Path:
    (tmp_path / RERANKER_GLOB / snapshot).mkdir(parents=True)
    return tmp_path


def _fake_cross_encoder(monkeypatch, recorded: dict, *, report_window: int | None = None):
    import sentence_transformers

    class FakeCrossEncoder:
        def __init__(self, path, **kwargs):
            recorded.update(kwargs)
            recorded["path"] = path
            self.max_seq_length = (
                kwargs.get("max_length", 8192) if report_window is None else report_window
            )

    monkeypatch.setattr(sentence_transformers, "CrossEncoder", FakeCrossEncoder)


def test_reranker_default_does_not_override_the_checkpoint_window(proxy, tmp_path, monkeypatch):
    """`max_length=0` must mean "inherit", not "pick a number"."""
    monkeypatch.setattr(proxy, "ROOT", _fake_reranker_root(tmp_path))
    recorded: dict = {}
    _fake_cross_encoder(monkeypatch, recorded)

    _predict, name, revision, window = proxy._reranker("cpu", 8)

    assert "max_length" not in recorded, "the default must leave the checkpoint window alone"
    assert window == 8192, "the returned window is what the cache identity and report record"
    assert name == "BAAI/bge-reranker-v2-m3"
    assert revision == "rev123"


def test_reranker_explicit_window_is_requested_and_reported(proxy, tmp_path, monkeypatch):
    monkeypatch.setattr(proxy, "ROOT", _fake_reranker_root(tmp_path))
    recorded: dict = {}
    _fake_cross_encoder(monkeypatch, recorded)

    _predict, _name, _revision, window = proxy._reranker("cpu", 8, 512)

    assert recorded["max_length"] == 512
    assert window == 512


def test_reranker_rejects_a_silently_different_window(proxy, tmp_path, monkeypatch):
    """A checkpoint that ignores the request would mislabel every cached score."""
    monkeypatch.setattr(proxy, "ROOT", _fake_reranker_root(tmp_path))
    recorded: dict = {}
    _fake_cross_encoder(monkeypatch, recorded, report_window=8192)

    with pytest.raises(RuntimeError, match="max_length=512"):
        proxy._reranker("cpu", 8, 512)


def _reranker_pairs(count: int = 4) -> list[tuple[str, str]]:
    return [(f"q{index}", f"passage {index}") for index in range(count)]


def _scoring_predict(calls: list[int]):
    def predict(pairs):
        calls.append(len(pairs))
        return np.linspace(0.0, 1.0, len(pairs), dtype=np.float32)

    return predict


def test_reranker_score_cache_identity_records_the_window(proxy, tmp_path, monkeypatch):
    monkeypatch.setattr(proxy, "DATA", tmp_path)
    pairs = _reranker_pairs()

    proxy._reranker_scores(pairs, _scoring_predict([]), "rev", tag="pool100", max_length=512)
    identity = json.loads(
        (tmp_path / "techqa_bge_reranker_rev_passages420_pool100.json").read_text()
    )
    assert identity["max_length"] == 512


def test_reranker_score_cache_is_not_reused_across_windows(proxy, tmp_path, monkeypatch):
    """The regression this guards: a 512-window cache satisfying an 8192-window run."""
    monkeypatch.setattr(proxy, "DATA", tmp_path)
    pairs = _reranker_pairs()
    calls: list[int] = []
    predict = _scoring_predict(calls)

    proxy._reranker_scores(pairs, predict, "rev", tag="pool100", max_length=512)
    proxy._reranker_scores(pairs, predict, "rev", tag="pool100", max_length=8192)

    assert calls == [len(pairs), len(pairs)], "a window change must recompute, not reuse"
    identity = json.loads(
        (tmp_path / "techqa_bge_reranker_rev_passages420_pool100.json").read_text()
    )
    assert identity["max_length"] == 8192


def test_reranker_score_cache_is_reused_within_one_window(proxy, tmp_path, monkeypatch):
    monkeypatch.setattr(proxy, "DATA", tmp_path)
    pairs = _reranker_pairs()
    calls: list[int] = []
    predict = _scoring_predict(calls)

    first = proxy._reranker_scores(pairs, predict, "rev", tag="pool100", max_length=8192)
    second = proxy._reranker_scores(pairs, predict, "rev", tag="pool100", max_length=8192)

    assert calls == [len(pairs)], "a verified cache must not be recomputed"
    assert np.array_equal(np.asarray(first), np.asarray(second))
