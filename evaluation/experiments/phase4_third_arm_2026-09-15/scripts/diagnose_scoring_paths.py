"""How far apart are the TEI and in-process reranker scoring paths?

Quantifies whether the 0/400 ordering mismatch is near-tie flipping (Spearman ~ 1.0,
top-k sets preserved) or a genuine score disagreement (lower correlation, top-k sets
churn). The layer-2 reranker A/B rests on the TEI matrix as its production baseline, so
the size of this gap decides how that baseline may be described.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

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

from transformers import AutoTokenizer

snapshot = sorted((ROOT / "data/phase4/models/embedding").glob("models--BAAI--bge-m3/snapshots/*"))[0]
tokenizer = AutoTokenizer.from_pretrained(snapshot / "onnx", local_files_only=True, trust_remote_code=False)
mapping: dict[str, list[str]] = {}
for item in P._passages(P._corpus_documents(), tokenizer):
    mapping.setdefault(item.doc_id, []).append(item.text)
log("passage map: %d docs" % len(mapping))

# Passage text lengths seen by each path: TEI truncates server-side, the harness truncates
# via the tokenizer at max_length=512.
lengths = np.array([len(seg) for segments in mapping.values() for seg in segments])
log("child passages: %d, token length p50=%d p95=%d max=%d"
    % (len(lengths), np.percentile(lengths, 50), np.percentile(lengths, 95), lengths.max()))

flat = np.load(DATA / ("techqa_bge_reranker_%s_passages420_pool100.npy" % REVISION))
harness = np.zeros((len(doc_lists), len(doc_lists[0])), dtype=np.float32)
offset = 0
for i, docs in enumerate(doc_lists):
    for slot, doc_id in enumerate(docs):
        children = mapping[doc_id]
        span = flat[offset:offset + len(children)]
        harness[i, slot] = float(np.max(span)) if len(children) else 0.0
        offset += len(children)
assert offset == flat.shape[0]

tei = np.load(OUT / "rerank_tei_v1.npy")

def order(row):
    return list(np.argsort(-row, kind="stable"))

spearman, top10_overlap, top1_same = [], [], 0
for i in range(len(doc_lists)):
    a, b = order(harness[i]), order(tei[i])
    ra = np.empty(len(a)); ra[a] = np.arange(len(a))
    rb = np.empty(len(b)); rb[b] = np.arange(len(b))
    spearman.append(np.corrcoef(ra, rb)[0, 1])
    top10_overlap.append(len(set(a[:10]) & set(b[:10])) / 10)
    top1_same += int(a[0] == b[0])

spearman = np.array(spearman)
log("per-query Spearman(harness, TEI) over the 100-doc pool: mean=%.4f min=%.4f p05=%.4f"
    % (spearman.mean(), spearman.min(), np.percentile(spearman, 5)))
log("top-10 set overlap: mean=%.4f  (exact-10 overlap on %d/400 queries)"
    % (np.mean(top10_overlap), sum(1 for x in top10_overlap if x == 1.0)))
log("rank-1 agreement: %d/400" % top1_same)
log("scale check: harness p50=%.3f range=[%.3f, %.3f] | TEI p50=%.3f range=[%.3f, %.3f]"
    % (np.median(harness), harness.min(), harness.max(), np.median(tei), tei.min(), tei.max()))

(OUT / "scoring_path_diagnosis.json").write_text(json.dumps({
    "spearman_mean": round(float(spearman.mean()), 4),
    "spearman_min": round(float(spearman.min()), 4),
    "spearman_p05": round(float(np.percentile(spearman, 5)), 4),
    "top10_overlap_mean": round(float(np.mean(top10_overlap)), 4),
    "top10_exact_overlap_queries": int(sum(1 for x in top10_overlap if x == 1.0)),
    "rank1_agreement_queries": int(top1_same),
    "harness_scale": {"p50": round(float(np.median(harness)), 3), "min": round(float(harness.min()), 3),
                      "max": round(float(harness.max()), 3)},
    "tei_scale": {"p50": round(float(np.median(tei)), 3), "min": round(float(tei.min()), 3),
                  "max": round(float(tei.max()), 3)},
    "child_passage_tokens": {"p50": int(np.percentile(lengths, 50)), "p95": int(np.percentile(lengths, 95)),
                             "max": int(lengths.max())},
}, indent=2) + "\n", encoding="utf-8")
