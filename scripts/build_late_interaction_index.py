#!/usr/bin/env python
"""Build the late-interaction shadow index, or size it before building anything.

The encoder is not importable here: the repo virtualenv pins ``torch 2.13.0+cu130``
against a 535/CUDA 12.2 driver, so it cannot see the cards. This script is the
client; ``scripts/serve_phase4_late_interaction.py`` runs the encoder under an
interpreter that can. Same shape as the TEI providers in ``rag.models``.

``--dry-run`` is the R2.0 spike and exists so that the cost question is answered
*before* a 300 GB index is written. It reports, from a real sample:

* the document token histogram, and the total tokens and bytes extrapolated to
  the frozen corpus size;
* encoding throughput, hence how long a full build would take;
* a MaxSim scan benchmark at two real sizes, so the extrapolation to full scale
  rests on a measured scaling rather than an assumption.

The scan is the part that decides the arm. A full MaxSim pass reads every token
vector once; if that cannot fit the latency budget at this corpus size, the arm
is eliminated here rather than after a day of building.

Exit codes
----------
``0``
    Dry run fits the budget, or an index was built.
``1``
    The dry run shows the full-scale scan cannot fit the budget. The arm as
    specified is not viable; this is a verdict, not an error.
``2``
    Nothing to measure: the frozen corpus is not on disk.
``3``
    Configuration: the encoder service is unreachable, or ``--limit`` is not a
    positive count. Never falls back to CPU -- measuring cost on hardware
    production does not use would make the number meaningless.
"""

from __future__ import annotations

import argparse
import io
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from servicemind.rag.late_interaction import (  # noqa: E402
    LateInteractionIndex,
    write_index,
)

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "phase4" / "raw" / "eval" / "lotte-hf"
MANIFEST_PATH = ROOT / "data" / "phase4" / "manifests" / "sources.v1.3.json"
REPORTS = ROOT / "evaluation" / "reports"
DEFAULT_SERVICE = "http://127.0.0.1:8087"

#: The latency the plan allows this arm to add: p95 of the incumbent C0-MQ arm is
#: 1020.7 ms and the cost gate forbids more than a 20% increase.
COST_BUDGET_MS = 204.0

#: ``evaluation/reports/phase4_query_arm_funnel_latest.json``, arm ``c0_mq``.
BASELINE_P95_MS = 1020.7


def corpus_path(dataset: str, split: str) -> Path:
    if dataset != "lotte-technology":
        raise SystemExit(f"unsupported dataset {dataset!r}; only lotte-technology is frozen")
    if split == "dev":
        return RAW_DIR / "technology" / "dev_collection.jsonl"
    if split == "test":
        return RAW_DIR / "technology" / "test" / "collection.tsv"
    raise SystemExit(f"unsupported split {split!r}")


def read_passages(path: Path, limit: int | None) -> tuple[list[str], list[str]]:
    """Return ``(keys, texts)``. The key is the corpus' own passage id."""
    keys: list[str] = []
    texts: list[str] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            if path.suffix == ".jsonl":
                record = json.loads(line)
                key, text = str(record["doc_id"]), record.get("text") or ""
            else:
                key, text = line.rstrip("\n").split("\t", 1)
            keys.append(key)
            texts.append(text)
            if limit is not None and len(keys) >= limit:
                break
    return keys, texts


def corpus_document_count() -> int | None:
    if not MANIFEST_PATH.is_file():
        return None
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for source in manifest.get("evaluation_sources", []):
        if source.get("id") == "lotte-technology":
            return int(source["dev_passages"])
    return None


def encode(client: httpx.Client, url: str, texts: list[str], *, kind: str, batch_size: int = 16):
    response = client.post(
        f"{url}/encode", json={"texts": texts, "kind": kind, "batch_size": batch_size}
    )
    response.raise_for_status()
    payload = np.load(io.BytesIO(response.content))
    vectors, offsets = payload["vectors"], payload["offsets"]
    return vectors, offsets, float(response.headers.get("X-Encode-Seconds", "nan"))


