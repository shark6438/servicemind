"""Run the reproducible Phase 4 proxy release gate without private query logs.

The benchmark uses the pinned NVIDIA TechQA labels as external silver truth. It
indexes production-shaped child passages from the full 28,481-file corpus, selects a
deterministic 400-query release set (280 answerable + 120 officially impossible),
measures exact max-pooled BGE-M3 dense retrieval and RRF hybrid retrieval, then
reranks the hybrid candidates by each document's best child passage. It reports three
reranked arms: the published v1.3 protocol (top-30 pool, pure rerank), the same
protocol at production depth, and the production shape (depth-100 pool plus the
0.85/0.15 rerank/retrieval blend from `rag/service.py`). Impossible queries
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
BASELINE = REPORTS / "phase4_proxy_regression_baseline.json"
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
    hybrid_reranked_depth100: tuple[str, ...]
    production_blend: tuple[str, ...]
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


def _rerank_weight() -> float:
    """`SERVICEMIND_RAG_RERANK_WEIGHT`, read the same way production reads `.env`."""
    file_values = dotenv_values(ROOT / ".env")
    raw = os.getenv("SERVICEMIND_RAG_RERANK_WEIGHT") or str(
        file_values.get("SERVICEMIND_RAG_RERANK_WEIGHT") or ""
    )
    weight = float(raw) if raw else 0.85
    if not 0.0 <= weight <= 1.0:
        raise RuntimeError(f"SERVICEMIND_RAG_RERANK_WEIGHT out of range: {weight}")
    return weight


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
    device: str, batch_size: int, max_length: int = 0
) -> tuple[Callable[[list[tuple[str, str]]], np.ndarray], str, str, int]:
    """Load the pinned cross-encoder and report the sequence window it actually uses.

    `max_length=0` means "no override", which is what production does: `BgeM3Reranker`
    constructs `CrossEncoder` without a `max_length`, so it inherits the checkpoint
    tokenizer's `model_max_length` (8192 for bge-reranker-v2-m3). An earlier revision of
    this harness hardcoded 512, which silently truncated every child passage - the
    production-shaped chunks run p50=1313 BGE tokens - so the scored text was not the
    text production scores. The window is returned rather than assumed so it can be
    written into both the score cache identity and the report.
    """
    from sentence_transformers import CrossEncoder

    snapshots = sorted(
        (ROOT / "data" / "phase4" / "models" / "reranker").glob(
            "models--BAAI--bge-reranker-v2-m3/snapshots/*"
        )
    )
    if len(snapshots) != 1:
        raise RuntimeError(f"expected one pinned reranker snapshot, found {snapshots}")
    kwargs: dict[str, Any] = {"device": device, "trust_remote_code": False}
    if max_length:
        kwargs["max_length"] = max_length
    model: Any = CrossEncoder(str(snapshots[0]), **kwargs)
    effective = int(model.max_seq_length)
    if max_length and effective != max_length:
        raise RuntimeError(
            f"requested reranker max_length={max_length} but the model reports {effective}"
        )

    def predict(pairs: list[tuple[str, str]]) -> np.ndarray:
        return np.asarray(
            model.predict(pairs, batch_size=batch_size, show_progress_bar=False)
        ).reshape(-1)

    return predict, "BAAI/bge-reranker-v2-m3", snapshots[0].name, effective


def _reranker_scores(
    pairs: list[tuple[str, str]],
    predict: Callable[[list[tuple[str, str]]], np.ndarray],
    revision: str,
    *,
    tag: str = "pool30",
    max_length: int = 0,
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
        "max_length": max_length,
        "pool": tag,
        "pairs": len(pairs),
        "ordered_pairs_sha256": digest.hexdigest(),
    }
    # The pool tag keeps the published depth-30 cache and the production depth-100
    # cache side by side instead of overwriting each other on every protocol change.
    stem = f"techqa_bge_reranker_{revision}_passages420_{tag}"
    cache = DATA / f"{stem}.npy"
    identity = DATA / f"{stem}.json"
    partial = DATA / f"{stem}.partial.npy"
    progress_file = DATA / f"{stem}.progress.json"
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


def _reference_gates(
    reranked: dict[str, Any], abstention: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """The §4.1 closure gate, recorded but explicitly not evaluated on this set.

    `docs/PHASE4_EVALUATION_BASELINE_V1_2.md` §4.1 defines these thresholds as the
    Phase 4 *closure* gate and §3/§6.4 scope that gate to the tenant-domain release
    set: private tenant queries, expert-signed qrels, ACL and tenant strata. This
    harness scores a public cross-domain set with source-provided silver labels, so
    the two are not comparable. `applicable: false` is a scope statement, not a
    relaxation - the tenant-domain gate stays open and stays an explicit quality
    exception until Phase 7 produces private qrels, and `passed` stays `None` so no
    reader can mistake the recorded numbers for a verdict.
    """
    thresholds = {
        "recall_at_5": (reranked["recall_at_5"], 0.85),
        "recall_at_10": (reranked["recall_at_10"], 0.90),
        "mrr_at_10": (reranked["mrr_at_10"], 0.75),
        "ndcg_at_10": (reranked["ndcg_at_10"], 0.80),
        "impossible_abstention_rate": (abstention["impossible_abstention_rate"], 0.90),
        "answerable_answer_rate": (abstention["answerable_answer_rate"], 0.90),
    }
    gates = {
        name: {
            "actual": actual,
            "operator": ">=",
            "threshold": threshold,
            "applicable": False,
            "passed": None,
            "gate_definition": "docs/PHASE4_EVALUATION_BASELINE_V1_2.md §4.1",
            "required_input": "tenant-domain release set: private queries + expert qrels",
            "reason": "not evaluated: this run scores an external silver set, not tenant qrels",
        }
        for name, (actual, threshold) in thresholds.items()
    }
    abstention_reason = (
        "not evaluated: production abstains through the Reviewer's semantic ABSTAIN on "
        "evidence sufficiency, not through a retrieval-score cut; on this set the score "
        f"separates answerable from impossible at ROC-AUC {abstention['top_score_roc_auc']} "
        f"with a best-case balanced accuracy of {abstention['best_case_balanced_accuracy']} "
        "over every cut"
    )
    for name in ("impossible_abstention_rate", "answerable_answer_rate"):
        gates[name]["reason"] = abstention_reason
    return gates


def _reranker_probabilities(raw_scores: np.ndarray) -> np.ndarray:
    """Return the reranker's own probabilities, without rescaling them.

    `sentence_transformers.CrossEncoder` applies the checkpoint's sigmoid activation
    itself whenever the model has `num_labels == 1`, so `bge-reranker-v2-m3` already
    scores in [0, 1]. An earlier revision of this script applied a second sigmoid on
    top, which compressed every score into (0.5, 0.731] and understated how confident
    the reranker was. Rankings, Recall@k, MRR and NDCG were unaffected (sigmoid is
    strictly monotone) and the abstention threshold is calibrated on the same values,
    so only the reported `top_score` was wrong. The range check below is the contract
    that makes the single-sigmoid assumption verifiable instead of assumed.
    """
    values = np.asarray(raw_scores, dtype=np.float64)
    if values.size and not (float(values.min()) >= 0.0 and float(values.max()) <= 1.0):
        raise RuntimeError(
            "reranker scores fall outside [0, 1]: the CrossEncoder activation "
            "assumption no longer holds and every reported score would be mis-scaled"
        )
    return values


def _proxy_regression(
    metrics: dict[str, float],
    source: dict[str, Any],
    *,
    update: bool,
    baseline_path: Path | None = None,
    tolerance: float = 0.005,
) -> tuple[dict[str, dict[str, Any]], str, float]:
    """Compare this run against the frozen proxy baseline (baseline doc §6.4).

    The baseline is written by `--update-baseline` after a reviewed change to the
    pinned model revisions, chunking or fusion - never to silence a drop. A stored
    baseline taken under different revisions is refused rather than silently compared.
    """
    path = baseline_path or BASELINE
    payload = {
        "schema_version": "phase4-proxy-regression-baseline-v1",
        "note": (
            "frozen proxy metrics for the §6.4 non-regression rule; refresh with "
            "`--update-baseline` only after a reviewed configuration change"
        ),
        "source": source,
        "metrics": metrics,
    }
    if update or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return (
            {
                name: {"baseline": value, "actual": value, "tolerance": 0.0, "passed": True}
                for name, value in metrics.items()
            },
            "regression_baseline_created",
            0.0,
        )
    stored = json.loads(path.read_text(encoding="utf-8"))
    if stored.get("source") != source:
        raise RuntimeError(
            "proxy regression baseline was frozen under a different dataset or model "
            "revision; re-run with --update-baseline only if that change was reviewed"
        )
    comparison = {
        name: {
            "baseline": stored["metrics"][name],
            "actual": value,
            "tolerance": tolerance,
            "passed": value >= stored["metrics"][name] - tolerance,
        }
        for name, value in metrics.items()
    }
    status = (
        "regression_passed"
        if all(item["passed"] for item in comparison.values())
        else "regression_failed"
    )
    return comparison, status, tolerance


def _abstention_metrics(answerable: list[Outcome], impossible: list[Outcome]) -> dict[str, Any]:
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
    abstention_rate = statistics.fmean(item.top_score < threshold for item in evaluation_impossible)
    # How much answerability signal the retrieval score actually carries on this set.
    # ROC-AUC is threshold-free; the in-sample best balanced accuracy is an upper bound
    # over every possible cut, so it bounds what *any* score-threshold abstention rule
    # (calibrated or not) could reach here. Both are reported so the abstention gate
    # cannot be read as a tuning problem: if the ceiling is below the gate, no threshold
    # choice passes it.
    positives = [item.top_score for item in evaluation_answerable]
    negatives = [item.top_score for item in evaluation_impossible]
    auc = statistics.fmean(
        [
            1.0 if positive > negative else 0.5 if positive == negative else 0.0
            for positive in positives
            for negative in negatives
        ]
    )
    in_sample_best = max(
        (
            statistics.fmean(score >= cut for score in positives)
            + statistics.fmean(score < cut for score in negatives)
        )
        / 2
        for cut in sorted(
            {item.top_score for item in [*evaluation_answerable, *evaluation_impossible]}
        )
    )
    return {
        "threshold": round(float(threshold), 6),
        "calibration": {"answerable": 40, "impossible": 20},
        "held_out": {"answerable": 240, "impossible": 100},
        "answerable_answer_rate": round(answer_rate, 4),
        "impossible_abstention_rate": round(abstention_rate, 4),
        "balanced_accuracy": round((answer_rate + abstention_rate) / 2, 4),
        "top_score_roc_auc": round(auc, 4),
        "best_case_balanced_accuracy": round(in_sample_best, 4),
    }


RRF_RANK_CONSTANT = 60  # matches the production pipeline `servicemind-rag-rrf-v1`


def _rrf_scores(left: list[str], right: list[str]) -> dict[str, float]:
    """Production RRF: each arm contributes 1/(rank_constant + rank).

    An arm that did not return a document contributes nothing for it, so a document
    found by only one arm scores below one found by both.
    """
    scores: dict[str, float] = {}
    for ranking in (left, right):
        for rank, doc_id in enumerate(ranking, 1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (RRF_RANK_CONSTANT + rank)
    return scores


def _rrf(left: list[str], right: list[str], *, k: int = 30) -> list[str]:
    scores = _rrf_scores(left, right)
    return [
        doc_id for doc_id, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:k]
    ]


def _blend_ranking(
    doc_ids: list[str],
    rerank_scores: dict[str, float],
    rrf_scores: dict[str, float],
    *,
    rerank_weight: float,
    depth: int,
) -> tuple[str, ...]:
    """Reproduce the production ordering exactly (rag/service.py `final_score`).

    `rerank_weight * rerank_score + (1 - rerank_weight) * minmax(retrieval_score)`,
    where the retrieval score is the fused RRF score of each candidate in the reranked
    window. This is not a monotone transform of the rerank score, so it genuinely
    reorders: on the pinned proxy set it beats pure reranking on every retrieval metric.
    """
    window = doc_ids[:depth]
    fused = [rrf_scores[doc_id] for doc_id in window]
    low, high = min(fused), max(fused)
    span = high - low

    def final_score(doc_id: str) -> float:
        retrieval = (rrf_scores[doc_id] - low) / span if span > 1e-12 else 0.5
        return rerank_weight * rerank_scores[doc_id] + (1 - rerank_weight) * retrieval

    return tuple(sorted(window, key=lambda doc_id: (-final_score(doc_id), doc_id)))


def _markdown(payload: dict[str, Any]) -> str:
    bm25 = payload["retrieval"]["bm25"]
    dense = payload["retrieval"]["dense"]
    hybrid = payload["retrieval"]["hybrid_rrf"]
    rerank = payload["retrieval"]["hybrid_bge_rerank"]
    rerank100 = payload["retrieval"]["hybrid_bge_rerank_depth100"]
    production = payload["retrieval"]["production"]
    gates = payload["gates"]
    regression = payload["proxy_regression"]
    verdict = (
        "baseline created by this run"
        if payload["status"] == "regression_baseline_created"
        else f"{regression['rule']} -> {payload['status']}"
    )
    return "\n".join(
        [
            "# Phase 4 proxy release evaluation",
            "",
            f"Status: **{payload['status']}** — {payload['status_semantics']}",
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
            f"| RRF hybrid + BGE rerank (published: top-30, pure) | {rerank['recall_at_5']:.4f} | "
            f"{rerank['recall_at_10']:.4f} | {rerank['recall_at_20']:.4f} | "
            f"{rerank['mrr_at_10']:.4f} | {rerank['ndcg_at_10']:.4f} |",
            f"| + depth-100 pool, pure rerank | {rerank100['recall_at_5']:.4f} | "
            f"{rerank100['recall_at_10']:.4f} | {rerank100['recall_at_20']:.4f} | "
            f"{rerank100['mrr_at_10']:.4f} | {rerank100['ndcg_at_10']:.4f} |",
            f"| **production shape (depth-100 + blend)** | {production['recall_at_5']:.4f} | "
            f"{production['recall_at_10']:.4f} | {production['recall_at_20']:.4f} | "
            f"{production['mrr_at_10']:.4f} | {production['ndcg_at_10']:.4f} |",
            "",
            f"The production arm applies `{payload['retrieval']['production_blend']['formula']}` "
            f"with rerank_weight={payload['retrieval']['production_blend']['rerank_weight']}, "
            "which is what `src/servicemind/rag/service.py` actually runs. The published "
            "arm is kept so the historical numbers stay comparable.",
            "",
            "## Proxy regression (§6.4)",
            "",
            f"Verdict: **{verdict}**",
            "",
            "| metric | baseline | actual | tolerance | held |",
            "| --- | ---: | ---: | ---: | :---: |",
            *[
                f"| {name} | {item['baseline']:.4f} | {item['actual']:.4f} | "
                f"{item['tolerance']:.4f} | {'yes' if item['passed'] else 'NO'} |"
                for name, item in payload["proxy_regression"]["metrics"].items()
            ],
            "",
            "## Tenant-domain closure gate (§4.1) - not evaluated",
            "",
            "| gate | observed on this proxy set | required on tenant release set | status |",
            "| --- | ---: | ---: | --- |",
            *[
                f"| {name} | {item['actual']} | {item['operator']} {item['threshold']} | "
                "NOT EVALUATED |"
                for name, item in gates.items()
            ],
            "",
            gates["impossible_abstention_rate"]["reason"] + ".",
            "",
            "## Scope",
            "",
            "This is a reproducible external silver benchmark. It replaces unavailable "
            "private logs for engineering closure, but does not claim tenant-domain human "
            "gold and grants no quality certification.",
            "",
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--reranker-batch-size", type=int, default=32)
    parser.add_argument(
        "--reranker-max-length",
        type=int,
        default=0,
        help=(
            "sequence window for the cross encoder; 0 (default) uses the pinned "
            "checkpoint's own window, matching production"
        ),
    )
    parser.add_argument("--keep-index", action="store_true")
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="refresh the frozen §6.4 non-regression baseline (reviewed changes only)",
    )
    parser.add_argument(
        "--rerank-depth",
        type=int,
        default=100,
        help="candidate depth reranked, matching SERVICEMIND_RAG_CANDIDATE_K",
    )
    parser.add_argument(
        "--published-depth",
        type=int,
        default=30,
        help="candidate depth of the published v1.3 protocol, kept for continuity",
    )
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
            # `fused` is the depth-100 candidate pool the production retrieval path
            # reranks (`SERVICEMIND_RAG_CANDIDATE_K`). `hybrid` is its top 30, which is
            # the pool the published protocol froze; `_rrf` sorts by the same scores, so
            # `fused[:30]` is byte-identical to `_rrf(..., k=30)`.
            fused_rankings = [
                _rrf(bm25_hits, dense_hits, k=args.rerank_depth)
                for (_, bm25_hits, _), dense_hits in zip(provisional, dense_rankings, strict=True)
            ]
            hybrid_rankings = [fused[: args.published_depth] for fused in fused_rankings]
            fused_scores = [
                _rrf_scores(bm25_hits, dense_hits)
                for (_, bm25_hits, _), dense_hits in zip(provisional, dense_rankings, strict=True)
            ]
            rerank_weight = _rerank_weight()

            predict, reranker_name, reranker_revision, reranker_window = _reranker(
                args.device, args.reranker_batch_size, args.reranker_max_length
            )
            passages_by_doc: dict[str, list[str]] = {}
            for passage in passages:
                passages_by_doc.setdefault(passage.doc_id, []).append(passage.text)
            pairs = [
                (row["question"], passage)
                for (row, _, _), fused in zip(provisional, fused_rankings, strict=True)
                for doc_id in fused
                for passage in passages_by_doc[doc_id]
            ]
            scores = _reranker_probabilities(
                _reranker_scores(
                    pairs,
                    predict,
                    reranker_revision,
                    tag=f"pool{args.rerank_depth}",
                    max_length=reranker_window,
                )
            )
            offset = 0
            for (row, bm25_hits, elapsed_ms), dense_hits, fused, hybrid, rrf in zip(
                provisional,
                dense_rankings,
                fused_rankings,
                hybrid_rankings,
                fused_scores,
                strict=True,
            ):
                doc_scores: list[tuple[float, str]] = []
                for doc_id in fused:
                    count = len(passages_by_doc[doc_id])
                    local = scores[offset : offset + count]
                    offset += count
                    doc_scores.append((float(local.max()), doc_id))
                sorted_doc_scores = sorted(doc_scores, key=lambda item: (-item[0], item[1]))
                by_doc = {doc_id: score for score, doc_id in doc_scores}
                # The published protocol reranks only the top-30 pool and orders those
                # 30; it never sees candidates 31-100. Filter rather than truncate, or
                # the "published" arm would silently become depth-100 with a shorter tail.
                published_pool = set(hybrid)
                hybrid_reranked = [
                    doc_id for _, doc_id in sorted_doc_scores if doc_id in published_pool
                ]
                production_blend = _blend_ranking(
                    [doc_id for _, doc_id in sorted_doc_scores],
                    by_doc,
                    rrf,
                    rerank_weight=rerank_weight,
                    depth=args.rerank_depth,
                )
                relevant = frozenset(context["filename"] for context in row.get("contexts", []))
                outcomes.append(
                    Outcome(
                        query_id=row["id"],
                        relevant=relevant,
                        bm25=tuple(bm25_hits),
                        dense=tuple(dense_hits),
                        hybrid=tuple(hybrid),
                        hybrid_reranked=tuple(hybrid_reranked),
                        hybrid_reranked_depth100=tuple(doc_id for _, doc_id in sorted_doc_scores),
                        production_blend=production_blend,
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
                        "production_blend": list(production_blend),
                        "reranker_scores": {doc_id: score for score, doc_id in sorted_doc_scores},
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
            reranked_depth100 = _score(answerable_outcomes, "hybrid_reranked_depth100")
            production = _score(answerable_outcomes, "production_blend")
            abstention = _abstention_metrics(answerable_outcomes, impossible_outcomes)
            regression_metrics = {
                name: production[name]
                for name in (
                    "recall_at_5",
                    "recall_at_10",
                    "recall_at_20",
                    "mrr_at_10",
                    "ndcg_at_10",
                )
            }
            gates = _reference_gates(production, abstention)
            # Second tier: what this harness *can* decide. §6.4 requires only that the
            # proxy metrics do not degrade, so compare against the frozen proxy baseline.
            regression, regression_status, regression_tolerance = _proxy_regression(
                regression_metrics,
                {
                    "dataset": "nvidia/TechQA-RAG-Eval",
                    "revision": TECHQA_REVISION,
                    "embedding_model": "BAAI/bge-m3",
                    "embedding_revision": embedding_revision,
                    "reranker_model": reranker_name,
                    "reranker_revision": reranker_revision,
                },
                update=args.update_baseline,
            )
            payload = {
                "schema_version": "phase4-proxy-release-v1.4",
                "status": regression_status,
                "quality_certification": False,
                "status_semantics": (
                    "this report certifies nothing about tenant-domain RAG quality. It "
                    "reports proxy retrieval metrics on an external silver set and whether "
                    "they held against the frozen proxy baseline. The §4.1 closure gate is "
                    "listed but not evaluated; see `gates`."
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
                    "hybrid_bge_rerank_depth100": reranked_depth100,
                    "production": production,
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
                        "activation": "sigmoid (applied once, by the checkpoint)",
                        "max_length": reranker_window,
                        "max_length_source": (
                            "explicit --reranker-max-length"
                            if args.reranker_max_length
                            else "pinned checkpoint tokenizer model_max_length (production shape)"
                        ),
                        "reranked_pool_depth": args.rerank_depth,
                    },
                    "production_blend": {
                        "formula": (
                            "rerank_weight * rerank_score + "
                            "(1 - rerank_weight) * minmax(fused_rrf_score)"
                        ),
                        "rerank_weight": rerank_weight,
                        "source": "src/servicemind/rag/service.py `final_score`",
                        "published_protocol": (
                            f"the published v1.3 report measured RRF top-{args.published_depth} "
                            "with pure reranking; this run keeps that arm for continuity and "
                            "adds the depth-100 production configuration alongside it"
                        ),
                    },
                    "bm25_latency_ms": {
                        "p50": round(float(np.quantile([o.search_ms for o in outcomes], 0.5)), 2),
                        "p95": round(float(np.quantile([o.search_ms for o in outcomes], 0.95)), 2),
                    },
                },
                "gates": gates,
                "proxy_regression": {
                    "rule": "docs/PHASE4_EVALUATION_BASELINE_V1_2.md §6.4 (no further degradation)",
                    "baseline_file": str(BASELINE.relative_to(ROOT)),
                    "tolerance": (
                        0.0
                        if regression_status == "regression_baseline_created"
                        else regression_tolerance
                    ),
                    "metrics": regression,
                },
                "duration_seconds": round(time.perf_counter() - started, 2),
                "limitations": [
                    "public technical-support distribution, not private tenant query distribution",
                    "source-provided silver labels, not independent ServiceMind human qrels",
                    "the §4.1 closure gate is reported but not evaluated here; it needs private tenant queries with expert qrels",
                    "each answerable query carries a single relevant filename, so a rank-2 hit scores the same as a miss under Recall@10",
                    "passages use the pinned BGE tokenizer instead of cl100k, while preserving the production 420-token/48-overlap shape",
                    "dense document ranking uses exact max pooling over child passages; production uses approximate OpenSearch HNSW before parent expansion",
                    "abstention is measured as a retrieval-score cut, which the data shows cannot separate answerable from impossible here; production refuses through the Reviewer's semantic ABSTAIN instead",
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
