"""Layer-2 experiment b: is the reranking stage the bottleneck?

Layers 2a + 2c showed the paradox: a third dense arm lifts RRF NDCG@10 by +4.75pt
(paired 95% CI lower bound above zero) and lifts the pool ceiling from 0.8714 to
0.9250, yet after the cross-encoder the gain collapses to +0.06pt with a CI straddling
zero. Conditional precision fell from 0.844 to 0.811 as the pool grew, so the reranker,
not the retrieval pool, is what caps Recall@10.

This experiment swaps the reranker on the *same* candidate pools, so the reranker is
the only variable:

  pools    : two-way (bm25 + bge-m3, production) and three-way (+ qwen3)
  rerankers: BAAI/bge-reranker-v2-m3 (production, scores reused from the layer-1 TEI
             run so the baseline is identical) and
             Alibaba-NLP/gte-reranker-modernbert-base

`Alibaba-NLP/gte-multilingual-reranker-base` was the first candidate and is deliberately
not used: its config declares `model_type: "new"` with packed QKV and RoPE, so loading
it requires `trust_remote_code=True` - executing code fetched from a third-party
mirror. The project pins `trust_remote_code=False` everywhere, and the ModernBERT
variant is a standard architecture that needs no remote code.
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
NEW_RERANKER = ROOT / "data/phase4/models/layer2/gte-reranker-modernbert-base"
MAX_LENGTH = 1024
BATCH = 64
BOOTSTRAP_SAMPLES = 10_000
BOOTSTRAP_SEED = 42

spec = importlib.util.spec_from_file_location(
    "proxy_eval", ROOT / "scripts" / "evaluate_phase4_proxy_release.py"
)
P = importlib.util.module_from_spec(spec)
sys.modules["proxy_eval"] = P
spec.loader.exec_module(P)

METRICS = {
    "recall_at_5": ("recall", 5),
    "recall_at_10": ("recall", 10),
    "recall_at_20": ("recall", 20),
    "mrr_at_10": ("mrr", 10),
    "ndcg_at_5": ("ndcg", 5),
    "ndcg_at_10": ("ndcg", 10),
}
POOLS = {
    "two_way": ("funnel_v1.json", "rrf", "rerank_tei_v1.npy"),
    "three_way": ("rrf3_top100.json", None, "rerank_tei_3way.npy"),
}


def log(message):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), message), flush=True)


def load_pool(name):
    source, key, baseline = POOLS[name]
    payload = json.loads((OUT / source).read_text())
    doc_lists = payload[key] if key else payload
    return doc_lists, np.load(OUT / baseline)


def rerank_pool(name, funnel, doc_lists, model, passages_by_doc):
    import torch

    cache = OUT / ("gte_modernbert_%s.npy" % name)
    docs_cache = OUT / ("gte_modernbert_%s.json" % name)
    if cache.exists() and docs_cache.exists() and json.loads(docs_cache.read_text()) == doc_lists:
        log("%s: rerank cache verified" % name)
        return np.load(cache)
    matrix = np.zeros((len(doc_lists), len(doc_lists[0])), dtype=np.float32)
    tick = time.perf_counter()
    with torch.inference_mode():
        for position, docs in enumerate(doc_lists):
            texts = []
            spans = []
            for doc_id in docs:
                children = passages_by_doc[doc_id]
                spans.append((len(texts), len(texts) + len(children)))
                texts.extend(children)
            scores = model.predict(
                [(funnel["questions"][position], text) for text in texts],
                batch_size=BATCH,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
            for slot, (start, stop) in enumerate(spans):
                matrix[position, slot] = float(np.max(scores[start:stop])) if stop > start else 0.0
            if position % 10 == 0 or position == len(doc_lists) - 1:
                done = position + 1
                rate = done / (time.perf_counter() - tick)
                log("%s: %d/%d q (%.2f q/s, eta %.1f min)"
                    % (name, done, len(doc_lists), rate, (len(doc_lists) - done) / rate / 60))
                np.save(cache, matrix)
    np.save(cache, matrix)
    docs_cache.write_text(json.dumps(doc_lists))
    return matrix


def outcomes_for(doc_lists, matrix, relevant, indices):
    """`indices` is restricted to answerable queries: an unanswerable query has an
    empty relevance set and every ranking metric divides by its size."""
    out = []
    for position in indices:
        docs = doc_lists[position]
        row = matrix[position]
        ranking = tuple(
            docs[slot] for slot in sorted(range(len(docs)), key=lambda s: (-float(row[s]), docs[s]))
        )
        out.append(SimpleNamespace(relevant=relevant[position], rank=ranking))
    return out


def scores_for(outcomes):
    return {
        name: round(statistics.fmean(P._metric_values(outcomes, "rank", metric, k)), 4)
        for name, (metric, k) in METRICS.items()
    }


def paired(left, right, metric="ndcg", k=10):
    a = np.asarray(P._metric_values(left, "rank", metric, k))
    b = np.asarray(P._metric_values(right, "rank", metric, k))
    difference = b - a
    generator = np.random.default_rng(BOOTSTRAP_SEED)
    draws = generator.integers(0, len(difference), size=(BOOTSTRAP_SAMPLES, len(difference)))
    means = difference[draws].mean(axis=1)
    low, high = np.quantile(means, [0.025, 0.975])
    return {
        "delta": round(float(difference.mean()), 4),
        "ci95_low": round(float(low), 4),
        "ci95_high": round(float(high), 4),
        "ci95_lower_above_zero": bool(low > 0),
    }


def main():
    from sentence_transformers import CrossEncoder
    from transformers import AutoTokenizer

    funnel = json.loads((OUT / "funnel_v1.json").read_text())
    relevant = [frozenset(x) for x in funnel["relevant"]]
    answerable = [i for i, rel in enumerate(relevant) if rel]
    log("answerable queries: %d/%d" % (len(answerable), len(relevant)))

    snapshot = sorted(
        (ROOT / "data/phase4/models/embedding").glob("models--BAAI--bge-m3/snapshots/*")
    )[0]
    tokenizer = AutoTokenizer.from_pretrained(str(snapshot / "onnx"), local_files_only=True)
    passages_by_doc = {}
    for item in P._passages(P._corpus_documents(), tokenizer):
        passages_by_doc.setdefault(item.doc_id, []).append(item.text)
    log("passage map: %d docs" % len(passages_by_doc))

    model = CrossEncoder(
        str(NEW_RERANKER), device="cuda:0", max_length=MAX_LENGTH, trust_remote_code=False
    )
    log("reranker: %s max_length=%s" % (type(model.model).__name__, model.max_length))

    report = {
        "schema": "servicemind-layer2-reranker-ab-v1",
        "note": "offline layer-2 experiment; not a repo release report",
        "new_reranker": {
            "model": "Alibaba-NLP/gte-reranker-modernbert-base",
            "max_length": MAX_LENGTH,
            "remote_code": False,
        },
        "production_reranker": {
            "model": "BAAI/bge-reranker-v2-m3",
            "note": "scores reused from the layer-1 TEI run",
        },
        "pools": {},
    }
    pooled = {}

    for name in POOLS:
        doc_lists, baseline_matrix = load_pool(name)
        log("%s: %d queries x %d docs" % (name, len(doc_lists), len(doc_lists[0])))
        new_matrix = rerank_pool(name, funnel, doc_lists, model, passages_by_doc)
        production_outcomes = outcomes_for(doc_lists, baseline_matrix, relevant, answerable)
        new_outcomes = outcomes_for(doc_lists, new_matrix, relevant, answerable)
        pooled[name] = {"production": production_outcomes, "new": new_outcomes}
        report["pools"][name] = {
            "depth": len(doc_lists[0]),
            "production_bge_reranker_v2_m3": scores_for(production_outcomes),
            "gte_reranker_modernbert_base": scores_for(new_outcomes),
            "paired_bootstrap_new_vs_production": {
                "ndcg_at_10": paired(production_outcomes, new_outcomes),
                "recall_at_10": paired(production_outcomes, new_outcomes, "recall", 10),
            },
        }

    # Re-ask the section 4.1 adoption question under a reranker that is not the
    # bottleneck: does the third arm's fusion gain survive into the top 10?
    report["third_arm_decision"] = {}
    for reranker in ("production", "new"):
        left = pooled["two_way"][reranker]
        right = pooled["three_way"][reranker]
        hard = [
            position
            for position, outcome in enumerate(left)
            if P._metric_values([outcome], "rank", "ndcg", 10)[0] < 1.0
        ]
        report["third_arm_decision"][reranker] = {
            "NDCG@10 overall": paired(left, right),
            "Recall@10 overall": paired(left, right, "recall", 10),
            "NDCG@10 hard subset": paired([left[i] for i in hard], [right[i] for i in hard]),
        }

    log(json.dumps(report["pools"], indent=2))
    log(json.dumps(report["third_arm_decision"], indent=2))
    (OUT / "layer2b_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
