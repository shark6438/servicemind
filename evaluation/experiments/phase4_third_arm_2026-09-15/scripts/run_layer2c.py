"""Layer-2 decision: does adding the third dense arm clear the §4.1 adoption rule?

`docs/PHASE4_EVALUATION_BASELINE_V1_2.md` §4.1: "新增检索臂只有在困难子集 NDCG@10
绝对提升至少 2 个百分点，且 paired-bootstrap 95% CI 下界大于 0 时才保留；否则选择
更简单、延迟更低的配置。"

So this script does not report a pretty overall delta and stop there. It:
  1. reranks the three-way pool with the *production* TEI reranker (same one the
     two-way baseline used), so the only variable is the candidate pool;
  2. scores four configurations end to end (2-way RRF / 2-way + rerank / 3-way RRF /
     3-way + rerank);
  3. defines the hard subset arm-independently, as the queries where the shipped
     production configuration fails to place gold in the top 10;
  4. runs a paired bootstrap over queries for both the overall and hard-subset
     NDCG@10 deltas and prints the verdict of the §4.1 rule.
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

_spec_p = importlib.util.spec_from_file_location(
    "proxy_eval", ROOT / "scripts" / "evaluate_phase4_proxy_release.py"
)
P = importlib.util.module_from_spec(_spec_p)
sys.modules["proxy_eval"] = P
_spec_p.loader.exec_module(P)

_spec_a = importlib.util.spec_from_file_location("layer1", OUT / "run_layer1.py")
L1 = importlib.util.module_from_spec(_spec_a)
sys.modules["layer1"] = L1
_spec_a.loader.exec_module(L1)

CONFIGS = ("rrf_2way", "rerank_2way", "rrf_3way", "rerank_3way")
HARD_SUBSET_RULE = "production configuration (2-way RRF top-100 + rerank) misses gold in top-10"
NDCG_GAIN_REQUIRED = 0.02
BOOTSTRAP_SAMPLES = 10_000
BOOTSTRAP_SEED = 42


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def per_query_ndcg(outcomes, k: int = 10) -> np.ndarray:
    return np.asarray(P._metric_values(outcomes, "rank", "ndcg", k), dtype=np.float64)


def paired_bootstrap(
    left: np.ndarray, right: np.ndarray, *, samples: int = BOOTSTRAP_SAMPLES, seed: int = BOOTSTRAP_SEED
) -> dict[str, float]:
    """Paired bootstrap over queries: is mean(right - left) above zero?"""
    assert left.shape == right.shape
    difference = right - left
    generator = np.random.default_rng(seed)
    draws = generator.integers(0, len(difference), size=(samples, len(difference)))
    means = difference[draws].mean(axis=1)
    low, high = np.quantile(means, [0.025, 0.975])
    return {
        "delta": round(float(difference.mean()), 4),
        "ci95_low": round(float(low), 4),
        "ci95_high": round(float(high), 4),
        "ci95_lower_above_zero": bool(low > 0.0),
        "queries": int(len(difference)),
    }


def main() -> None:
    funnel = json.loads((OUT / "funnel_v1.json").read_text())
    pool_2way = funnel["rrf"]
    pool_3way = json.loads((OUT / "rrf3_top100.json").read_text())
    relevant = [frozenset(x) for x in funnel["relevant"]]
    answerable = [i for i, rel in enumerate(relevant) if rel]
    log(f"answerable queries: {len(answerable)}")

    tei_2way = np.load(OUT / "rerank_tei_v1.npy")
    cache = OUT / "rerank_tei_3way.npy"
    if cache.exists() and json.loads((OUT / "rerank_tei_3way.json").read_text())["docs"] == pool_3way:
        tei_3way = np.load(cache)
        log("3-way rerank cache verified")
    else:
        log("reranking the 3-way pool with the production TEI reranker")
        import httpx

        mapping = L1.passages_by_doc()
        matrix = np.zeros((len(pool_3way), len(pool_3way[0])), dtype=np.float32)
        tick = time.perf_counter()
        with httpx.Client(timeout=600, trust_env=False) as client:
            for i, docs in enumerate(pool_3way):
                texts: list[str] = []
                spans: list[tuple[int, int]] = []
                for doc_id in docs:
                    children = mapping[doc_id]
                    spans.append((len(texts), len(texts) + len(children)))
                    texts.extend(children)
                flat = L1.tei_rerank(client, funnel["questions"][i], texts)
                for pos, (start, stop) in enumerate(spans):
                    matrix[i, pos] = max(flat[start:stop]) if stop > start else 0.0
                if i % 25 == 0 or i == len(pool_3way) - 1:
                    done = i + 1
                    rate = done / (time.perf_counter() - tick)
                    log(f"reranked {done}/{len(pool_3way)} q ({rate:.2f} q/s, eta {(len(pool_3way)-done)/rate/60:.1f} min)")
                    np.save(cache, matrix)
        np.save(cache, matrix)
        (OUT / "rerank_tei_3way.json").write_text(json.dumps({"docs": pool_3way}) + "\n")
        tei_3way = matrix
        log("3-way rerank cached")

    def order(docs, row):
        return tuple(docs[j] for j in sorted(range(len(docs)), key=lambda j: (-float(row[j]), docs[j])))

    outcomes = {name: [] for name in CONFIGS}
    for i in answerable:
        outcomes["rrf_2way"].append(SimpleNamespace(relevant=relevant[i], rank=tuple(pool_2way[i])))
        outcomes["rerank_2way"].append(SimpleNamespace(relevant=relevant[i], rank=order(pool_2way[i], tei_2way[i])))
        outcomes["rrf_3way"].append(SimpleNamespace(relevant=relevant[i], rank=tuple(pool_3way[i])))
        outcomes["rerank_3way"].append(SimpleNamespace(relevant=relevant[i], rank=order(pool_3way[i], tei_3way[i])))

    summary = {}
    for name in CONFIGS:
        row = {}
        for metric, k in (("recall", 10), ("mrr", 10), ("ndcg", 10), ("ndcg", 5)):
            values = P._metric_values(outcomes[name], "rank", metric, k)
            row[f"{'ndcg' if metric == 'ndcg' else metric}_at_{k}"] = round(statistics.fmean(values), 4)
        summary[name] = row
    log(json.dumps(summary, indent=2))

    # Hard subset: where the shipped production configuration fails. Defined from the
    # baseline alone, so it cannot be tuned to flatter the new arm.
    baseline_ndcg = per_query_ndcg(outcomes["rerank_2way"])
    hard = np.asarray([i for pos, i in enumerate(answerable) if baseline_ndcg[pos] < 1.0])
    log(f"hard subset (baseline misses gold in top-10): {len(hard)}/{len(answerable)} queries")

    def subset(values: np.ndarray, indices: np.ndarray) -> np.ndarray:
        positions = {query: pos for pos, query in enumerate(answerable)}
        return values[[positions[int(q)] for q in indices]]

    report = {
        "schema": "servicemind-layer2-arm-decision-v1",
        "note": "offline layer-2 experiment; not a repo release report",
        "configurations": summary,
        "hard_subset": {"rule": HARD_SUBSET_RULE, "queries": int(len(hard)), "of": len(answerable)},
        "decision_rule": (
            f"docs/PHASE4_EVALUATION_BASELINE_V1_2.md §4.1: keep the new arm only if hard-subset "
            f"NDCG@10 gains at least {NDCG_GAIN_REQUIRED:.2f} and the paired-bootstrap 95% CI "
            "lower bound is above zero"
        ),
    }

    for arm in ("rrf_3way", "rerank_3way"):
        for label, indices in (("overall", np.asarray(answerable)), ("hard_subset", hard)):
            left = subset(per_query_ndcg(outcomes["rerank_2way" if arm == "rerank_3way" else "rrf_2way"]), indices)
            right = subset(per_query_ndcg(outcomes[arm]), indices)
            report.setdefault("paired_bootstrap", {}).setdefault(arm, {})[label] = paired_bootstrap(left, right)
    # Also isolate the pool change alone (RRF 3-way vs RRF 2-way) and the reranker alone.
    report["paired_bootstrap"].setdefault("rerank_3way_vs_rerank_2way", {})
    for label, indices in (("overall", np.asarray(answerable)), ("hard_subset", hard)):
        left = subset(per_query_ndcg(outcomes["rerank_2way"]), indices)
        right = subset(per_query_ndcg(outcomes["rerank_3way"]), indices)
        report["paired_bootstrap"]["rerank_3way_vs_rerank_2way"][label] = paired_bootstrap(left, right)

    hard_check = report["paired_bootstrap"]["rerank_3way_vs_rerank_2way"]["hard_subset"]
    report["verdict"] = {
        "arm": "qwen3-embedding-0.6b dense (third retrieval arm)",
        "hard_subset_ndcg_at_10_gain": hard_check["delta"],
        "required_gain": NDCG_GAIN_REQUIRED,
        "ci95_low": hard_check["ci95_low"],
        "adopt": bool(
            hard_check["delta"] >= NDCG_GAIN_REQUIRED and hard_check["ci95_lower_above_zero"]
        ),
    }
    log(json.dumps(report["verdict"], indent=2))
    (OUT / "layer2_decision.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
