"""Layer-1 experiment: does the production-shaped funnel (depth 100 + rerank + blend)
clear more of the Recall@10 gate than the published proxy (RRF top-30 + pure rerank)?

This does NOT modify the repo. It reuses the frozen proxy evaluation's own helpers,
caches and metric code so every number is directly comparable with evaluation/reports/.

Phases:
  A  corpus -> passages (cache identity re-verified) -> dense top100 -> bm25 top100 -> RRF
  B  rerank the RRF top100 per query with the *production* TEI reranker, max-pool per doc
  C  score arms: rrf100 / pure30 (reproduces the published number) / pure100 /
     blend100 (production 0.85 * rerank + 0.15 * minmax(rrf)) / oracle (pool ceiling)
"""

from __future__ import annotations

import argparse
import gc
import importlib.util
import json
import os
import statistics
import sys
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np

ROOT = Path("/home/shihongye/data1/servicemind")
OUT = Path("/tmp/exp_layer1")
OUT.mkdir(exist_ok=True)

spec = importlib.util.spec_from_file_location(
    "proxy_eval", ROOT / "scripts" / "evaluate_phase4_proxy_release.py"
)
assert spec and spec.loader
P = importlib.util.module_from_spec(spec)
sys.modules["proxy_eval"] = P  # dataclasses need cls.__module__ resolvable
spec.loader.exec_module(P)

RERANK_URL = os.getenv("EXP_RERANK_URL", "http://127.0.0.1:8086")
TEI_BATCH = 16
TEI_CONCURRENCY = 4
POOL_DEPTH = 100
ARMS = ("rrf100", "pure30", "pure100", "blend100", "oracle")
METRICS = {
    "recall_at_5": ("recall", 5),
    "recall_at_10": ("recall", 10),
    "recall_at_20": ("recall", 20),
    "mrr_at_10": ("mrr", 10),
    "ndcg_at_10": ("ndcg", 10),
}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def score_arm(outcomes, arm: str) -> dict:
    out = {}
    for name, (metric, k) in METRICS.items():
        values = P._metric_values(outcomes, arm, metric, k)
        out[name] = round(statistics.fmean(values), 4)
        out[f"{name}_ci95"] = P._ci(values)
    return out


def tei_rerank(client: httpx.Client, query: str, texts: list[str]) -> list[float]:
    scores = [0.0] * len(texts)
    chunks = [(start, texts[start : start + TEI_BATCH]) for start in range(0, len(texts), TEI_BATCH)]
    for attempt in range(4):
        try:

            def call(item):
                start, batch = item
                response = client.post(
                    f"{RERANK_URL}/rerank",
                    json={"query": query, "texts": batch, "return_text": False},
                )
                response.raise_for_status()
                return start, response.json()

            with ThreadPoolExecutor(max_workers=TEI_CONCURRENCY) as pool:
                for start, batch in pool.map(call, chunks):
                    for item in batch:
                        scores[start + int(item["index"])] = float(item["score"])
            return scores
        except Exception as exc:  # noqa: BLE001 - transient TEI/GPU pressure
            if attempt == 3:
                raise
            log(f"rerank retry {attempt + 1}: {type(exc).__name__}: {exc}")
            time.sleep(2.0 * (attempt + 1))
    raise AssertionError("unreachable")


