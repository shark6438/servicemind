"""Attribute the v1.4 vs offline gap to a cause, by reconstructing the harness's own
accounting from its flat reranker cache.

The harness caches one raw score per (query, candidate doc, child passage) pair in
`techqa_bge_reranker_<rev>_passages420_pool100.npy`, ordered
`for query: for doc in fused_ranking: for passage in passages_by_doc[doc]`.
Rebuilding the per-doc max from that file, using the harness's own pool order, must
reproduce the harness's published `production` metrics exactly. If it does, the
published numbers are confirmed and the residual gap to the offline TEI matrix is
attributable to the TEI-vs-in-process scoring path rather than to the metric code.
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
DATA = ROOT / "data/phase4/raw/eval/techqa-rag-eval"
REVISION = "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"


def log(message):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), message), flush=True)


spec = importlib.util.spec_from_file_location(
    "proxy_eval", ROOT / "scripts" / "evaluate_phase4_proxy_release.py"
)
P = importlib.util.module_from_spec(spec)
sys.modules["proxy_eval"] = P
spec.loader.exec_module(P)

funnel = json.loads((OUT / "funnel_v1.json").read_text())
doc_lists = funnel["rrf"]
relevant = [frozenset(x) for x in funnel["relevant"]]
answerable = [i for i, r in enumerate(relevant) if r]

# Rebuild exactly the pair ordering the harness used.
from transformers import AutoTokenizer

snapshot = sorted((ROOT / "data/phase4/models/embedding").glob("models--BAAI--bge-m3/snapshots/*"))[0]
tokenizer = AutoTokenizer.from_pretrained(snapshot / "onnx", local_files_only=True, trust_remote_code=False)
log("tokenizing corpus")
mapping: dict[str, list[str]] = {}
for item in P._passages(P._corpus_documents(), tokenizer):
    mapping.setdefault(item.doc_id, []).append(item.text)
log("passage map: %d docs" % len(mapping))

flat = np.load(DATA / ("techqa_bge_reranker_%s_passages420_pool100.npy" % REVISION))
log("harness flat cache: %s" % (flat.shape,))

matrix = np.zeros((len(doc_lists), len(doc_lists[0])), dtype=np.float32)
offset = 0
for i, docs in enumerate(doc_lists):
    for slot, doc_id in enumerate(docs):
        children = mapping[doc_id]
        span = flat[offset:offset + len(children)]
        matrix[i, slot] = float(np.max(span)) if len(children) else 0.0
        offset += len(children)
log("reconstructed matrix %s from %d pairs" % (matrix.shape, offset))
assert offset == flat.shape[0], (offset, flat.shape)

# A monotone transform (the harness applies sigmoid downstream) cannot change a ranking,
# so rankings are compared raw.
def ranking(docs, row):
    return tuple(docs[s] for s in sorted(range(len(docs)), key=lambda s: (-float(row[s]), docs[s])))


def score(ranks):
    items = [SimpleNamespace(relevant=relevant[i], rank=ranks[i][:20]) for i in answerable]
    return {
        name: round(statistics.fmean(P._metric_values(items, "rank", metric, k)), 4)
        for name, (metric, k) in {
            "recall_at_5": ("recall", 5), "recall_at_10": ("recall", 10),
            "recall_at_20": ("recall", 20), "mrr_at_10": ("mrr", 10),
            "ndcg_at_10": ("ndcg", 10),
        }.items()
    }


alpha = P._rerank_weight()
rebuilt, offline_ranks = [], []
offline_matrix = np.load(OUT / "rerank_tei_v1.npy")
for i, docs in enumerate(doc_lists):
    rebuilt.append(tuple(P._blend_ranking(docs, {d: float(matrix[i][j]) for j, d in enumerate(docs)},
                                          P._rrf_scores(funnel["bm25"][i], funnel["dense"][i]),
                                          rerank_weight=alpha, depth=len(docs))))
    offline_ranks.append(ranking(docs, offline_matrix[i]))

rebuilt_metrics = score(rebuilt)
log("reconstructed production blend: %s" % rebuilt_metrics)

published = json.loads((ROOT / "evaluation/reports/phase4_proxy_release_latest.json").read_text())
pub = published["retrieval"]["production"]
expected = {k: pub[k] for k in rebuilt_metrics}
log("harness published production:   %s" % expected)
log("exact match: %s" % (rebuilt_metrics == expected,))

# Pure-rerank rankings: how often do the two scoring paths order the pool differently?
base_ranks = [ranking(doc_lists[i], matrix[i]) for i in range(len(doc_lists))]
identical = sum(1 for a, b in zip(base_ranks, offline_ranks, strict=True) if a == b)
log("pure-rerank top-100 rankings identical (harness vs TEI): %d/%d" % (identical, len(doc_lists)))
log("offline matrix production blend: %s" % score(offline_ranks))

(OUT / "reconcile_latest.json").write_text(json.dumps({
    "harness_flat_cache": list(flat.shape),
    "reconstructed_matrix": list(matrix.shape),
    "production_blend_from_harness_cache": rebuilt_metrics,
    "production_published_v14": expected,
    "exact_match": rebuilt_metrics == expected,
    "pure_rerank_rankings_identical_harness_vs_tei": identical,
    "production_blend_from_offline_tei_matrix": score(offline_ranks),
}, indent=2) + "\n", encoding="utf-8")
