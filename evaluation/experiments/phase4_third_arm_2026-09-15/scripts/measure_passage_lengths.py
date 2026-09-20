"""How long is a reranker input, really -- characters, passage tokens, or pair tokens?

Written to settle a factual dispute about §2.6 of the root-cause document. The first
draft stated "child passages run p50=1313 BGE tokens, so a 512-token window discards
~61% of every passage". Three separate quantities were being conflated:

  1. the CHARACTER length of a passage string,
  2. the TOKEN length of that passage under the reranker's own tokenizer,
  3. the TOKEN length of the (query, passage) PAIR the cross-encoder actually consumes.

`CrossEncoder(max_length=N)` sets `tokenizer.model_max_length = N`, and the tokenizer
truncates the concatenated pair -- not the passage in isolation. Only (3) decides
whether a 512 window bites at all.

No model inference, no GPU: tokenizers only.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path("/home/shihongye/data1/servicemind")
OUT = Path("/tmp/exp_layer1")
PAIR_SAMPLE = 3000
SEED = 0

spec = importlib.util.spec_from_file_location(
    "proxy_eval", ROOT / "scripts/evaluate_phase4_proxy_release.py"
)
P = importlib.util.module_from_spec(spec)
sys.modules["proxy_eval"] = P
spec.loader.exec_module(P)

from transformers import AutoTokenizer


def log(message):
    print("[lengths] %s" % message, flush=True)


def describe(values: np.ndarray) -> dict:
    return {
        "n": int(len(values)),
        "p50": int(np.percentile(values, 50)),
        "p95": int(np.percentile(values, 95)),
        "max": int(values.max()),
        "mean": round(float(values.mean()), 1),
    }


embedding_snap = sorted(
    (ROOT / "data/phase4/models/embedding").glob("models--BAAI--bge-m3/snapshots/*")
)[0]
reranker_snap = sorted(
    (ROOT / "data/phase4/models/reranker").glob("models--BAAI--bge-reranker-v2-m3/snapshots/*")
)[0]
embedding_tok = AutoTokenizer.from_pretrained(
    embedding_snap / "onnx", local_files_only=True, trust_remote_code=False
)
reranker_tok = AutoTokenizer.from_pretrained(
    reranker_snap, local_files_only=True, trust_remote_code=False
)
log("reranker tokenizer.model_max_length = %s" % reranker_tok.model_max_length)

passages = P._passages(P._corpus_documents(), embedding_tok)
texts = [item.text for item in passages]
log("passages: %d" % len(texts))


def token_lengths(tokenizer, strings, batch=512):
    lengths = []
    for start in range(0, len(strings), batch):
        encoded = tokenizer(strings[start : start + batch], add_special_tokens=False)["input_ids"]
        lengths.extend(len(ids) for ids in encoded)
    return np.array(lengths)


chars = np.array([len(text) for text in texts])
passage_tokens_embedding = token_lengths(embedding_tok, texts)
passage_tokens_reranker = token_lengths(reranker_tok, texts)

funnel = json.loads((OUT / "funnel_v1.json").read_text())
questions = funnel["questions"]
query_tokens = token_lengths(reranker_tok, questions)

rng = np.random.default_rng(SEED)
sample = [texts[i] for i in rng.choice(len(texts), size=min(PAIR_SAMPLE, len(texts)), replace=False)]
sample_queries = [questions[i % len(questions)] for i in range(len(sample))]
pair_tokens = np.array(
    [
        len(ids)
        for ids in reranker_tok(
            list(zip(sample_queries, sample, strict=True)), add_special_tokens=True
        )["input_ids"]
    ]
)

report = {
    "characters": describe(chars),
    "passage_tokens_bge_tokenizer": describe(passage_tokens_embedding),
    "passage_tokens_reranker_tokenizer": describe(passage_tokens_reranker),
    "query_tokens_reranker_tokenizer": describe(query_tokens),
    "pair_tokens_reranker_tokenizer": describe(pair_tokens),
    "passages_over_512_tokens": {
        "count": int((passage_tokens_reranker > 512).sum()),
        "of": int(len(passage_tokens_reranker)),
    },
    "pairs_over_512_tokens": {
        "count": int((pair_tokens > 512).sum()),
        "of": int(len(pair_tokens)),
        "fraction": round(float((pair_tokens > 512).mean()), 4),
    },
    "reranker_tokenizer_model_max_length": int(reranker_tok.model_max_length),
    "pair_sample_size": int(len(pair_tokens)),
    "seed": SEED,
}
log(json.dumps(report, indent=2))
(OUT / "passage_lengths.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
log("wrote %s" % (OUT / "passage_lengths.json"))