def phase_a(limit: int) -> dict:
    log("loading corpus")
    documents = P._corpus_documents()
    document_ids = [doc_id for doc_id, _ in documents]
    document_index = {doc_id: i for i, doc_id in enumerate(document_ids)}
    log(f"corpus docs: {len(document_ids)}")

    tokenizer, session, revision = P._embedding_session()
    log(f"embedding revision: {revision}")
    passages = P._passages(documents, tokenizer)
    log(f"passages: {len(passages)}")
    vectors = P._passage_embeddings(passages, tokenizer, session, revision)
    log(f"passage vectors: {vectors.shape} (identity hash re-verified by helper)")

    rows = json.loads((P.DATA / "train.json").read_text(encoding="utf-8"))
    answerable, impossible = P._select(rows)
    selected = [*answerable, *impossible][: limit or None]
    log(f"queries: {len(selected)}")

    query_vectors = P._encode(
        [row["question"] for row in selected], tokenizer, session, label="exp dense queries"
    )
    passage_document_indices = np.asarray(
        [document_index[item.doc_id] for item in passages], dtype=np.int32
    )
    dense_rankings = P._top_dense(
        query_vectors, vectors, document_ids, passage_document_indices, k=POOL_DEPTH
    )
    log("dense top100 done")
    del session, vectors, query_vectors
    gc.collect()

    url, auth = P._config()
    index = f"sm-exp-layer1-{uuid.uuid4().hex[:8]}"
    bm25_rankings: list[list[str]] = []
    search_ms: list[float] = []
    with httpx.Client(base_url=url, auth=auth, verify=False, timeout=120, trust_env=False) as client:
        P._request(
            client,
            "PUT",
            f"/{index}",
            json_body={
                "settings": {
                    "number_of_shards": 1,
                    "number_of_replicas": 0,
                    "refresh_interval": "-1",
                },
                "mappings": {
                    "dynamic": "strict",
                    "properties": {
                        "passage_id": {"type": "keyword"},
                        "doc_id": {"type": "keyword"},
                        "title": {"type": "text"},
                        "text": {"type": "text"},
                    },
                },
            },
        )
        try:
            P._bulk_index(client, index, passages)
            P._request(client, "POST", f"/{index}/_refresh")
            log("bm25 index built")
            for row in selected:
                tick = time.perf_counter()
                response = P._request(
                    client,
                    "POST",
                    f"/{index}/_search",
                    json_body={
                        "size": 500,
                        "_source": ["doc_id"],
                        "query": {
                            "multi_match": {
                                "query": row["question"],
                                "fields": ["title^2", "text"],
                                "type": "best_fields",
                            }
                        },
                    },
                )
                search_ms.append((time.perf_counter() - tick) * 1000)
                bm25_rankings.append(
                    P._distinct_documents(response["hits"]["hits"], limit=POOL_DEPTH)
                )
        finally:
            client.delete(f"/{index}")
    log("bm25 top100 done")

    rrf_rankings = [
        P._rrf(bm25, dense, k=POOL_DEPTH)
        for bm25, dense in zip(bm25_rankings, dense_rankings, strict=True)
    ]
    payload = {
        "queries": [row["id"] for row in selected],
        "questions": [row["question"] for row in selected],
        "relevant": [
            sorted({c["filename"] for c in row.get("contexts", [])}) for row in selected
        ],
        "is_impossible": [bool(row.get("is_impossible")) for row in selected],
        "dense": dense_rankings,
        "bm25": bm25_rankings,
        "rrf": rrf_rankings,
        "search_ms": search_ms,
    }
    (OUT / "funnel_v1.json").write_text(json.dumps(payload), encoding="utf-8")
    log("phase A cached -> funnel_v1.json")
    return payload


def passages_by_doc() -> dict[str, list[str]]:
    from transformers import AutoTokenizer

    snap = sorted(
        (ROOT / "data" / "phase4" / "models" / "embedding").glob(
            "models--BAAI--bge-m3/snapshots/*"
        )
    )[0]
    tokenizer = AutoTokenizer.from_pretrained(
        snap / "onnx", local_files_only=True, trust_remote_code=False
    )
    mapping: dict[str, list[str]] = {}
    for item in P._passages(P._corpus_documents(), tokenizer):
        mapping.setdefault(item.doc_id, []).append(item.text)
    gc.collect()
    return mapping


def phase_b(payload: dict, limit: int) -> np.ndarray:
    scores_file = OUT / "rerank_tei_v1.npy"
    docs_file = OUT / "rerank_tei_v1.json"
    doc_lists = payload["rrf"]
    if scores_file.exists() and docs_file.exists():
        if json.loads(docs_file.read_text())["docs"] == doc_lists:
            log("rerank cache verified")
            return np.load(scores_file)

    mapping = passages_by_doc()
    sizes = Counter(len(mapping[d]) for d in doc_lists[0])
    log(f"passages per candidate doc (first query): {dict(sizes)}")
    matrix = np.zeros((len(doc_lists), POOL_DEPTH), dtype=np.float32)
    n = limit or len(doc_lists)
    tick = time.perf_counter()
    with httpx.Client(timeout=600, trust_env=False) as client:
        for i in range(n):
            texts: list[str] = []
            spans: list[tuple[int, int]] = []
            for doc_id in doc_lists[i]:
                children = mapping[doc_id]
                spans.append((len(texts), len(texts) + len(children)))
                texts.extend(children)
            flat = tei_rerank(client, payload["questions"][i], texts)
            for pos, (start, stop) in enumerate(spans):
                matrix[i, pos] = max(flat[start:stop]) if stop > start else 0.0
            if i % 5 == 0 or i == n - 1:
                done = i + 1
                rate = done / (time.perf_counter() - tick)
                log(f"reranked {done}/{n} q ({rate:.2f} q/s, eta {(n - done) / rate / 60:.1f} min)")
                np.save(scores_file, matrix)
    np.save(scores_file, matrix)
    docs_file.write_text(json.dumps({"docs": doc_lists}) + "\n", encoding="utf-8")
    log("phase B cached -> rerank_tei_v1.npy")
    return matrix


