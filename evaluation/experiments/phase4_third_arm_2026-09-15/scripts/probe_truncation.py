"""Does the harness's max_length=512 truncation change what the reranker says?

Child passages run p50=1313 BGE tokens, so a 512-token window discards ~61% of every
passage the cross-encoder scores. Production's `BgeM3Reranker` passes no max_length to
`CrossEncoder`, so it inherits the tokenizer's model_max_length (8192) and never truncates
at 512. This probe rescores a sample of the depth-100 pool under both windows and measures
how much the per-doc scores, the pool ordering and the blended top-10 move.
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
SAMPLE_QUERIES = 40
BATCH = 32


def log(message):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), message), flush=True)


spec = importlib.util.spec_from_file_location(
    "proxy_eval", ROOT / "scripts/evaluate_phase4_proxy_release.py"
)
P = importlib.util.module_from_spec(spec)
sys.modules["proxy_eval"] = P
spec.loader.exec_module(P)

funnel = json.loads((OUT / "funnel_v1.json").read_text())
doc_lists = funnel["rrf"]
relevant = [frozenset(x) for x in funnel["relevant"]]
answerable = [i for i, r in enumerate(relevant) if r]
sample = answerable[:SAMPLE_QUERIES]

from sentence_transformers import CrossEncoder
from transformers import AutoTokenizer

snapshot = sorted((ROOT / "data/phase4/models/embedding").glob("models--BAAI--bge-m3/snapshots/*"))[0]
tokenizer = AutoTokenizer.from_pretrained(snapshot / "onnx", local_files_only=True, trust_remote_code=False)
mapping: dict[str, list[str]] = {}
for item in P._passages(P._corpus_documents(), tokenizer):
    mapping.setdefault(item.doc_id, []).append(item.text)
log("passage map: %d docs" % len(mapping))

reranker_snap = sorted((ROOT / "data/phase4/models/reranker").glob(
    "models--BAAI--bge-reranker-v2-m3/snapshots/*"))[0]

def score_window(window: int, spans, pairs):
    cache = OUT / ("truncation_probe_window%d.npy" % window)
    if cache.exists():
        values = np.load(cache)
        if values.shape == (len(pairs),):
            log("window=%d: cache hit" % window)
            return values, spans
    model = CrossEncoder(str(reranker_snap), device="cuda:0", max_length=window, trust_remote_code=False)
    log("window=%d -> CrossEncoder max_length=%s" % (window, model.max_seq_length))
    tick = time.perf_counter()
    values = np.asarray(model.predict(pairs, batch_size=BATCH, show_progress_bar=False)).reshape(-1)
    log("window=%d: scored %d pairs in %.1fs" % (window, len(pairs), time.perf_counter() - tick))
    np.save(cache, values)
    import gc, torch
    del model
    gc.collect(); torch.cuda.empty_cache()
    return values, spans

def build(spans, values, n_docs=100):
    matrix = {}
    for i, span, start in spans:
        row = np.zeros(n_docs, dtype=np.float32)
        for slot, (a, b) in enumerate(span):
            row[slot] = float(np.max(values[start + a:start + b])) if b > a else 0.0
        matrix[i] = row
    return matrix

pairs = []
spans = []
for i in sample:
    docs = doc_lists[i]
    texts, span, cursor = [], [], 0
    for doc_id in docs:
        children = mapping[doc_id]
        texts.extend(children)
        span.append((cursor, cursor + len(children)))
        cursor += len(children)
    spans.append((i, span, len(pairs)))
    pairs.extend((funnel["questions"][i], t) for t in texts)
log("sample: %d queries, %d pairs" % (len(sample), len(pairs)))

log("scoring sample at both windows")
payload512, _ = score_window(512, spans, pairs)
payload8192, _ = score_window(8192, spans, pairs)
m512 = build(spans, payload512)
m8192 = build(spans, payload8192)

summary = {"sample_queries": len(sample), "pairs_scored": int(len(payload512))}
spearman, rank1_same, top10_overlap, doc_metric = [], 0, [], []
for i in sample:
    a = list(np.argsort(-m512[i], kind="stable"))
    b = list(np.argsort(-m8192[i], kind="stable"))
    ra = np.empty(len(a)); ra[a] = np.arange(len(a))
    rb = np.empty(len(b)); rb[b] = np.arange(len(b))
    spearman.append(float(np.corrcoef(ra, rb)[0, 1]))
    rank1_same += int(a[0] == b[0])
    top10_overlap.append(len(set(a[:10]) & set(b[:10])) / 10)

score_delta = np.concatenate([(m8192[i] - m512[i]) for i in sample])
summary.update({
    "spearman_pool_mean": round(float(np.mean(spearman)), 4),
    "spearman_pool_min": round(float(np.min(spearman)), 4),
    "rank1_agreement": rank1_same,
    "top10_set_overlap_mean": round(float(np.mean(top10_overlap)), 4),
    "per_doc_score_delta_mean": round(float(score_delta.mean()), 4),
    "per_doc_score_delta_p05": round(float(np.percentile(score_delta, 5)), 4),
    "per_doc_score_delta_p95": round(float(np.percentile(score_delta, 95)), 4),
    "score512_mean": round(float(np.concatenate([m512[i] for i in sample]).mean()), 4),
    "score8192_mean": round(float(np.concatenate([m8192[i] for i in sample]).mean()), 4),
})
log(json.dumps(summary, indent=2))

# Blended top-10 verdict on the sample, mirroring the production shape.
alpha = P._rerank_weight()
def blend_metrics(matrix):
    items = []
    for i in sample:
        docs = doc_lists[i]
        rank = P._blend_ranking(docs, {d: float(matrix[i][j]) for j, d in enumerate(docs)},
                                P._rrf_scores(funnel["bm25"][i], funnel["dense"][i]),
                                rerank_weight=alpha, depth=len(docs))
        items.append(SimpleNamespace(relevant=relevant[i], rank=rank))
    return {
        name: round(statistics.fmean(P._metric_values(items, "rank", metric, k)), 4)
        for name, (metric, k) in {"recall_at_5": ("recall", 5), "recall_at_10": ("recall", 10),
                                  "mrr_at_10": ("mrr", 10), "ndcg_at_10": ("ndcg", 10)}.items()
    }

summary["blend_at_512"] = blend_metrics(m512)
summary["blend_at_8192"] = blend_metrics(m8192)
log("blended production shape on the sample: %s" % json.dumps(
    {"512": summary["blend_at_512"], "8192": summary["blend_at_8192"]}, indent=2))

(OUT / "truncation_probe.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
