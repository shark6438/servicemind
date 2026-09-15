"""Run the reproducible Phase 4 proxy release gate without private query logs.

The benchmark uses the pinned NVIDIA TechQA labels as external silver truth. It
indexes production-shaped child passages from the full 28,481-file corpus, selects a
deterministic 400-query release set (280 answerable + 120 officially impossible),
measures exact max-pooled BGE-M3 dense retrieval and RRF hybrid retrieval, then
reranks the hybrid top 30 by each document's best child passage. Impossible queries
participate in a held-out abstention test. It does not describe these public labels
as tenant-domain human gold.

The GPU command used for the release report is intentionally explicit because the
project venv's current torch build requires a newer driver than this host, while the
host Python uses its compatible CUDA 12.1 torch build:

    LIBS=$(find /data/shihongye/data/miniconda3/lib/python3.13/site-packages/nvidia \
      -type d -name lib | paste -sd:)
    LD_LIBRARY_PATH="$LIBS:${LD_LIBRARY_PATH:-}" \
      python scripts/evaluate_phase4_proxy_release.py --device cuda:0
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import os
import statistics
import time
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "phase4" / "raw" / "eval" / "techqa-rag-eval"
REPORTS = ROOT / "evaluation" / "reports"
SELECTION = ROOT / "evaluation" / "gold" / "phase4_proxy_release_v1.2.json"
TECHQA_REVISION = "0b5bbc84b7f07d6d09d063130e90b716d8d4a32a"
SELECTION_SEED = "servicemind-phase4-proxy-v1.2"


@dataclass(frozen=True)
class Outcome:
    query_id: str
    relevant: frozenset[str]
    bm25: tuple[str, ...]
    dense: tuple[str, ...]
    hybrid: tuple[str, ...]
    hybrid_reranked: tuple[str, ...]
    search_ms: float
    top_score: float


@dataclass(frozen=True)
class Passage:
    passage_id: str
    doc_id: str
    title: str
    text: str


def _config() -> tuple[str, tuple[str, str]]:
    file_values = dotenv_values(ROOT / ".env")

    def value(name: str, default: str = "") -> str:
        return os.getenv(name) or str(file_values.get(name) or default)

    url = value("SERVICEMIND_OPENSEARCH_URL", "https://127.0.0.1:9200").rstrip("/")
    username = value("SERVICEMIND_OPENSEARCH_USERNAME", "admin")
    password = value("SERVICEMIND_OPENSEARCH_PASSWORD") or value("POSTGRES_PASSWORD")
    if not password:
        raise RuntimeError("OpenSearch password is not configured")
    return url, (username, password)


def _request(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    json_body: Any | None = None,
    content: bytes | None = None,
) -> Any:
    response = client.request(method, path, json=json_body, content=content)
    response.raise_for_status()
    return response.json() if response.content else None


def _corpus_documents() -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    with zipfile.ZipFile(DATA / "corpus.zip") as archive:
        for item in archive.infolist():
            if item.is_dir():
                continue
            doc_id = Path(item.filename).name
            text = archive.read(item).decode("utf-8", "replace").strip()
            result.append((doc_id, text))
    return result


def _bulk_index(client: httpx.Client, index: str, passages: list[Passage]) -> None:
    for start in range(0, len(passages), 250):
        lines: list[str] = []
        for passage in passages[start : start + 250]:
            lines.append(json.dumps({"index": {"_index": index, "_id": passage.passage_id}}))
            lines.append(
                json.dumps(
                    {
                        "passage_id": passage.passage_id,
                        "doc_id": passage.doc_id,
                        "title": passage.title,
                        "text": passage.text,
                    },
                    ensure_ascii=False,
                )
            )
        payload = ("\n".join(lines) + "\n").encode("utf-8")
        response = client.post(
            "/_bulk",
            content=payload,
            headers={"content-type": "application/x-ndjson"},
            timeout=120,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("errors"):
            failures = [item for item in body["items"] if item["index"].get("error")]
            raise RuntimeError(f"TechQA bulk indexing failed: {failures[:3]}")


def _rank_key(row: dict[str, Any]) -> str:
    return hashlib.sha256(f"{SELECTION_SEED}|{row['id']}".encode()).hexdigest()


def _select(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    answerable = sorted((row for row in rows if not row["is_impossible"]), key=_rank_key)[:280]
    impossible = sorted((row for row in rows if row["is_impossible"]), key=_rank_key)[:120]
    return answerable, impossible


def _selection_payload(
    answerable: list[dict[str, Any]], impossible: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "schema_version": "phase4-proxy-release-v1.2",
        "source": "nvidia/TechQA-RAG-Eval",
        "source_revision": TECHQA_REVISION,
        "selection_seed": SELECTION_SEED,
        "label_tier": "external_silver_no_tenant_human_signoff",
        "answerable": [
            {
                "id": row["id"],
                "relevant_filenames": sorted(
                    {context["filename"] for context in row.get("contexts", [])}
                ),
            }
            for row in answerable
        ],
        "impossible": [row["id"] for row in impossible],
    }


def _metric_values(outcomes: list[Outcome], ranking: str, metric: str, k: int) -> list[float]:
    values = []
    for outcome in outcomes:
        ranked = getattr(outcome, ranking)[:k]
        gains = [1 if item in outcome.relevant else 0 for item in ranked]
        if metric == "recall":
            values.append(sum(gains) / len(outcome.relevant))
        elif metric == "mrr":
            values.append(next((1.0 / rank for rank, gain in enumerate(gains, 1) if gain), 0.0))
        elif metric == "ndcg":
            dcg = sum(gain / math.log2(rank + 1) for rank, gain in enumerate(gains, 1))
            ideal = sum(
                1.0 / math.log2(rank + 1) for rank in range(1, min(k, len(outcome.relevant)) + 1)
            )
            values.append(dcg / ideal if ideal else 0.0)
        else:
            raise ValueError(metric)
    return values


def _ci(values: list[float], *, seed: int = 42, samples: int = 10_000) -> list[float]:
    if not values:
        return [0.0, 0.0]
    generator = np.random.default_rng(seed)
    array = np.asarray(values, dtype=np.float64)
    means = np.empty(samples, dtype=np.float64)
    for start in range(0, samples, 500):
        size = min(500, samples - start)
        indices = generator.integers(0, len(array), size=(size, len(array)))
        means[start : start + size] = array[indices].mean(axis=1)
    low, high = np.quantile(means, [0.025, 0.975])
    return [round(float(low), 4), round(float(high), 4)]


def _score(outcomes: list[Outcome], ranking: str) -> dict[str, Any]:
    definitions = {
        "recall_at_5": ("recall", 5),
        "recall_at_10": ("recall", 10),
        "recall_at_20": ("recall", 20),
        "mrr_at_10": ("mrr", 10),
        "ndcg_at_10": ("ndcg", 10),
    }
    result: dict[str, Any] = {}
    for name, (metric, k) in definitions.items():
        values = _metric_values(outcomes, ranking, metric, k)
        result[name] = round(statistics.fmean(values), 4)
        result[f"{name}_ci95"] = _ci(values)
    return result


def _reranker(
    device: str, batch_size: int
) -> tuple[Callable[[list[tuple[str, str]]], np.ndarray], str, str]:
    from sentence_transformers import CrossEncoder

    snapshots = sorted(
        (ROOT / "data" / "phase4" / "models" / "reranker").glob(
            "models--BAAI--bge-reranker-v2-m3/snapshots/*"
        )
    )
    if len(snapshots) != 1:
        raise RuntimeError(f"expected one pinned reranker snapshot, found {snapshots}")
    model: Any = CrossEncoder(
        str(snapshots[0]), device=device, max_length=512, trust_remote_code=False
    )

    def predict(pairs: list[tuple[str, str]]) -> np.ndarray:
        return np.asarray(
            model.predict(pairs, batch_size=batch_size, show_progress_bar=False)
        ).reshape(-1)

    return predict, "BAAI/bge-reranker-v2-m3", snapshots[0].name


def _reranker_scores(
    pairs: list[tuple[str, str]],
    predict: Callable[[list[tuple[str, str]]], np.ndarray],
    revision: str,
) -> np.ndarray:
    digest = hashlib.sha256()
    for query, passage in pairs:
        digest.update(query.encode())
        digest.update(b"\0")
        digest.update(passage.encode())
        digest.update(b"\n")
    expected = {
        "dataset_revision": TECHQA_REVISION,
        "model_revision": revision,
        "max_length": 512,
        "pairs": len(pairs),
        "ordered_pairs_sha256": digest.hexdigest(),
    }
    cache = DATA / f"techqa_bge_reranker_{revision}_passages420.npy"
    identity = DATA / f"techqa_bge_reranker_{revision}_passages420.json"
    partial = DATA / f"techqa_bge_reranker_{revision}_passages420.partial.npy"
    progress_file = DATA / f"techqa_bge_reranker_{revision}_passages420.progress.json"
    if cache.exists() and identity.exists():
        values = np.load(cache, mmap_mode="r")
        if json.loads(identity.read_text(encoding="utf-8")) == expected and values.shape == (
            len(pairs),
        ):
            print(f"reranker score cache verified: {cache}")
            return values

    completed = 0
    if partial.exists() and progress_file.exists():
        state = json.loads(progress_file.read_text(encoding="utf-8"))
        values = np.load(partial, mmap_mode="r+")
        if state.get("identity") == expected and values.shape == (len(pairs),):
            completed = int(state.get("completed", 0))
            print(f"resuming reranker score cache at {completed}/{len(pairs)}")
        else:
            partial.unlink(missing_ok=True)
            progress_file.unlink(missing_ok=True)
            values = np.lib.format.open_memmap(
                partial, mode="w+", dtype=np.float32, shape=(len(pairs),)
            )
    else:
        values = np.lib.format.open_memmap(
            partial, mode="w+", dtype=np.float32, shape=(len(pairs),)
        )
    chunk_size = 512
    for start in range(completed, len(pairs), chunk_size):
        stop = min(start + chunk_size, len(pairs))
        values[start:stop] = predict(pairs[start:stop])
        values.flush()
        progress_file.write_text(
            json.dumps({"identity": expected, "completed": stop}) + "\n",
            encoding="utf-8",
        )
        print(f"reranker pairs: {stop}/{len(pairs)}")
    values.flush()
    partial.replace(cache)
    identity.write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
    progress_file.unlink(missing_ok=True)
    return np.load(cache, mmap_mode="r")


def _embedding_session() -> tuple[Any, Any, str]:
    import onnxruntime as ort
    from transformers import AutoTokenizer

    snapshots = sorted(
        (ROOT / "data" / "phase4" / "models" / "embedding").glob("models--BAAI--bge-m3/snapshots/*")
    )
    if len(snapshots) != 1:
        raise RuntimeError(f"expected one pinned embedding snapshot, found {snapshots}")
    model_path = snapshots[0] / "onnx" / "model.onnx"
    tokenizer = AutoTokenizer.from_pretrained(
        model_path.parent, local_files_only=True, trust_remote_code=False
    )
    session = ort.InferenceSession(
        str(model_path), providers=["CUDAExecutionProvider", "CPUExecutionProvider"]
    )
    if session.get_providers()[0] != "CUDAExecutionProvider":
        raise RuntimeError(
            "BGE-M3 ONNX CUDA provider is unavailable; launch with the NVIDIA library "
            "directories in LD_LIBRARY_PATH as shown in this script's docstring"
        )
    return tokenizer, session, snapshots[0].name


def _passages(documents: list[tuple[str, str]], tokenizer: Any) -> list[Passage]:
    """Create title-aware child passages within the production 420-token ceiling."""
    result: list[Passage] = []
    for position, (doc_id, text) in enumerate(documents, 1):
        title = next((line.strip() for line in text.splitlines() if line.strip()), doc_id)[:1000]
        title_ids = tokenizer(title, add_special_tokens=False)["input_ids"][:80]
        body_ids = tokenizer(text, add_special_tokens=False)["input_ids"]
        content_limit = max(420 - len(title_ids) - 4, 256)
        overlap = min(48, content_limit // 4)
        step = content_limit - overlap
        for offset in range(0, max(len(body_ids), 1), step):
            content_ids = body_ids[offset : offset + content_limit]
            content = tokenizer.decode(content_ids, skip_special_tokens=True)
            passage_id = f"{doc_id}::{offset // step}"
            result.append(
                Passage(
                    passage_id=passage_id,
                    doc_id=doc_id,
                    title=title,
                    text=f"{title} / {content}" if content else title,
                )
            )
            if offset + content_limit >= len(body_ids):
                break
        if position % 4096 == 0 or position == len(documents):
            print(f"production-shaped passages: {position}/{len(documents)} docs")
    return result


def _encode(
    texts: list[str],
    tokenizer: Any,
    session: Any,
    *,
    label: str,
    batch_size: int = 256,
    result: np.ndarray | None = None,
    start_at: int = 0,
    progress: Callable[[int], None] | None = None,
) -> np.ndarray:
    if result is None:
        result = np.empty((len(texts), 1024), dtype=np.float32)
    for start in range(start_at, len(texts), batch_size):
        stop = min(start + batch_size, len(texts))
        tokens = tokenizer(
            texts[start:stop],
            padding=True,
            truncation=True,
            max_length=512,
            return_tensors="np",
        )
        result[start:stop] = session.run(
            ["sentence_embedding"],
            {
                "input_ids": tokens["input_ids"].astype(np.int64, copy=False),
                "attention_mask": tokens["attention_mask"].astype(np.int64, copy=False),
            },
        )[0]
        if progress is not None:
            progress(stop)
        if stop == len(texts) or stop % 2048 == 0:
            print(f"{label}: {stop}/{len(texts)}")
    return result


def _passage_embeddings(
    passages: list[Passage], tokenizer: Any, session: Any, revision: str
) -> np.ndarray:
    cache = DATA / f"techqa_bge_m3_{revision}_passages420.npy"
    identity = DATA / f"techqa_bge_m3_{revision}_passages420.json"
    partial = DATA / f"techqa_bge_m3_{revision}_passages420.partial.npy"
    progress_file = DATA / f"techqa_bge_m3_{revision}_passages420.progress.json"
    ordered_ids_hash = hashlib.sha256(
        "\n".join(item.passage_id for item in passages).encode()
    ).hexdigest()
    expected = {
        "dataset_revision": TECHQA_REVISION,
        "model_revision": revision,
        "max_length": 512,
        "passage_tokens": 420,
        "overlap_tokens": 48,
        "passages": len(passages),
        "ordered_passage_ids_sha256": ordered_ids_hash,
    }
    if cache.exists() and identity.exists():
        actual = json.loads(identity.read_text(encoding="utf-8"))
        vectors = np.load(cache, mmap_mode="r")
        if actual == expected and vectors.shape == (len(passages), 1024):
            print(f"dense corpus cache verified: {cache}")
            return vectors

    completed = 0
    if partial.exists() and progress_file.exists():
        state = json.loads(progress_file.read_text(encoding="utf-8"))
        vectors = np.load(partial, mmap_mode="r+")
        if state.get("identity") == expected and vectors.shape == (len(passages), 1024):
            completed = int(state.get("completed", 0))
            print(f"resuming dense passage cache at {completed}/{len(passages)}")
        else:
            partial.unlink(missing_ok=True)
            progress_file.unlink(missing_ok=True)
            vectors = np.lib.format.open_memmap(
                partial, mode="w+", dtype=np.float32, shape=(len(passages), 1024)
            )
    else:
        vectors = np.lib.format.open_memmap(
            partial, mode="w+", dtype=np.float32, shape=(len(passages), 1024)
        )

    def checkpoint(stop: int) -> None:
        if stop % 2048 == 0 or stop == len(passages):
            vectors.flush()
            progress_file.write_text(
                json.dumps({"identity": expected, "completed": stop}) + "\n",
                encoding="utf-8",
            )

    _encode(
        [item.text for item in passages],
        tokenizer,
        session,
        label="dense passages",
        result=vectors,
        start_at=completed,
        progress=checkpoint,
    )
    vectors.flush()
    partial.replace(cache)
    identity.write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
    progress_file.unlink(missing_ok=True)
    return np.load(cache, mmap_mode="r")


def _top_dense(
    query_vectors: np.ndarray,
    passage_vectors: np.ndarray,
    document_ids: list[str],
    passage_document_indices: np.ndarray,
    *,
    k: int = 100,
) -> list[list[str]]:
    rankings: list[list[str]] = []
    for query in query_vectors:
        passage_scores = np.asarray(passage_vectors @ query)
        document_scores = np.full(len(document_ids), -np.inf, dtype=np.float32)
        np.maximum.at(document_scores, passage_document_indices, passage_scores)
        candidates = np.argpartition(document_scores, -k)[-k:]
        ordered = candidates[np.argsort(-document_scores[candidates], kind="stable")]
        rankings.append([document_ids[int(index)] for index in ordered])
    return rankings


def _distinct_documents(hits: list[dict[str, Any]], *, limit: int = 100) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        doc_id = str(hit["_source"]["doc_id"])
        if doc_id not in seen:
            seen.add(doc_id)
            result.append(doc_id)
            if len(result) >= limit:
                break
    return result


def _abstention_metrics(
    answerable: list[Outcome], impossible: list[Outcome]
) -> dict[str, Any]:
    calibration_answerable, evaluation_answerable = answerable[:40], answerable[40:]
    calibration_impossible, evaluation_impossible = impossible[:20], impossible[20:]
    candidates = sorted(
        {item.top_score for item in [*calibration_answerable, *calibration_impossible]}
    )
    best: tuple[float, float, float, float] | None = None
    for threshold in candidates:
        true_answer = statistics.fmean(
            item.top_score >= threshold for item in calibration_answerable
        )
        true_abstain = statistics.fmean(
            item.top_score < threshold for item in calibration_impossible
        )
        candidate = ((true_answer + true_abstain) / 2, true_abstain, true_answer, threshold)
        if best is None or candidate > best:
            best = candidate
    assert best is not None
    threshold = best[3]
    answer_rate = statistics.fmean(item.top_score >= threshold for item in evaluation_answerable)
    abstention_rate = statistics.fmean(
        item.top_score < threshold for item in evaluation_impossible
    )
    return {
        "threshold": round(float(threshold), 6),
        "calibration": {"answerable": 40, "impossible": 20},
        "held_out": {"answerable": 240, "impossible": 100},
        "answerable_answer_rate": round(answer_rate, 4),
        "impossible_abstention_rate": round(abstention_rate, 4),
        "balanced_accuracy": round((answer_rate + abstention_rate) / 2, 4),
    }


def _rrf(left: list[str], right: list[str], *, k: int = 30) -> list[str]:
    scores: dict[str, float] = {}
    for ranking in (left, right):
        for rank, doc_id in enumerate(ranking, 1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (60 + rank)
    return [
        doc_id for doc_id, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:k]
    ]


def _markdown(payload: dict[str, Any]) -> str:
    bm25 = payload["retrieval"]["bm25"]
    dense = payload["retrieval"]["dense"]
    hybrid = payload["retrieval"]["hybrid_rrf"]
    rerank = payload["retrieval"]["hybrid_bge_rerank"]
    gates = payload["gates"]
    return "\n".join(
        [
            "# Phase 4 proxy release evaluation",
            "",
            f"Status: **{payload['status']}**",
            "",
            f"Queries: {payload['queries']['total']} "
            f"({payload['queries']['answerable']} answerable / "
            f"{payload['queries']['impossible']} impossible).",
            "",
            "| run | Recall@5 | Recall@10 | Recall@20 | MRR@10 | NDCG@10 |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
            f"| BM25 | {bm25['recall_at_5']:.4f} | {bm25['recall_at_10']:.4f} | "
            f"{bm25['recall_at_20']:.4f} | {bm25['mrr_at_10']:.4f} | "
            f"{bm25['ndcg_at_10']:.4f} |",
            f"| BGE-M3 dense | {dense['recall_at_5']:.4f} | "
            f"{dense['recall_at_10']:.4f} | {dense['recall_at_20']:.4f} | "
            f"{dense['mrr_at_10']:.4f} | {dense['ndcg_at_10']:.4f} |",
            f"| RRF hybrid | {hybrid['recall_at_5']:.4f} | "
            f"{hybrid['recall_at_10']:.4f} | {hybrid['recall_at_20']:.4f} | "
            f"{hybrid['mrr_at_10']:.4f} | {hybrid['ndcg_at_10']:.4f} |",
            f"| RRF hybrid + BGE rerank | {rerank['recall_at_5']:.4f} | "
            f"{rerank['recall_at_10']:.4f} | {rerank['recall_at_20']:.4f} | "
            f"{rerank['mrr_at_10']:.4f} | {rerank['ndcg_at_10']:.4f} |",
            "",
            "## Gates",
            "",
            *[
                f"- {'PASS' if item['passed'] else 'FAIL'}: {name} {item['actual']} "
                f"{item['operator']} {item['threshold']}"
                for name, item in gates.items()
            ],
            "",
            "## Scope",
            "",
            "This is a reproducible external silver benchmark. It replaces unavailable "
            "private logs for engineering closure, but does not claim tenant-domain human gold.",
            "",
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--reranker-batch-size", type=int, default=32)
    parser.add_argument("--keep-index", action="store_true")
    args = parser.parse_args()

    rows = json.loads((DATA / "train.json").read_text(encoding="utf-8"))
    answerable, impossible = _select(rows)
    selected = [*answerable, *impossible]
    selection = _selection_payload(answerable, impossible)
    SELECTION.write_text(json.dumps(selection, indent=2) + "\n", encoding="utf-8")
    known_files = {doc_id for doc_id, _ in _corpus_documents()}
    missing = {
        context["filename"]
        for row in answerable
        for context in row.get("contexts", [])
        if context["filename"] not in known_files
    }
    if missing:
        raise RuntimeError(f"TechQA qrels do not match corpus: {sorted(missing)[:10]}")

    url, auth = _config()
    index = f"sm-techqa-proxy-{uuid.uuid4().hex[:8]}"
    outcomes: list[Outcome] = []
    diagnostics: list[dict[str, Any]] = []
    started = time.perf_counter()
    with httpx.Client(base_url=url, auth=auth, verify=False, timeout=60, trust_env=False) as client:
        _request(
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
            documents = _corpus_documents()
            tokenizer, embedding_session, embedding_revision = _embedding_session()
            passages = _passages(documents, tokenizer)
            _bulk_index(client, index, passages)
            _request(client, "POST", f"/{index}/_refresh")
            _request(
                client, "PUT", f"/{index}/_settings", json_body={"index.refresh_interval": "1s"}
            )

            provisional: list[tuple[dict[str, Any], list[str], float]] = []
            for row in selected:
                tick = time.perf_counter()
                response = _request(
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
                elapsed_ms = (time.perf_counter() - tick) * 1000
                hits = _distinct_documents(response["hits"]["hits"])
                provisional.append((row, hits, elapsed_ms))

            passage_vectors = _passage_embeddings(
                passages, tokenizer, embedding_session, embedding_revision
            )
            query_vectors = _encode(
                [row["question"] for row in selected],
                tokenizer,
                embedding_session,
                label="dense queries",
            )
            document_ids = [doc_id for doc_id, _ in documents]
            document_index = {doc_id: index for index, doc_id in enumerate(document_ids)}
            passage_document_indices = np.asarray(
                [document_index[item.doc_id] for item in passages], dtype=np.int32
            )
            dense_rankings = _top_dense(
                query_vectors,
                passage_vectors,
                document_ids,
                passage_document_indices,
            )
            # ONNX Runtime's CUDA arena can retain most GPU memory. The dense
            # rankings are now CPU values, so release that session before loading
            # the PyTorch cross encoder.
            del embedding_session, query_vectors, passage_vectors
            gc.collect()
            hybrid_rankings = [
                _rrf(bm25_hits, dense_hits)
                for (_, bm25_hits, _), dense_hits in zip(provisional, dense_rankings, strict=True)
            ]

            predict, reranker_name, reranker_revision = _reranker(
                args.device, args.reranker_batch_size
            )
            passages_by_doc: dict[str, list[str]] = {}
            for passage in passages:
                passages_by_doc.setdefault(passage.doc_id, []).append(passage.text)
            pairs = [
                (row["question"], passage)
                for (row, _, _), hybrid in zip(provisional, hybrid_rankings, strict=True)
                for doc_id in hybrid
                for passage in passages_by_doc[doc_id]
            ]
            raw_scores = _reranker_scores(pairs, predict, reranker_revision)
            scores = 1.0 / (1.0 + np.exp(-np.clip(raw_scores, -60, 60)))
            offset = 0
            for (row, bm25_hits, elapsed_ms), dense_hits, hybrid in zip(
                provisional, dense_rankings, hybrid_rankings, strict=True
            ):
                doc_scores: list[tuple[float, str]] = []
                for doc_id in hybrid:
                    count = len(passages_by_doc[doc_id])
                    local = scores[offset : offset + count]
                    offset += count
                    doc_scores.append((float(local.max()), doc_id))
                sorted_doc_scores = sorted(doc_scores, key=lambda item: (-item[0], item[1]))
                hybrid_reranked = [doc_id for _, doc_id in sorted_doc_scores]
                relevant = frozenset(
                    context["filename"] for context in row.get("contexts", [])
                )
                outcomes.append(
                    Outcome(
                        query_id=row["id"],
                        relevant=relevant,
                        bm25=tuple(bm25_hits),
                        dense=tuple(dense_hits),
                        hybrid=tuple(hybrid),
                        hybrid_reranked=tuple(hybrid_reranked),
                        search_ms=elapsed_ms,
                        top_score=sorted_doc_scores[0][0] if sorted_doc_scores else 0.0,
                    )
                )
                diagnostics.append(
                    {
                        "query_id": row["id"],
                        "question": row["question"],
                        "relevant": sorted(relevant),
                        "bm25": bm25_hits,
                        "dense": dense_hits,
                        "hybrid": hybrid,
                        "hybrid_reranked": hybrid_reranked,
                        "reranker_scores": {
                            doc_id: score for score, doc_id in sorted_doc_scores
                        },
                    }
                )

            (DATA / "phase4_proxy_candidate_diagnostics.json").write_text(
                json.dumps(diagnostics, ensure_ascii=False) + "\n", encoding="utf-8"
            )

            answerable_outcomes = outcomes[: len(answerable)]
            impossible_outcomes = outcomes[len(answerable) :]
            bm25 = _score(answerable_outcomes, "bm25")
            dense = _score(answerable_outcomes, "dense")
            hybrid = _score(answerable_outcomes, "hybrid")
            reranked = _score(answerable_outcomes, "hybrid_reranked")
            abstention = _abstention_metrics(answerable_outcomes, impossible_outcomes)
            gate_specs = {
                "recall_at_5": (reranked["recall_at_5"], 0.85),
                "recall_at_10": (reranked["recall_at_10"], 0.90),
                "mrr_at_10": (reranked["mrr_at_10"], 0.75),
                "ndcg_at_10": (reranked["ndcg_at_10"], 0.80),
                "impossible_abstention_rate": (
                    abstention["impossible_abstention_rate"],
                    0.90,
                ),
                "answerable_answer_rate": (abstention["answerable_answer_rate"], 0.90),
            }
            gates = {
                name: {
                    "actual": actual,
                    "operator": ">=",
                    "threshold": threshold,
                    "passed": actual >= threshold,
                }
                for name, (actual, threshold) in gate_specs.items()
            }
            payload = {
                "schema_version": "phase4-proxy-release-v1.3",
                "status": (
                    "passed_with_documented_proxy"
                    if all(gate["passed"] for gate in gates.values())
                    else "failed"
                ),
                "source": {
                    "dataset": "nvidia/TechQA-RAG-Eval",
                    "revision": TECHQA_REVISION,
                    "corpus_documents": len(documents),
                    "production_shaped_passages": len(passages),
                    "label_tier": "external_silver_no_tenant_human_signoff",
                },
                "queries": {"total": 400, "answerable": 280, "impossible": 120},
                "retrieval": {
                    "bm25": bm25,
                    "dense": dense,
                    "hybrid_rrf": hybrid,
                    "hybrid_bge_rerank": reranked,
                    "held_out_abstention": abstention,
                    "embedding": {
                        "model": "BAAI/bge-m3",
                        "revision": embedding_revision,
                        "backend": "ONNX Runtime CUDA exact cosine",
                        "passage_tokens": 420,
                        "overlap_tokens": 48,
                    },
                    "reranker": {
                        "model": reranker_name,
                        "revision": reranker_revision,
                        "activation": "sigmoid",
                    },
                    "bm25_latency_ms": {
                        "p50": round(float(np.quantile([o.search_ms for o in outcomes], 0.5)), 2),
                        "p95": round(float(np.quantile([o.search_ms for o in outcomes], 0.95)), 2),
                    },
                },
                "gates": gates,
                "duration_seconds": round(time.perf_counter() - started, 2),
                "limitations": [
                    "public technical-support distribution, not private tenant query distribution",
                    "source-provided silver labels, not independent ServiceMind human qrels",
                    "passages use the pinned BGE tokenizer instead of cl100k, while preserving the production 420-token/48-overlap shape",
                    "dense document ranking uses exact max pooling over child passages; production uses approximate OpenSearch HNSW before parent expansion",
                    "abstention is a retrieval-score proxy; end-to-end answer refusal remains a separate semantic gate",
                ],
            }
            REPORTS.mkdir(parents=True, exist_ok=True)
            (REPORTS / "phase4_proxy_release_latest.json").write_text(
                json.dumps(payload, indent=2) + "\n", encoding="utf-8"
            )
            (REPORTS / "phase4_proxy_release_latest.md").write_text(
                _markdown(payload), encoding="utf-8"
            )
            print(json.dumps(payload, indent=2))
        finally:
            if not args.keep_index:
                client.delete(f"/{index}")


if __name__ == "__main__":
    main()
