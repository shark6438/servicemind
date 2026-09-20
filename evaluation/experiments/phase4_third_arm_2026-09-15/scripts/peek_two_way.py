"""Early read of the two-way (production) pool reranker A/B, from cached matrices."""

import importlib.util
import json
import statistics
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path("/home/shihongye/data1/servicemind")
OUT = Path("/tmp/exp_layer1")

spec = importlib.util.spec_from_file_location(
    "proxy_eval", ROOT / "scripts/evaluate_phase4_proxy_release.py"
)
P = importlib.util.module_from_spec(spec)
sys.modules["proxy_eval"] = P
spec.loader.exec_module(P)

funnel = json.loads((OUT / "funnel_v1.json").read_text())
relevant = [frozenset(x) for x in funnel["relevant"]]
answerable = [i for i, rel in enumerate(relevant) if rel]

doc_lists = funnel["rrf"]
assert json.loads((OUT / "gte_modernbert_two_way.json").read_text()) == doc_lists

METRICS = {
    "recall_at_5": ("recall", 5),
    "recall_at_10": ("recall", 10),
    "recall_at_20": ("recall", 20),
    "mrr_at_10": ("mrr", 10),
    "ndcg_at_5": ("ndcg", 5),
    "ndcg_at_10": ("ndcg", 10),
}


def outcomes(matrix):
    out = []
    for i in answerable:
        docs, row = doc_lists[i], matrix[i]
        ranking = tuple(
            docs[j] for j in sorted(range(len(docs)), key=lambda j: (-float(row[j]), docs[j]))
        )
        out.append(SimpleNamespace(relevant=relevant[i], rank=ranking))
    return out


def scores(items):
    return {
        name: round(statistics.fmean(P._metric_values(items, "rank", metric, k)), 4)
        for name, (metric, k) in METRICS.items()
    }


def paired(left, right, metric="ndcg", k=10, samples=10_000, seed=42):
    a = np.asarray(P._metric_values(left, "rank", metric, k))
    b = np.asarray(P._metric_values(right, "rank", metric, k))
    diff = b - a
    draws = np.random.default_rng(seed).integers(0, len(diff), size=(samples, len(diff)))
    low, high = np.quantile(diff[draws].mean(axis=1), [0.025, 0.975])
    return {
        "delta": round(float(diff.mean()), 4),
        "ci95_low": round(float(low), 4),
        "ci95_high": round(float(high), 4),
        "ci95_lower_above_zero": bool(low > 0),
    }


old = outcomes(np.load(OUT / "rerank_tei_v1.npy"))
new = outcomes(np.load(OUT / "gte_modernbert_two_way.npy"))
print(json.dumps({
    "pool": "two_way (bm25 + bge-m3), production pool, depth 100",
    "bge_reranker_v2_m3": scores(old),
    "gte_reranker_modernbert_base": scores(new),
    "paired_new_vs_production": {
        "ndcg_at_10": paired(old, new),
        "recall_at_10": paired(old, new, "recall", 10),
        "recall_at_5": paired(old, new, "recall", 5),
    },
}, indent=2))