def phase_c(payload: dict, matrix: np.ndarray) -> dict:
    doc_lists = payload["rrf"]
    relevant = [set(x) for x in payload["relevant"]]
    alpha = P._rerank_weight()
    rrf_scores = [
        P._rrf_scores(dense, bm25)
        for dense, bm25 in zip(payload["dense"], payload["bm25"], strict=True)
    ]
    outcomes = []
    for i, docs in enumerate(doc_lists):
        row = matrix[i]
        rel = relevant[i]
        ranked = list(range(len(docs)))

        def order(indices, key):
            return tuple(docs[j] for j in sorted(indices, key=key))

        pure30 = order(range(30), lambda j: (-float(row[j]), docs[j]))
        pure100 = order(ranked, lambda j: (-float(row[j]), docs[j]))
        # `final_score` normalises the *retrieval* (fused RRF) score, not the rerank
        # score. Normalising the rerank score here would make the blend a monotone
        # transform of it, so every ranking would come out identical to pure rerank.
        by_doc = {doc: float(row[j]) for j, doc in enumerate(docs)}
        blend100 = P._blend_ranking(
            docs, by_doc, rrf_scores[i], rerank_weight=alpha, depth=POOL_DEPTH
        )
        oracle = tuple(sorted(docs, key=lambda d: (d not in rel, d)))
        outcomes.append(
            SimpleNamespace(
                relevant=frozenset(rel),
                rrf100=tuple(docs),
                pure30=pure30,
                pure100=pure100,
                blend100=blend100,
                oracle=oracle,
            )
        )

    report = {
        "schema": "servicemind-layer1-funnel-experiment-v1",
        "note": "offline layer-1 experiment; not a repo release report",
        "queries": len(doc_lists),
        "pool_depth": POOL_DEPTH,
        "reranker": "TEI bge-reranker-v2-m3 @127.0.0.1:8086 (production provider, single sigmoid)",
        "blend": f"{alpha} * rerank + {1 - alpha} * minmax(rrf)",
        "bm25_search_ms": {
            "p50": round(float(np.percentile(payload["search_ms"], 50)), 2),
            "p95": round(float(np.percentile(payload["search_ms"], 95)), 2),
        },
        # Retrieval metrics are only defined for answerable queries (the original
        # harness scores `_score(answerable_outcomes, ...)`); impossible queries carry
        # an empty relevant set and would divide by zero.
        "arms": {
            arm: score_arm([o for o in outcomes if o.relevant], arm) for arm in ARMS
        },
        "answerable": sum(1 for o in outcomes if o.relevant),
    }
    (OUT / "layer1_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["a", "b", "c", "all"], default="all")
    parser.add_argument("--limit", type=int, default=0, help="debug: only first N queries")
    args = parser.parse_args()

    funnel = OUT / "funnel_v1.json"
    if args.phase in {"a"} or (args.phase == "all" and not funnel.exists()):
        payload = phase_a(args.limit)
    else:
        payload = json.loads(funnel.read_text())
    log(f"funnel: {len(payload['queries'])} queries, depth {POOL_DEPTH}, "
        f"median candidate docs {statistics.median(len(x) for x in payload['rrf'])}")

    if args.phase == "a":
        return
    matrix = phase_b(payload, args.limit)
    log(f"rerank scores: {matrix.shape} min={matrix.min():.4f} max={matrix.max():.4f}")
    if args.phase == "b":
        return
    report = phase_c(payload, matrix)
    log(json.dumps(report["arms"], indent=2))


if __name__ == "__main__":
    main()
