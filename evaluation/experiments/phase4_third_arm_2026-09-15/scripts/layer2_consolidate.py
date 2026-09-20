"""Consolidated layer-2 view: four configurations, and the hard-subset confound.

The layer2b `third_arm_decision` block re-defines the hard subset per reranker, so its
two "hard subset" deltas are not comparable: under the recall-weaker reranker the subset
is larger and easier to improve on. This script reports the subset sizes and the
absolute metrics side by side.
"""

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
pool_2way = funnel["rrf"]
pool_3way = json.loads((OUT / "rrf3_top100.json").read_text())
relevant = [frozenset(x) for x in funnel["relevant"]]
answerable = [i for i, rel in enumerate(relevant) if rel]

MATRICES = {
    ("two_way", "bge"): np.load(OUT / "rerank_tei_v1.npy"),
    ("two_way", "gte"): np.load(OUT / "gte_modernbert_two_way.npy"),
    ("three_way", "bge"): np.load(OUT / "rerank_tei_3way.npy"),
    ("three_way", "gte"): np.load(OUT / "gte_modernbert_three_way.npy"),
}
POOLS = {"two_way": pool_2way, "three_way": pool_3way}
# Top-100 coverage of the gold document, measured in layer 2a.
COVERAGE = {"two_way": 0.8714, "three_way": 0.9250}


def outcomes(pool, matrix):
    docs_lists = POOLS[pool]
    out = []
    for i in answerable:
        docs, row = docs_lists[i], matrix[i]
        ranking = tuple(
            docs[j] for j in sorted(range(len(docs)), key=lambda j: (-float(row[j]), docs[j]))
        )
        out.append(SimpleNamespace(relevant=relevant[i], rank=ranking))
    return out


rows = {}
for key in (("two_way", "bge"), ("two_way", "gte"), ("three_way", "bge"), ("three_way", "gte")):
    pool, reranker = key
    items = outcomes(pool, MATRICES[key])
    name = f"{pool}+{reranker}"
    ndcg10 = statistics.fmean(P._metric_values(items, "rank", "ndcg", 10))
    recall10 = statistics.fmean(P._metric_values(items, "rank", "recall", 10))
    rows[name] = {
        "recall_at_5": round(statistics.fmean(P._metric_values(items, "rank", "recall", 5)), 4),
        "recall_at_10": round(recall10, 4),
        "mrr_at_10": round(statistics.fmean(P._metric_values(items, "rank", "mrr", 10)), 4),
        "ndcg_at_10": round(ndcg10, 4),
        "pool_coverage_at_100": COVERAGE[pool],
        "conditional_precision_at_10": round(recall10 / COVERAGE[pool], 4),
        "hard_subset_size": sum(
            1 for o in items if P._metric_values([o], "rank", "ndcg", 10)[0] < 1.0
        ),
    }

print(json.dumps({
    "note": "absolute metrics; conditional precision = recall@10 / pool coverage@100",
    "configurations": rows,
    "best_ndcg_at_10": max(rows, key=lambda n: rows[n]["ndcg_at_10"]),
    "best_recall_at_10": max(rows, key=lambda n: rows[n]["recall_at_10"]),
    "production_today": "two_way+bge reranked, then the 0.85/0.15 production blend",
}, indent=2))
(OUT / "layer2_consolidated.json").write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
