"""Layer-2 experiment d: the third arm, fused the way production would fuse it.

Layer 2a/2c (and `docs/PHASE4_RAG_QUALITY_ROOT_CAUSE_2026-09-15.md` §3.3) built the
three-way pool as `RRF(RRF(bm25, bge, k=200), qwen3)` - a *nested* fusion in which the
two-arm fused list competes with the qwen3 list as a single co-equal arm. Production
never does that: `rag/opensearch.py` puts the dense knn and every BM25 rewrite in one
OpenSearch `hybrid` query, and the cluster RRF pipeline fuses each sub-query with its
own rank at `rank_constant: 60`. Adding a third dense arm in production therefore means
`1/(60+r_bm25) + 1/(60+r_bge) + 1/(60+r_qwen3)`, three equal-weight arms.

The distinction is not cosmetic: the nested pool's coverage@100 is 0.9250 while the
true three-arm pool's is 0.9143, and their top-100 *sets* differ for 400/400 queries.

This script reranks the true three-arm pool with the production TEI reranker and
re-asks the §4.1 adoption question on it.
"""

from __future__ import annotations

import importlib.util
import json
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path("/home/shihongye/data1/servicemind")
OUT = Path("/tmp/exp_layer1")
ALPHA = 0.85
NDCG_GAIN_REQUIRED = 0.02
SAMPLES, SEED = 10_000, 42

_spec_p = importlib.util.spec_from_file_location(
    "proxy_eval", ROOT / "scripts/evaluate_phase4_proxy_release.py")
P = importlib.util.module_from_spec(_spec_p)
sys.modules["proxy_eval"] = P
_spec_p.loader.exec_module(P)

_spec_l1 = importlib.util.spec_from_file_location("layer1", OUT / "run_layer1.py")
L1 = importlib.util.module_from_spec(_spec_l1)
sys.modules["layer1"] = L1
_spec_l1.loader.exec_module(L1)

ARMS = ("rrf_2way", "pure_2way", "blend_2way", "rrf_true3", "pure_true3", "blend_true3")


def log(message):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), message), flush=True)


def fused_scores(*arms):
    """Production RRF: every arm contributes 1/(60 + rank); a missing arm adds zero."""
    scores = {}
    for arm in arms:
        for rank, doc in enumerate(arm, 1):
            scores[doc] = scores.get(doc, 0.0) + 1.0 / (P.RRF_RANK_CONSTANT + rank)
    return scores


def order(docs, scores, mode):
    if mode == "rrf":
        return tuple(docs)
    if mode == "pure":
        return tuple(sorted(docs, key=lambda d: (-scores["rerank"][d], d)))
    return P._blend_ranking(
        docs, scores["rerank"], scores["rrf"], rerank_weight=ALPHA, depth=len(docs)
    )


def paired(left, right, metric="ndcg", k=10):
    a = np.asarray(P._metric_values(left, "rank", metric, k))
    b = np.asarray(P._metric_values(right, "rank", metric, k))
    diff = b - a
    draws = np.random.default_rng(SEED).integers(0, len(diff), size=(SAMPLES, len(diff)))
    low, high = np.quantile(diff[draws].mean(axis=1), [0.025, 0.975])
    return {
        "delta": round(float(diff.mean()), 4),
        "ci95_low": round(float(low), 4),
        "ci95_high": round(float(high), 4),
        "ci95_lower_above_zero": bool(low > 0),
    }


