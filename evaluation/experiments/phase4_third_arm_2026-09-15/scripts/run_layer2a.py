"""Layer-2 experiment a: add a modern dense arm (Qwen3-Embedding-0.6B) to the funnel.

Question: the two-arm pool (BM25 + BGE-M3 dense) tops out at 0.8714 recall@10 coverage,
which is already below the 0.90 gate. Does a different, newer embedding family recover a
different subset of queries, i.e. raise the ceiling?

Nothing in the repo is modified; the existing funnel cache keeps the BM25 and BGE-M3 arms
byte-identical, so the comparison isolates the new arm.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import numpy as np

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

ROOT = Path("/home/shihongye/data1/servicemind")
OUT = Path("/tmp/exp_layer1")
MODEL = (
    ROOT
    / "data/phase4/models/layer2/models--Qwen--Qwen3-Embedding-0.6B/snapshots"
    / "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
)
TASK = (
    "Given a technical support question about IBM software products, retrieve the product "
    "documentation passage that answers it"
)
POOL_DEPTH = 100

import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "proxy_eval", ROOT / "scripts" / "evaluate_phase4_proxy_release.py"
)
P = importlib.util.module_from_spec(spec)
sys.modules["proxy_eval"] = P
spec.loader.exec_module(P)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> None:
    import torch
    from sentence_transformers import SentenceTransformer
    from transformers import AutoTokenizer

    funnel = json.loads((OUT / "funnel_v1.json").read_text())
    documents = P._corpus_documents()
    document_ids = [doc_id for doc_id, _ in documents]
    document_index = {doc_id: i for i, doc_id in enumerate(document_ids)}

    snap = sorted(
        (ROOT / "data/phase4/models/embedding").glob("models--BAAI--bge-m3/snapshots/*")
    )[0]
    tokenizer = AutoTokenizer.from_pretrained(
        snap / "onnx", local_files_only=True, trust_remote_code=False
    )
    passages = P._passages(documents, tokenizer)
    log(f"passages: {len(passages)}")

    model = SentenceTransformer(str(MODEL), device="cuda:0", trust_remote_code=False)
    model.max_seq_length = 512
    model.eval()
    log(f"model loaded: {MODEL.name} dim={model.get_sentence_embedding_dimension()}")

    cache = OUT / "qwen3_passages.npy"
    if cache.exists():
        vectors = np.load(cache, mmap_mode="r")
        log(f"passage vectors cached: {vectors.shape}")
    else:
        texts = [item.text for item in passages]
        chunks = []
        with torch.inference_mode():
            for start in range(0, len(texts), 2048):
                batch = texts[start : start + 2048]
                emb = model.encode(
                    batch,
                    batch_size=64,
                    normalize_embeddings=True,
                    convert_to_numpy=True,
                    show_progress_bar=False,
                )
                chunks.append(emb.astype(np.float32))
                if start % 16384 == 0:
                    log(f"encoded {start + len(batch)}/{len(texts)}")
        vectors = np.vstack(chunks)
        np.save(cache, vectors)
        log(f"passage vectors -> {cache} {vectors.shape}")

    queries = [f"Instruct: {TASK}\nQuery: {q}" for q in funnel["questions"]]
    with torch.inference_mode():
        query_vectors = model.encode(
            queries,
            batch_size=32,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
            prompt_name=None,
        ).astype(np.float32)
    log(f"query vectors: {query_vectors.shape}")

    passage_document_indices = np.asarray(
        [document_index[item.doc_id] for item in passages], dtype=np.int32
    )
    dense_rankings = P._top_dense(
        query_vectors, vectors, document_ids, passage_document_indices, k=POOL_DEPTH
    )
    log("qwen3 dense top100 done")

    dense_file = OUT / "qwen3_dense_top100.json"
    dense_file.write_text(json.dumps(dense_rankings), encoding="utf-8")

    bge = funnel["dense"]
    bm25 = funnel["bm25"]
    two_way = funnel["rrf"]
    three_way = [
        P._rrf(P._rrf(b, d1, k=200), d2, k=POOL_DEPTH)
        for b, d1, d2 in zip(bm25, bge, dense_rankings, strict=True)
    ]
    (OUT / "rrf3_top100.json").write_text(json.dumps(three_way), encoding="utf-8")

    rel = [set(x) for x in funnel["relevant"]]
    ans = [i for i, r in enumerate(rel) if r]

    def coverage(rankings):
        return sum(1 for i in ans if rel[i] & set(rankings[i])) / len(ans)

    def recall_at(rankings, k):
        return sum(1 for i in ans if rel[i] & set(rankings[i][:k])) / len(ans)

    summary = {
        "queries_answerable": len(ans),
        "pool_coverage_bm25_dense_bge": round(coverage(two_way), 4),
        "pool_coverage_bm25_dense_bge_qwen3": round(coverage(three_way), 4),
        "recall_at_10_rrf_2way": round(recall_at(two_way, 10), 4),
        "recall_at_10_rrf_3way": round(recall_at(three_way, 10), 4),
        "qwen3_arm_alone_coverage": round(coverage(dense_rankings), 4),
        "qwen3_arm_alone_recall_at_10": round(recall_at(dense_rankings, 10), 4),
    }
    recovered = sum(
        1
        for i in ans
        if rel[i] and not (rel[i] & set(two_way[i])) and (rel[i] & set(three_way[i]))
    )
    lost = sum(
        1
        for i in ans
        if rel[i] and (rel[i] & set(two_way[i])) and not (rel[i] & set(three_way[i]))
    )
    summary["queries_recovered_by_third_arm"] = recovered
    summary["queries_lost_by_three_way_rrf"] = lost
    log(json.dumps(summary, indent=2))
    (OUT / "layer2a_report.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