def health(client: httpx.Client, url: str) -> dict[str, Any]:
    response = client.get(f"{url}/health")
    response.raise_for_status()
    return response.json()


def _histogram(lengths: list[int]) -> dict[str, float]:
    array = np.asarray(lengths, dtype=np.int64)
    return {
        "min": int(array.min()),
        "p50": int(np.percentile(array, 50)),
        "p90": int(np.percentile(array, 90)),
        "p99": int(np.percentile(array, 99)),
        "max": int(array.max()),
        "mean": float(array.mean()),
    }


def _time_search(index: LateInteractionIndex, query: np.ndarray, repeats: int = 5) -> dict:
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        index.search(query, top_k=100)
        samples.append((time.perf_counter() - started) * 1000)
    return {
        "p50_ms": float(statistics.median(samples)),
        "min_ms": float(min(samples)),
        "max_ms": float(max(samples)),
    }


def _prefix_view(flat: np.ndarray, offsets: np.ndarray, keys: list[str], documents: int):
    """A genuine smaller index: an exact prefix of the same arrays."""
    return flat[: int(offsets[documents])], offsets[: documents + 1], keys[:documents]


def run_dry_run(client: httpx.Client, url: str, args: argparse.Namespace) -> int:
    path = corpus_path(args.dataset, args.split)
    if not path.is_file():
        print(f"frozen corpus missing: {path.relative_to(ROOT)}", file=sys.stderr)
        return 2
    runtime = health(client, url)
    total_documents = corpus_document_count()
    if total_documents is None:
        print("sources.v1.3.json has no lotte-technology entry; run the R1 freeze first")
        return 2

    keys, texts = read_passages(path, args.limit)
    print(f"sampled {len(keys)} of {total_documents} passages from {path.name}")

    started = time.perf_counter()
    vectors, offsets, _ = encode(client, url, texts, kind="document", batch_size=args.batch_size)
    elapsed = time.perf_counter() - started
    lengths = np.diff(offsets).tolist()
    sampled_tokens = int(offsets[-1])
    print(
        f"encoded {len(keys)} documents / {sampled_tokens} tokens in {elapsed:.1f}s "
        f"({sampled_tokens / elapsed:.0f} tokens/s)"
    )
    histogram = _histogram(lengths)
    mean_tokens = float(np.mean(lengths))
    extrapolated_tokens = mean_tokens * total_documents
    bytes_per_token = 2 * runtime["dimension"]  # float16
    extrapolated_bytes = extrapolated_tokens * bytes_per_token
    build_hours = (
        extrapolated_tokens / (sampled_tokens / elapsed) / 3600 if elapsed else float("inf")
    )

    index_dir = REPORTS.parent / "li-index" / "dryrun"
    header = write_index(
        index_dir,
        keys=keys,
        vectors=vectors,
        offsets=offsets,
        model_name=runtime["model"],
        model_revision=runtime["revision"],
        dimension=runtime["dimension"],
        max_doc_tokens=runtime["max_doc_tokens"],
    )
    index = LateInteractionIndex(index_dir)

    query_text = _first_query(args.split)
    query_vectors, _, query_seconds = encode(client, url, [query_text], kind="query")
    query = np.asarray(query_vectors, dtype=np.float32)

    full_scan = _time_search(index, query)
    half = max(1, len(keys) // 2)
    half_flat, half_offsets, half_keys = _prefix_view(
        index.vectors, index.offsets, index.keys, half
    )
    from servicemind.rag.late_interaction import search as li_search

    half_samples = []
    for _ in range(5):
        started = time.perf_counter()
        li_search(query, half_flat, half_offsets, half_keys, top_k=100)
        half_samples.append((time.perf_counter() - started) * 1000)

    sample_tokens = int(offsets[-1])
    tokens_per_ms = sample_tokens / max(full_scan["p50_ms"], 1e-9)
    projected_scan_ms = extrapolated_tokens / tokens_per_ms
    query_ms = query_seconds * 1000
    added_ms = projected_scan_ms + query_ms
    verdict = "PASS" if added_ms <= COST_BUDGET_MS else "FAIL"

    print()
    print(f"runtime          {runtime['device']}  torch {runtime['torch']} cuda {runtime['cuda']}")
    print(f"index (sample)   {index.size_bytes / 1e6:.1f} MB, {header.total_tokens} tokens")
    print(f"tokens/document  {json.dumps(histogram)}")
    print(
        f"extrapolated     {extrapolated_tokens / 1e9:.2f} G tokens = "
        f"{extrapolated_bytes / 1e9:.1f} GB at {bytes_per_token} B/token"
    )
    print(f"full build       {build_hours:.1f} h at the measured encode rate")
    print(
        f"scan @sample     p50 {full_scan['p50_ms']:.1f} ms  "
        f"(@{half} docs p50 {statistics.median(half_samples):.1f} ms)"
    )
    print(f"scan @full       {projected_scan_ms:.0f} ms projected ({tokens_per_ms:.2e} tokens/ms)")
    print(f"query encode     {query_ms:.1f} ms")
    print(
        f"added p95        {added_ms:.0f} ms vs budget {COST_BUDGET_MS:.0f} ms "
        f"(baseline p95 {BASELINE_P95_MS} ms)"
    )
    print(f"VERDICT          {verdict}")

    sidecar = {
        "dataset": args.dataset,
        "split": args.split,
        "sample_documents": len(keys),
        "corpus_documents": total_documents,
        "sampled_tokens": sampled_tokens,
        "token_histogram": histogram,
        "extrapolated_tokens": extrapolated_tokens,
        "extrapolated_bytes": extrapolated_bytes,
        "bytes_per_token": bytes_per_token,
        "encode_tokens_per_second": sampled_tokens / elapsed if elapsed else None,
        "projected_build_hours": build_hours,
        "scan_sample_p50_ms": full_scan["p50_ms"],
        "scan_half_p50_ms": float(statistics.median(half_samples)),
        "projected_scan_ms": projected_scan_ms,
        "query_encode_ms": query_ms,
        "projected_added_ms": added_ms,
        "cost_budget_ms": COST_BUDGET_MS,
        "baseline_p95_ms": BASELINE_P95_MS,
        "verdict": verdict,
        "runtime": runtime,
        "index_generation_fingerprint": header.generation_fingerprint,
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / f"phase4_li_dryrun_{args.dataset}_{args.split}_latest.json"
    out.write_text(json.dumps(sidecar, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out.relative_to(ROOT)}")
    return 0 if verdict == "PASS" else 1


def _first_query(split: str) -> str:
    questions = RAW_DIR / "technology" / split / "questions.search.tsv"
    with questions.open(encoding="utf-8") as handle:
        for line in handle:
            parts = line.rstrip("\n").split("\t", 1)
            if len(parts) == 2 and parts[1].strip():
                return parts[1].strip()
    raise SystemExit(f"no usable query in {questions}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", default="lotte-technology")
    parser.add_argument("--split", default="dev", choices=("dev", "test"))
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--service-url",
        default=DEFAULT_SERVICE,
        help="the encoder process; see scripts/serve_phase4_late_interaction.py",
    )
    args = parser.parse_args(argv)
    if args.limit <= 0:
        print("--limit must be positive", file=sys.stderr)
        return 3
    try:
        with httpx.Client(timeout=1800) as client:
            if args.dry_run:
                return run_dry_run(client, args.service_url, args)
            print("only --dry-run is implemented so far", file=sys.stderr)
            return 3
    except httpx.HTTPError as error:
        print(
            f"encoder service at {args.service_url} is unusable: {error}\n"
            "start it with the interpreter that can see the GPU:\n"
            "  PYTHONPATH=src /data/shihongye/data/miniconda3/bin/python "
            "scripts/serve_phase4_late_interaction.py --port 8087",
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