def main():
    funnel = json.loads((OUT / "funnel_v1.json").read_text())
    qwen3 = json.loads((OUT / "qwen3_dense_top100.json").read_text())
    true3 = json.loads((OUT / "rrf3_true_top100.json").read_text())
    relevant = [frozenset(x) for x in funnel["relevant"]]
    answerable = [i for i, rel in enumerate(relevant) if rel]
    pool_2way = funnel["rrf"]
    log("answerable queries: %d" % len(answerable))

    cache = OUT / "rerank_tei_true3.npy"
    identity = OUT / "rerank_tei_true3.json"
    if cache.exists() and identity.exists() and json.loads(identity.read_text())["docs"] == true3:
        rerank_true3 = np.load(cache)
        log("true-3-way rerank cache verified")
    else:
        import httpx

        mapping = L1.passages_by_doc()
        rerank_true3 = np.zeros((len(true3), len(true3[0])), dtype=np.float32)
        tick = time.perf_counter()
        with httpx.Client(timeout=600, trust_env=False) as client:
            for i, docs in enumerate(true3):
                texts, spans = [], []
                for doc_id in docs:
                    children = mapping[doc_id]
                    spans.append((len(texts), len(texts) + len(children)))
                    texts.extend(children)
                flat = L1.tei_rerank(client, funnel["questions"][i], texts)
                for slot, (start, stop) in enumerate(spans):
                    rerank_true3[i, slot] = max(flat[start:stop]) if stop > start else 0.0
                if i % 20 == 0 or i == len(true3) - 1:
                    done = i + 1
                    rate = done / (time.perf_counter() - tick)
                    log("reranked %d/%d q (%.2f q/s, eta %.1f min)"
                        % (done, len(true3), rate, (len(true3) - done) / rate / 60))
                    np.save(cache, rerank_true3)
        np.save(cache, rerank_true3)
        identity.write_text(json.dumps({"docs": true3}))
        log("true-3-way rerank cached")

    rerank_2way = np.load(OUT / "rerank_tei_v1.npy")

    outcomes = {name: [] for name in ARMS}
    rrf2_cache, rrf3_cache = [], []
    for i in answerable:
        rrf2 = fused_scores(funnel["bm25"][i], funnel["dense"][i])
        rrf3 = fused_scores(funnel["bm25"][i], funnel["dense"][i], qwen3[i])
        rrf2_cache.append(rrf2)
        rrf3_cache.append(rrf3)
        docs2, docs3 = pool_2way[i], true3[i]
        r2 = {"rerank": {d: float(rerank_2way[i][j]) for j, d in enumerate(docs2)}, "rrf": rrf2}
        r3 = {"rerank": {d: float(rerank_true3[i][j]) for j, d in enumerate(docs3)}, "rrf": rrf3}
        for name, docs, sc in (
            ("rrf_2way", docs2, r2), ("pure_2way", docs2, r2), ("blend_2way", docs2, r2),
            ("rrf_true3", docs3, r3), ("pure_true3", docs3, r3), ("blend_true3", docs3, r3),
        ):
            mode = name.split("_")[0]
            outcomes[name].append(
                SimpleNamespace(relevant=relevant[i], rank=order(docs, sc, mode))
            )

    summary = {}
    for name in ARMS:
        summary[name] = {
            metric: round(statistics.fmean(P._metric_values(outcomes[name], "rank", m, k)), 4)
            for metric, m, k in (
                ("R@5", "recall", 5), ("R@10", "recall", 10),
                ("R@20", "recall", 20), ("MRR@10", "mrr", 10), ("NDCG@10", "ndcg", 10),
            )
        }
    log(json.dumps(summary, indent=2))

    # Anchors: the two-way arms must reproduce the numbers verified elsewhere.
    assert summary["rrf_2way"]["R@10"] == 0.7357, summary["rrf_2way"]
    assert summary["pure_2way"]["R@10"] == 0.7357 and summary["pure_2way"]["NDCG@10"] == 0.6036
    assert summary["blend_2way"]["R@10"] == 0.7643 and summary["blend_2way"]["NDCG@10"] == 0.6279
    log("two-way anchors verified against the layer-1 corrected table")

    base = np.asarray(P._metric_values(outcomes["blend_2way"], "rank", "ndcg", 10))
    hard = np.asarray([p for p, v in enumerate(base) if v < 1.0])
    log("hard subset (production blend misses gold in top-10): %d/%d" % (len(hard), len(answerable)))

    report = {
        "schema": "servicemind-layer2-third-arm-true-fusion-v1",
        "note": "offline layer-2 experiment; not a repo release report",
        "why": (
            "the nested fusion used by layer 2a/2c RRF(RRF(bm25,bge),qwen3) is not what "
            "production's OpenSearch hybrid + RRF pipeline would run for three arms; this "
            "script fuses three equal-weight arms with rank_constant 60"
        ),
        "fused_coverage_at_100": {"two_way": 0.8714, "three_way_nested": 0.9250, "three_way_true": 0.9143},
        "arms": summary,
        "hard_subset": {"rule": "production blend misses gold in top-10", "queries": int(len(hard)),
                        "of": len(answerable)},
    }

    def subset(values, idx):
        return values[idx]

    for metric, k, label in (("ndcg", 10, "NDCG@10"), ("recall", 10, "Recall@10"), ("recall", 5, "Recall@5")):
        values = np.asarray(P._metric_values(outcomes["blend_true3"], "rank", metric, k))
        reference = np.asarray(P._metric_values(outcomes["blend_2way"], "rank", metric, k))
        diff = values - reference
        draws = np.random.default_rng(SEED).integers(0, len(diff), size=(SAMPLES, len(diff)))
        low, high = np.quantile(diff[draws].mean(axis=1), [0.025, 0.975])
        report.setdefault("third_arm_vs_production", {})[label] = {
            "delta": round(float(diff.mean()), 4),
            "ci95_low": round(float(low), 4),
            "ci95_high": round(float(high), 4),
            "ci95_lower_above_zero": bool(low > 0),
        }
        diff_h = diff[hard]
        draws_h = np.random.default_rng(SEED).integers(0, len(diff_h), size=(SAMPLES, len(diff_h)))
        low_h, high_h = np.quantile(diff_h[draws_h].mean(axis=1), [0.025, 0.975])
        report.setdefault("third_arm_vs_production_hard_subset", {})[label] = {
            "delta": round(float(diff_h.mean()), 4),
            "ci95_low": round(float(low_h), 4),
            "ci95_high": round(float(high_h), 4),
            "ci95_lower_above_zero": bool(low_h > 0),
        }

    hard_check = report["third_arm_vs_production_hard_subset"]["NDCG@10"]
    report["verdict"] = {
        "arm": "qwen3-embedding-0.6b dense, fused as production would fuse it",
        "hard_subset_ndcg_at_10_gain": hard_check["delta"],
        "required_gain": NDCG_GAIN_REQUIRED,
        "ci95_low": hard_check["ci95_low"],
        "adopt": bool(hard_check["delta"] >= NDCG_GAIN_REQUIRED and hard_check["ci95_lower_above_zero"]),
    }
    log(json.dumps(report["verdict"], indent=2))
    log(json.dumps({k: report[k] for k in ("third_arm_vs_production", "third_arm_vs_production_hard_subset")}, indent=2))
    (OUT / "layer2d_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
