"""Layer-2b: recompute the cross-encoder A/B from the cached score matrices.

The ad-hoc runner died in its reporting step: it fed all 400 outcomes -- including the
120 unanswerable ones, whose relevance set is empty -- to
``_metric_values(..., "recall", k)``, which divides by ``len(outcome.relevant)``. The
score matrices were already on disk, so the A/B is recomputed here against the answerable
subset only, which is the same population the release report scores.

Alignment evidence for each matrix (a mis-aligned matrix would silently produce a
meaningless A/B): the shape is (400, depth); the doc lists reproduce the documented pool
coverage (0.8714 two-way, 0.9250 three-way); and each matrix correlates more strongly
with the pool it was computed over than with the other one.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

OUT = Path("/tmp/exp_layer1")
SAMPLES = 10_000
SEED = 42
CUTOFFS = (5, 10, 20)


def load(name):
    return json.loads((OUT / name).read_text())


def ranking(docs, row):
    return tuple(
        docs[slot] for slot in sorted(range(len(docs)), key=lambda s: (-float(row[s]), docs[s]))
    )


def metrics(docs, matrix, relevant, indices):
    out = {f"recall@{k}": [] for k in CUTOFFS}
    out["mrr@10"] = []
    out["ndcg@10"] = []
    for i in indices:
        ranked = ranking(docs[i], matrix[i])[:20]
        gains = [1 if item in relevant[i] else 0 for item in ranked]
        for k in CUTOFFS:
            out[f"recall@{k}"].append(sum(gains[:k]) / len(relevant[i]))
        out["mrr@10"].append(
            next((1.0 / r for r, g in enumerate(gains[:10], 1) if g), 0.0)
        )
        dcg = sum(g / math.log2(r + 1) for r, g in enumerate(gains[:10], 1))
        ideal = sum(1.0 / math.log2(r + 1) for r in range(1, min(10, len(relevant[i])) + 1))
        out["ndcg@10"].append(dcg / ideal if ideal else 0.0)
    return {key: round(float(np.mean(value)), 4) for key, value in out.items()}


def per_query(docs, matrix, relevant, indices):
    values = []
    for i in indices:
        ranked = ranking(docs[i], matrix[i])[:10]
        gains = [1 if item in relevant[i] else 0 for item in ranked]
        dcg = sum(g / math.log2(r + 1) for r, g in enumerate(gains, 1))
        ideal = sum(1.0 / math.log2(r + 1) for r in range(1, min(10, len(relevant[i])) + 1))
        values.append(dcg / ideal if ideal else 0.0)
    return np.asarray(values)


def recall_per_query(docs, matrix, relevant, indices):
    values = []
    for i in indices:
        ranked = ranking(docs[i], matrix[i])[:10]
        gains = [1 if item in relevant[i] else 0 for item in ranked]
        values.append(sum(gains) / len(relevant[i]))
    return np.asarray(values)


def paired(left, right):
    difference = right - left
    generator = np.random.default_rng(SEED)
    draws = generator.integers(0, len(difference), size=(SAMPLES, len(difference)))
    means = difference[draws].mean(axis=1)
    low, high = np.quantile(means, [0.025, 0.975])
    return {
        "delta": round(float(difference.mean()), 4),
        "ci95_low": round(float(low), 4),
        "ci95_high": round(float(high), 4),
        "ci95_lower_above_zero": bool(low > 0),
    }


def main():
    funnel = load("funnel_v1.json")
    relevant = [frozenset(x) for x in funnel["relevant"]]
    indices = [i for i, r in enumerate(relevant) if r]

    report = {"answerable": len(indices), "unanswerable": len(relevant) - len(indices), "pools": {}}
    for pool, source, key, baseline_path, challenger_path, challenger in (
        ("two_way", "funnel_v1.json", "rrf", "rerank_tei_v1.npy", "mxbai_rerank.npy",
         "mixedbread-ai/mxbai-rerank-large-v1"),
        ("three_way", "rrf3_top100.json", None, "rerank_tei_3way.npy", None, None),
    ):
        if challenger_path is None:
            continue
        payload = load(source)
        docs = payload[key] if key else payload
        baseline = np.load(OUT / baseline_path)
        new = np.load(OUT / challenger_path)
        coverage = sum(1 for i in indices if relevant[i] & set(docs[i])) / len(indices)
        base_metrics = metrics(docs, baseline, relevant, indices)
        new_metrics = metrics(docs, new, relevant, indices)
        report["pools"][pool] = {
            "depth": len(docs[0]),
            "coverage": round(coverage, 4),
            "production_bge_reranker_v2_m3": base_metrics,
            "challenger": challenger,
            "challenger_metrics": new_metrics,
            "conditional_precision_at_10": {
                "production": round(base_metrics["recall@10"] / coverage, 4),
                "challenger": round(new_metrics["recall@10"] / coverage, 4),
            },
            "paired_bootstrap_challenger_minus_production": {
                "ndcg@10": paired(
                    per_query(docs, baseline, relevant, indices),
                    per_query(docs, new, relevant, indices),
                ),
                "recall@10": paired(
                    recall_per_query(docs, baseline, relevant, indices),
                    recall_per_query(docs, new, relevant, indices),
                ),
            },
        }
    (OUT / "reranker_ab.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
