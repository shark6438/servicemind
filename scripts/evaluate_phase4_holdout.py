"""Phase 4 third-arm pre-registration: the ONE confirmatory run on the untouched holdout.

Frozen protocol: `docs/PHASE4_THIRD_ARM_PREREGISTRATION_2026-09-15.md`.
This script refuses to start unless that file still hashes to the value recorded in its
sidecar `...freeze.json`, so an edited protocol cannot silently inherit this run's result.

What it does, in order:

  1. Rebuilds the 910-row TechQA split and asserts the holdout is 330 answerable +
     180 impossible -- the complement of the 400 already analysed.
  2. **Anchor check first.** Scores C0 (the incumbent two-arm configuration) on the
     already-analysed dev set through the *same* TEI path the holdout will use, and
     aborts unless it reproduces the four published numbers (0.6857 / 0.7643 / 0.5848 /
     0.6279). A mis-wired pipeline therefore dies before it can consume the holdout.
  3. Scores C0 and C1 (C0 + the Qwen3 dense arm, production equal-weight three-way RRF)
     once on the holdout, with the frozen quantiles and bootstrap seed.
  4. Writes the three-state verdict of protocol §7 verbatim into the report.

Only C0 and C1 are produced. No knob configuration is evaluated here, by design.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.util
import json
import os
import statistics
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "phase4" / "raw" / "eval" / "techqa-rag-eval"
REPORTS = ROOT / "evaluation" / "reports"
PROTOCOL = ROOT / "docs" / "PHASE4_THIRD_ARM_PREREGISTRATION_2026-09-15.md"
FREEZE = ROOT / "docs" / "PHASE4_THIRD_ARM_PREREGISTRATION_2026-09-15.freeze.json"
REPORT_STEM = "phase4_holdout_prereg_2026-09-15"

TECHQA_REVISION = "0b5bbc84b7f07d6d09d063130e90b716d8d4a32a"
DEV_ANSWERABLE, DEV_IMPOSSIBLE = 280, 120
HOLDOUT_ANSWERABLE, HOLDOUT_IMPOSSIBLE = 330, 180

RERANK_DEPTH = 100
RERANK_WEIGHT = 0.85
DELTA = 0.020
BOOTSTRAP_SAMPLES = 10_000
BOOTSTRAP_SEED = 20260915
QUANTILES_GAIN = (0.025, 0.975)
QUANTILES_NONINF = (0.05, 0.95)

RERANK_URL = os.getenv("EXP_RERANK_URL", "http://127.0.0.1:8086")
TEI_BATCH = 16
TEI_CONCURRENCY = 4
TEI_RERANKER_WINDOW = 8192

QWEN3_SNAPSHOT = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
QWEN3_TASK = (
    "Given a technical support question about IBM software products, retrieve the product "
    "documentation passage that answers it"
)
QWEN3_PASSAGES = "techqa_qwen3_0.6b_passages420.npy"
QWEN3_PASSAGES_SHA256 = "f5491a3392a15f9e751d6c1b5c26428aafdff69f86fd2b0e794a493e49d8d3fe"

METRICS = {
    "recall_at_5": ("recall", 5),
    "recall_at_10": ("recall", 10),
    "mrr_at_10": ("mrr", 10),
    "ndcg_at_10": ("ndcg", 10),
}
# The frozen C0 anchors, from the published v1.5 production-shaped run.
DEV_ANCHOR = {
    "recall_at_5": 0.6857,
    "recall_at_10": 0.7643,
    "mrr_at_10": 0.5848,
    "ndcg_at_10": 0.6279,
}
GAIN_LABELS = {
    "recall_at_5_overall": "Recall@5, all answerable",
    "recall_at_5_hard": "Recall@5, hard subset",
}
NONINF_LABELS = {
    "recall_at_10": "Recall@10",
    "mrr_at_10": "MRR@10",
    "ndcg_at_10": "NDCG@10",
}
# The harness's `Outcome` carries one field per arm; this protocol scores exactly one.
RANKING_FIELD = "production_blend"


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def _proxy_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "proxy_eval", ROOT / "scripts" / "evaluate_phase4_proxy_release.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load scripts/evaluate_phase4_proxy_release.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["proxy_eval"] = module  # dataclasses resolve `cls.__module__` at exec time
    spec.loader.exec_module(module)
    return module


def _verify_protocol() -> dict[str, Any]:
    """Refuse to run against a protocol that no longer matches its freeze record."""
    if not FREEZE.exists():
        raise RuntimeError(f"protocol is not frozen: {FREEZE} is missing")
    record = json.loads(FREEZE.read_text(encoding="utf-8"))
    expected = record["source"]["sha256"]
    actual = hashlib.sha256(PROTOCOL.read_bytes()).hexdigest()
    if actual != expected:
        raise RuntimeError(
            f"protocol {PROTOCOL.name} has changed since it was frozen; a confirmatory "
            f"run must not proceed against a modified protocol:\n"
            f"  frozen:  {expected}\n"
            f"  current: {actual}"
        )
    log(f"protocol frozen and verified: {actual}")
    return record


def _split(rows: list[dict[str, Any]], proxy: Any) -> tuple[list[dict], list[dict]]:
    """Rebuild the dev/holdout split and assert the holdout is the untouched complement."""
    dev_answerable, dev_impossible = proxy._select(rows)
    dev_ids = {row["id"] for row in (*dev_answerable, *dev_impossible)}
    holdout = sorted((row for row in rows if row["id"] not in dev_ids), key=proxy._rank_key)
    answerable = [row for row in holdout if not row["is_impossible"]]
    impossible = [row for row in holdout if row["is_impossible"]]
    if (len(answerable), len(impossible)) != (HOLDOUT_ANSWERABLE, HOLDOUT_IMPOSSIBLE):
        raise RuntimeError(
            f"holdout split is {len(answerable)} answerable + {len(impossible)} impossible, "
            f"expected {HOLDOUT_ANSWERABLE} + {HOLDOUT_IMPOSSIBLE}"
        )
    log(
        f"split ok: dev {len(dev_answerable)}+{len(dev_impossible)} consumed, "
        f"holdout {len(answerable)}+{len(impossible)} untouched"
    )
    return [*dev_answerable, *dev_impossible], [*answerable, *impossible]


def _tei_rerank(client: httpx.Client, query: str, texts: list[str]) -> list[float]:
    """Score `texts` against `query` with the production TEI cross-encoder."""
    scores = [0.0] * len(texts)
    chunks = [
        (start, texts[start : start + TEI_BATCH]) for start in range(0, len(texts), TEI_BATCH)
    ]
    for attempt in range(4):
        try:

            def call(item: tuple[int, list[str]]) -> tuple[int, Any]:
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
        except (httpx.HTTPError, OSError, ValueError) as exc:
            if attempt == 3:
                raise
            log(f"rerank retry {attempt + 1}: {type(exc).__name__}: {exc}")
            time.sleep(2.0 * (attempt + 1))
    raise AssertionError("unreachable")


def _rerank_pools(
    questions: list[str],
    pools: list[list[str]],
    passages_by_doc: dict[str, list[str]],
    *,
    stem: str,
    tag: str,
) -> np.ndarray:
    """Rerank a per-query document pool, max-pooling the scores of each document's children.

    That max is the per-document score `src/servicemind/rag/service.py` keeps, so the value
    fed into the blend here is the value production would feed into it.
    """
    depths = {len(pool) for pool in pools}
    if depths != {RERANK_DEPTH}:
        raise RuntimeError(f"{tag}: pool depths are {sorted(depths)}, expected {RERANK_DEPTH}")
    cache = DATA / f"{stem}.npy"
    identity = DATA / f"{stem}.json"
    ordered = hashlib.sha256(json.dumps(pools, separators=(",", ":")).encode()).hexdigest()
    expected = {
        "dataset_revision": TECHQA_REVISION,
        "reranker_service": RERANK_URL,
        "reranker_window": TEI_RERANKER_WINDOW,
        "pool_depth": RERANK_DEPTH,
        "tag": tag,
        "queries": len(pools),
        "ordered_pools_sha256": ordered,
    }
    if cache.exists() and identity.exists():
        recorded = json.loads(identity.read_text(encoding="utf-8"))
        values = np.load(cache, mmap_mode="r")
        if recorded == expected and values.shape == (len(pools), RERANK_DEPTH):
            log(f"{tag}: rerank cache verified ({cache.name})")
            return np.asarray(values, dtype=np.float32)

    values = np.zeros((len(pools), RERANK_DEPTH), dtype=np.float32)
    started = time.perf_counter()
    with httpx.Client(timeout=600, trust_env=False) as client:
        for row, (question, docs) in enumerate(zip(questions, pools, strict=True)):
            texts: list[str] = []
            spans: list[tuple[int, int]] = []
            for doc_id in docs:
                children = passages_by_doc[doc_id]
                spans.append((len(texts), len(texts) + len(children)))
                texts.extend(children)
            flat = _tei_rerank(client, question, texts)
            for slot, (start, stop) in enumerate(spans):
                values[row, slot] = max(flat[start:stop]) if stop > start else 0.0
            if row % 10 == 0 or row == len(pools) - 1:
                done = row + 1
                elapsed = time.perf_counter() - started
                rate = done / elapsed if elapsed else 0.0
                eta = (len(pools) - done) / rate / 60.0 if rate else 0.0
                log(f"{tag}: {done}/{len(pools)} queries ({rate:.2f} q/s, eta {eta:.1f} min)")
    low, high = float(values.min()), float(values.max())
    if not (low >= 0.0 and high <= 1.0):
        raise RuntimeError(
            f"{tag}: TEI scores fall outside [0, 1] ({low} .. {high}); every blended score "
            "would be mis-scaled"
        )
    np.save(cache, values)
    identity.write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
    log(f"{tag}: rerank cache written ({cache.name})")
    return values


def _fused_scores(*arms: list[str], rank_constant: int) -> dict[str, float]:
    """Production RRF: every arm contributes 1/(rank_constant + rank); a missing arm adds zero."""
    scores: dict[str, float] = {}
    for arm in arms:
        for rank, doc_id in enumerate(arm, 1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (rank_constant + rank)
    return scores


def _top_pool(scores: dict[str, float], depth: int) -> list[str]:
    return [
        doc_id for doc_id, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:depth]
    ]


def _outcomes(
    rows: list[dict[str, Any]],
    pools: list[list[str]],
    rerank: np.ndarray,
    fusion: list[dict[str, float]],
    proxy: Any,
) -> list[Any]:
    """Build production-shaped outcomes: depth-100 pool, 0.85 * rerank + 0.15 * minmax(rrf).

    Only `production_blend` and `relevant` are read back out; the harness's other ranking
    fields exist so its own arms can share one dataclass, and none of them is evaluated
    here. `RANKING_FIELD` names the one this protocol scores.
    """
    built = []
    for row, pool, scores, fused in zip(rows, pools, rerank, fusion, strict=True):
        by_doc = {doc_id: float(scores[slot]) for slot, doc_id in enumerate(pool)}
        ranking = proxy._blend_ranking(
            pool, by_doc, fused, rerank_weight=RERANK_WEIGHT, depth=RERANK_DEPTH
        )
        built.append(
            proxy.Outcome(
                query_id=row["id"],
                relevant=frozenset(context["filename"] for context in row.get("contexts", [])),
                bm25=(),
                dense=(),
                hybrid=(),
                hybrid_reranked=(),
                hybrid_reranked_depth100=(),
                production_blend=ranking,
                search_ms=0.0,
                top_score=max(by_doc.values()),
            )
        )
    return built


def _values(outcomes: list[Any], metric: str, k: int, proxy: Any) -> np.ndarray:
    return np.asarray(proxy._metric_values(outcomes, RANKING_FIELD, metric, k), dtype=np.float64)


def _paired(new: np.ndarray, old: np.ndarray, quantiles: tuple[float, float]) -> dict[str, Any]:
    """Paired bootstrap over queries at the pre-registered seed and quantiles."""
    diff = new - old
    draws = np.random.default_rng(BOOTSTRAP_SEED).integers(
        0, len(diff), size=(BOOTSTRAP_SAMPLES, len(diff))
    )
    low, high = np.quantile(diff[draws].mean(axis=1), quantiles)
    return {
        "delta": round(float(diff.mean()), 4),
        "lcb": round(float(low), 4),
        "ucb": round(float(high), 4),
        "n_queries": int(len(diff)),
    }


def _verdict(gain: dict[str, dict[str, Any]], noninf: dict[str, dict[str, Any]]) -> str:
    """Protocol §7, verbatim.

    FAIL needs an interval that lies *entirely* on the wrong side of its threshold: a
    lower bound merely dipping below -delta proves only that zero effect cannot be ruled
    out, which is INCONCLUSIVE, not proven harm.
    """
    if all(gain[key]["lcb"] > 0 for key in GAIN_LABELS) and all(
        noninf[key]["lcb"] > -DELTA for key in NONINF_LABELS
    ):
        return "PASS"
    if any(gain[key]["ucb"] <= 0 for key in GAIN_LABELS) or any(
        noninf[key]["ucb"] <= -DELTA for key in NONINF_LABELS
    ):
        return "FAIL"
    return "INCONCLUSIVE"


def _markdown(payload: dict[str, Any]) -> str:
    gain = payload["gates"]["gate_one_gain"]
    noninf = payload["gates"]["gate_two_non_inferiority"]
    lines = [
        "# Phase 4 third arm — pre-registered holdout verdict",
        "",
        f"**Verdict: `{payload['verdict']}`**",
        "",
        f"Protocol `{payload['protocol']['path']}` (sha256 `{payload['protocol']['sha256'][:16]}…`), "
        f"frozen {payload['protocol']['frozen_at_utc']}.",
        "",
        f"Holdout: {payload['holdout']['answerable']} answerable + "
        f"{payload['holdout']['impossible']} impossible, previously untouched "
        f"(fingerprint `{payload['holdout']['id_fingerprint'][:16]}…`). "
        f"C1 = C0 + Qwen3-Embedding-0.6B dense arm, δ = {DELTA:.3f}.",
        "",
        "## Gate one — gain (two-sided 95%, lower quantile 0.025)",
        "",
        "| sub-item | C0 | C1 | Δ | LCB | UCB | pass |",
        "| --- | ---: | ---: | ---: | ---: | ---: | :---: |",
    ]
    for key, label in GAIN_LABELS.items():
        item = gain[key]
        lines.append(
            f"| {label} | {item['c0']:.4f} | {item['c1']:.4f} | {item['delta']:+.4f} | "
            f"{item['lcb']:+.4f} | {item['ucb']:+.4f} | {'yes' if item['passed'] else 'NO'} |"
        )
    lines += [
        "",
        f"Hard subset (protocol §4): {payload['hard_subset']['size']} of "
        f"{payload['holdout']['answerable']} answerable queries C0 misses at 10. It is "
        "defined by the incumbent's own failures, so it is a biased target subset and "
        "not independent adoption evidence.",
        "",
        "## Gate two — non-inferiority (one-sided 95%, lower quantile 0.05)",
        "",
        f"Non-inferiority margin δ = {DELTA:.3f}.",
        "",
        "| metric | C0 | C1 | Δ | LCB | UCB | pass |",
        "| --- | ---: | ---: | ---: | ---: | ---: | :---: |",
    ]
    for key, label in NONINF_LABELS.items():
        item = noninf[key]
        lines.append(
            f"| {label} | {item['c0']:.4f} | {item['c1']:.4f} | {item['delta']:+.4f} | "
            f"{item['lcb']:+.4f} | {item['ucb']:+.4f} | {'yes' if item['passed'] else 'NO'} |"
        )
    lines += [
        "",
        "## Anchor check (ran before the holdout was touched)",
        "",
        "| metric | published C0 (dev) | recomputed C0 (dev) |",
        "| --- | ---: | ---: |",
        *[
            f"| {name} | {value:.4f} | {payload['anchors'][name]:.4f} |"
            for name, value in DEV_ANCHOR.items()
        ],
        "",
        "## Scope",
        "",
        "Gates three (cost) and four (generation/abstention) are outside this protocol. "
        "A PASS here buys a controlled shadow run, not production adoption.",
        "",
    ]
    return "\n".join(lines)


def _qwen3_passages(n_passages: int) -> np.ndarray:
    cache = DATA / QWEN3_PASSAGES
    digest = hashlib.sha256()
    with cache.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 22), b""):
            digest.update(block)
    if digest.hexdigest() != QWEN3_PASSAGES_SHA256:
        raise RuntimeError(
            f"{cache.name} does not match the archived embedding matrix; regenerate it from "
            "the pinned Qwen3-Embedding-0.6B revision before running"
        )
    vectors = np.load(cache, mmap_mode="r")
    if vectors.shape != (n_passages, 1024):
        raise RuntimeError(f"{cache.name} has shape {vectors.shape}, expected ({n_passages}, 1024)")
    log(f"qwen3 passage matrix verified: {cache.name} {vectors.shape}")
    return vectors


def _qwen3_queries(questions: list[str], device: str) -> np.ndarray:
    import torch
    from sentence_transformers import SentenceTransformer

    snapshot = (
        ROOT
        / "data/phase4/models/layer2/models--Qwen--Qwen3-Embedding-0.6B/snapshots"
        / QWEN3_SNAPSHOT
    )
    if not snapshot.exists():
        raise RuntimeError(f"pinned Qwen3 snapshot is missing: {snapshot}")
    model = SentenceTransformer(str(snapshot), device=device, trust_remote_code=False)
    model.max_seq_length = 512
    model.eval()
    with torch.inference_mode():
        vectors = model.encode(
            [f"Instruct: {QWEN3_TASK}\nQuery: {question}" for question in questions],
            batch_size=32,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        ).astype(np.float32)
    del model
    torch.cuda.empty_cache()
    return vectors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--check-split-only", action="store_true")
    parser.add_argument("--keep-index", action="store_true")
    args = parser.parse_args()

    record = _verify_protocol()
    proxy = _proxy_module()

    rows = json.loads((DATA / "train.json").read_text(encoding="utf-8"))
    dev_rows, holdout_rows = _split(rows, proxy)
    fingerprint = hashlib.sha256("\n".join(row["id"] for row in holdout_rows).encode()).hexdigest()
    log(f"holdout fingerprint: {fingerprint}")
    if args.check_split_only:
        return

    documents = proxy._corpus_documents()
    known = {doc_id for doc_id, _ in documents}
    missing = {
        context["filename"]
        for row in holdout_rows
        for context in row.get("contexts", [])
        if context["filename"] not in known
    }
    if missing:
        raise RuntimeError(f"holdout qrels do not match the corpus: {sorted(missing)[:10]}")

    document_ids = [doc_id for doc_id, _ in documents]
    document_index = {doc_id: index for index, doc_id in enumerate(document_ids)}
    tokenizer, embedding_session, embedding_revision = proxy._embedding_session()
    passages = proxy._passages(documents, tokenizer)
    passages_by_doc: dict[str, list[str]] = {}
    for passage in passages:
        passages_by_doc.setdefault(passage.doc_id, []).append(passage.text)
    passage_document_indices = np.asarray(
        [document_index[item.doc_id] for item in passages], dtype=np.int32
    )
    log(f"corpus: {len(documents)} documents -> {len(passages)} production-shaped passages")

    scopes = {"dev": dev_rows, "holdout": holdout_rows}
    url, auth = proxy._config()
    index = f"sm-techqa-holdout-{uuid.uuid4().hex[:8]}"
    with httpx.Client(base_url=url, auth=auth, verify=False, timeout=60, trust_env=False) as client:
        proxy._request(
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
            proxy._bulk_index(client, index, passages)
            proxy._request(client, "POST", f"/{index}/_refresh")
            proxy._request(
                client, "PUT", f"/{index}/_settings", json_body={"index.refresh_interval": "1s"}
            )
            bm25: dict[str, list[list[str]]] = {}
            for scope, scope_rows in scopes.items():
                rankings = []
                for row in scope_rows:
                    response = proxy._request(
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
                    rankings.append(proxy._distinct_documents(response["hits"]["hits"]))
                bm25[scope] = rankings
                log(f"{scope}: BM25 top-{RERANK_DEPTH} for {len(rankings)} queries")

            passage_vectors = proxy._passage_embeddings(
                passages, tokenizer, embedding_session, embedding_revision
            )
            qwen3_vectors = _qwen3_passages(len(passages))
            bge_query_vectors = {
                scope: proxy._encode(
                    [row["question"] for row in scope_rows],
                    tokenizer,
                    embedding_session,
                    label=f"bge queries [{scope}]",
                )
                for scope, scope_rows in scopes.items()
            }
            # ONNX Runtime's CUDA arena retains GPU memory; the dense rankings are CPU
            # values now, so release the session before loading the torch Qwen3 model.
            del embedding_session
            gc.collect()
            all_questions = [row["question"] for row in (*dev_rows, *holdout_rows)]
            qwen3_query_vectors = _qwen3_queries(all_questions, args.device)
            split_at = len(dev_rows)
            dense = {
                scope: proxy._top_dense(
                    bge_query_vectors[scope],
                    passage_vectors,
                    document_ids,
                    passage_document_indices,
                    k=RERANK_DEPTH,
                )
                for scope in scopes
            }
            qwen3 = {
                scope: proxy._top_dense(
                    qwen3_query_vectors[slice(*bounds)],
                    qwen3_vectors,
                    document_ids,
                    passage_document_indices,
                    k=RERANK_DEPTH,
                )
                for scope, bounds in (
                    ("dev", (0, split_at)),
                    ("holdout", (split_at, len(all_questions))),
                )
            }
            del bge_query_vectors, qwen3_query_vectors, passage_vectors, qwen3_vectors
            gc.collect()

            rank_constant = proxy.RRF_RANK_CONSTANT

            def c0(scope: str) -> tuple[list[list[str]], list[dict[str, float]]]:
                fusion = [
                    _fused_scores(b, d, rank_constant=rank_constant)
                    for b, d in zip(bm25[scope], dense[scope], strict=True)
                ]
                return [_top_pool(f, RERANK_DEPTH) for f in fusion], fusion

            fusion_c1 = [
                _fused_scores(b, d, q, rank_constant=rank_constant)
                for b, d, q in zip(bm25["holdout"], dense["holdout"], qwen3["holdout"], strict=True)
            ]
            pools_c1 = [_top_pool(f, RERANK_DEPTH) for f in fusion_c1]
            pools_c0: dict[str, list[list[str]]] = {}
            fusions_c0: dict[str, list[dict[str, float]]] = {}
            for scope in scopes:
                pools_c0[scope], fusions_c0[scope] = c0(scope)

            # Anchor check first: identical TEI path, already-analysed queries only.
            dev_rerank = _rerank_pools(
                [row["question"] for row in dev_rows],
                pools_c0["dev"],
                passages_by_doc,
                stem=f"techqa_holdout_c0_dev_pool{RERANK_DEPTH}",
                tag="anchor C0 (dev)",
            )
            dev_outcomes = _outcomes(
                dev_rows, pools_c0["dev"], dev_rerank, fusions_c0["dev"], proxy
            )
            # Retrieval metrics are defined on answerable queries only; the 120 impossible
            # rows have no gold set and would divide by zero.
            dev_answerable_outcomes = dev_outcomes[:DEV_ANSWERABLE]
            if any(not outcome.relevant for outcome in dev_answerable_outcomes):
                raise RuntimeError("the dev slice is not all answerable; the split is misaligned")
            anchors = {
                name: round(
                    statistics.fmean(_values(dev_answerable_outcomes, metric, k, proxy)),
                    4,
                )
                for name, (metric, k) in METRICS.items()
            }
            if anchors != DEV_ANCHOR:
                raise RuntimeError(
                    "C0 does not reproduce the published dev-set numbers, so this pipeline "
                    f"is not the one that produced them; the holdout stays untouched:\n"
                    f"  published:  {DEV_ANCHOR}\n"
                    f"  recomputed: {anchors}"
                )
            log(f"anchor check passed: {anchors}")

            holdout_questions = [row["question"] for row in holdout_rows]
            rerank_c0 = _rerank_pools(
                holdout_questions,
                pools_c0["holdout"],
                passages_by_doc,
                stem=f"techqa_holdout_c0_pool{RERANK_DEPTH}",
                tag="C0 (holdout)",
            )
            rerank_c1 = _rerank_pools(
                holdout_questions,
                pools_c1,
                passages_by_doc,
                stem=f"techqa_holdout_c1_pool{RERANK_DEPTH}",
                tag="C1 (holdout)",
            )
        finally:
            if not args.keep_index:
                client.delete(f"/{index}")

    n_answerable = len([row for row in holdout_rows if not row["is_impossible"]])
    c0_outcomes = _outcomes(
        holdout_rows, pools_c0["holdout"], rerank_c0, fusions_c0["holdout"], proxy
    )
    c1_outcomes = _outcomes(holdout_rows, pools_c1, rerank_c1, fusion_c1, proxy)
    if any(not outcome.relevant for outcome in c0_outcomes[:n_answerable]) or any(
        not outcome.relevant for outcome in c1_outcomes[:n_answerable]
    ):
        raise RuntimeError("the holdout answerable slice is not all answerable; split misaligned")

    # Dump the rankings and pools. Without them a reader cannot re-derive a single number
    # below without rebuilding the index, which would make this report unauditable.
    (DATA / f"{REPORT_STEM}_rankings.json").write_text(
        json.dumps(
            {
                "query_ids": [row["id"] for row in holdout_rows],
                "answerable": n_answerable,
                "relevant": [sorted(outcome.relevant) for outcome in c0_outcomes],
                "c0_pools": pools_c0["holdout"],
                "c1_pools": pools_c1,
                "c0_rankings": [list(getattr(o, RANKING_FIELD)) for o in c0_outcomes],
                "c1_rankings": [list(getattr(o, RANKING_FIELD)) for o in c1_outcomes],
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )

    # Hard subset (protocol §4): holdout answerable queries C0's fused arm misses at 10.
    hard = [
        position
        for position, outcome in enumerate(c0_outcomes[:n_answerable])
        if not (outcome.relevant & set(getattr(outcome, RANKING_FIELD)[:10]))
    ]
    log(f"hard subset: {len(hard)}/{n_answerable} answerable queries C0 misses at 10")

    gain: dict[str, dict[str, Any]] = {}
    for key, indices in (
        ("recall_at_5_overall", list(range(n_answerable))),
        ("recall_at_5_hard", hard),
    ):
        new = _values([c1_outcomes[i] for i in indices], "recall", 5, proxy)
        old = _values([c0_outcomes[i] for i in indices], "recall", 5, proxy)
        item = _paired(new, old, QUANTILES_GAIN)
        item["c0"] = round(float(old.mean()), 4)
        item["c1"] = round(float(new.mean()), 4)
        item["passed"] = bool(item["lcb"] > 0)
        gain[key] = item

    noninf: dict[str, dict[str, Any]] = {}
    for name in NONINF_LABELS:
        metric, k = METRICS[name]
        new = _values(c1_outcomes[:n_answerable], metric, k, proxy)
        old = _values(c0_outcomes[:n_answerable], metric, k, proxy)
        item = _paired(new, old, QUANTILES_NONINF)
        item["c0"] = round(float(old.mean()), 4)
        item["c1"] = round(float(new.mean()), 4)
        item["passed"] = bool(item["lcb"] > -DELTA)
        noninf[name] = item

    verdict = _verdict(gain, noninf)
    log(f"VERDICT: {verdict}")

    payload = {
        "schema_version": "phase4-holdout-prereg-2026-09-15",
        "verdict": verdict,
        "protocol": {
            "path": str(PROTOCOL.relative_to(ROOT)),
            "sha256": record["source"]["sha256"],
            "frozen_at_utc": record["frozen_at_utc"],
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
        "dataset": {"source": "nvidia/TechQA-RAG-Eval", "revision": TECHQA_REVISION},
        "holdout": {
            "answerable": n_answerable,
            "impossible": len(holdout_rows) - n_answerable,
            "id_fingerprint": fingerprint,
            "consumed": True,
        },
        "configurations": {
            "C0": "BM25 + BGE-M3 dense, equal-weight RRF (rank_constant=60), depth-100",
            "C1": "C0 + Qwen3-Embedding-0.6B dense arm, equal-weight three-way RRF",
            "rerank_weight": RERANK_WEIGHT,
        },
        "statistics": {
            "bootstrap_samples": BOOTSTRAP_SAMPLES,
            "bootstrap_seed": BOOTSTRAP_SEED,
            "quantiles_gain_two_sided_95": list(QUANTILES_GAIN),
            "quantiles_non_inferiority_one_sided_95": list(QUANTILES_NONINF),
            "delta": DELTA,
            "unit": "query (paired)",
        },
        "hard_subset": {
            "definition": "C0 fused top-10 miss on holdout answerable",
            "size": len(hard),
        },
        "anchors": anchors,
        "gates": {"gate_one_gain": gain, "gate_two_non_inferiority": noninf},
        "hard_set_scope_note": (
            "The hard subset is defined by the incumbent system's own failures, so it is a "
            "biased target subset and not independent adoption evidence (protocol §4)."
        ),
        "status_semantics": (
            "PASS buys a controlled shadow run, not production adoption. Gates three and four "
            "are outside this protocol."
        ),
    }
    (REPORTS / f"{REPORT_STEM}.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (REPORTS / f"{REPORT_STEM}.md").write_text(_markdown(payload), encoding="utf-8")
    log(f"wrote {REPORTS / REPORT_STEM}.json and .md")


if __name__ == "__main__":
    main()
