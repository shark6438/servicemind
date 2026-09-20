"""Are the in-process and TEI reranker scoring paths equivalent at the production window?

The 512-era comparison (`diagnose_scoring_paths.py`) mixed two variables: the harness
truncated at 512 while TEI served the checkpoint's own 8192 window. This script redoes
the comparison at a matched window (both 8192, i.e. after the §2.6 fix) and also asserts
that the framework's published production arm equals the offline TEI-path blend arm
number for number, CIs included.

Inputs (all already on disk, no model inference):
  - the v1.5 candidate diagnostics, which carry per-query in-process reranker scores
  - `rerank_tei_v1.{json,npy}`: the offline TEI score matrix over the same pool
  - `evaluation/reports/phase4_proxy_release_latest.json`: the framework's own report
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from scipy.stats import spearmanr

ROOT = Path("/home/shihongye/data1/servicemind")
OUT = Path("/tmp/exp_layer1")
DIAG = ROOT / "data/phase4/raw/eval/techqa-rag-eval/phase4_proxy_candidate_diagnostics.json"


def log(message):
    print("[compare] %s" % message, flush=True)


diag = json.loads(DIAG.read_text(encoding="utf-8"))
tei_docs = json.loads((OUT / "rerank_tei_v1.json").read_text(encoding="utf-8"))["docs"]
tei = np.load(OUT / "rerank_tei_v1.npy")
assert len(diag) == len(tei_docs) == tei.shape[0], (len(diag), len(tei_docs), tei.shape)

same_pool = sum(1 for d, t in zip(diag, tei_docs, strict=True) if set(d["reranker_scores"]) == set(t))
log("identical candidate pools: %d/%d" % (same_pool, len(diag)))

spearman, top10, rank1, deltas, identical_order = [], [], 0, [], 0
for row, (d, t) in enumerate(zip(diag, tei_docs, strict=True)):
    scores = d["reranker_scores"]
    if not all(doc in scores for doc in t):
        continue
    a = np.array([scores[doc] for doc in t])
    b = tei[row]
    deltas.append(float(np.mean(a - b)))

    ra = np.empty(len(t))
    rb = np.empty(len(t))
    ia, ib = np.argsort(-a), np.argsort(-b)
    ra[ia], rb[ib] = np.arange(len(t)), np.arange(len(t))
    spearman.append(float(spearmanr(ra, rb).statistic))
    top10.append(len(set(ia[:10]) & set(ib[:10])) / 10)
    rank1 += int(ia[0] == ib[0])
    identical_order += int(np.array_equal(ia, ib))

spearman = np.array(spearman)
report = {
    "candidate_pools_identical": "%d/%d" % (same_pool, len(diag)),
    "queries_compared": len(spearman),
    "spearman_mean": round(float(spearman.mean()), 4),
    "spearman_p05": round(float(np.percentile(spearman, 5)), 4),
    "spearman_min": round(float(spearman.min()), 4),
    "rank1_agreement": "%d/%d" % (rank1, len(spearman)),
    "top10_overlap_mean": round(float(np.mean(top10)), 4),
    "per_doc_score_delta_mean": round(float(np.mean(deltas)), 6),
    "fully_identical_orderings": "%d/%d" % (identical_order, len(spearman)),
}
log(json.dumps(report, indent=2))

# End-to-end: the framework's production arm vs the offline TEI path's blend arm.
published = json.loads(
    (ROOT / "evaluation/reports/phase4_proxy_release_latest.json").read_text(encoding="utf-8")
)["retrieval"]["production"]
offline = json.loads((OUT / "layer1_report.json").read_text(encoding="utf-8"))["arms"]["blend100"]
keys = ["recall_at_5", "recall_at_10", "recall_at_20", "mrr_at_10", "ndcg_at_10"]
comparison, all_equal = {}, True
for key in keys:
    value_equal = offline[key] == published[key]
    ci_equal = list(offline[key + "_ci95"]) == list(published[key + "_ci95"])
    all_equal &= value_equal and ci_equal
    comparison[key] = {
        "offline_tei": offline[key],
        "in_process": published[key],
        "equal": value_equal,
        "ci_equal": ci_equal,
    }
    log(
        "%-13s offline=%.4f in-process=%.4f equal=%s ci_equal=%s"
        % (key, offline[key], published[key], value_equal, ci_equal)
    )
log("production arm identical to the offline TEI blend arm (10 numbers): %s" % all_equal)

report["production_arm_comparison"] = comparison
report["production_arm_all_ten_numbers_identical"] = all_equal
(OUT / "path_compare.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
log("wrote %s" % (OUT / "path_compare.json"))
