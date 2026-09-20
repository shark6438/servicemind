"""Power analysis for the pre-registered adoption gate (docs/PHASE4_..._PREREGISTRATION).

Runs on the ALREADY-ANALYSED 280-query set only. This is a planning calculation, not a
confirmation -- the untouched holdout (330 answerable) is never touched here.

Two things the pre-registration draft got wrong and this script fixes:

  1. It used the Recall@5 standard error to stand in for all three non-inferiority
     metrics. The three paired-difference vectors have very different spreads, so each
     metric gets its own SE.
  2. It quoted a single-metric pass probability as if it were the protocol's power.
     Gate two requires all THREE metrics to pass, which is a joint event. Joint power is
     estimated by simulating the three correlated difference vectors.

The joint simulation resamples the three per-query difference vectors TOGETHER (one
shared index draw per replicate), which preserves the cross-metric correlation that a
product of marginal powers would destroy. Under H0 the true effect is taken as zero:
no gain and no harm.

Method note: power is reported for a one-sided 95% non-inferiority bound (z = 1.645),
i.e. the lower 0.05 bootstrap quantile -- NOT the lower bound of a two-sided 95%
interval, which is a 97.5% one-sided bound and would not match this z.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from scipy.stats import norm

ROOT = Path("/home/shihongye/data1/servicemind")
OUT = Path("/tmp/exp_layer1")
SAMPLES, SEED = 10_000, 42
N_DEV, N_HOLDOUT = 280, 330
DELTA = 0.020
Z_ONE_SIDED_95 = 1.6448536269514722
SIM_REPLICATES = 200_000

_spec = importlib.util.spec_from_file_location("layer2d", OUT / "run_layer2d.py")
L2D = importlib.util.module_from_spec(_spec)
sys.modules["layer2d"] = L2D
_spec.loader.exec_module(L2D)
P = L2D.P

METRICS = (("Recall@5", "recall", 5), ("Recall@10", "recall", 10), ("MRR@10", "mrr", 10),
           ("NDCG@10", "ndcg", 10))
GATE_TWO = ("Recall@10", "MRR@10", "NDCG@10")


def log(message):
    print("[power] %s" % message, flush=True)


funnel = json.loads((OUT / "funnel_v1.json").read_text())
qwen3 = json.loads((OUT / "qwen3_dense_top100.json").read_text())
true3 = json.loads((OUT / "rrf3_true_top100.json").read_text())
relevant = [frozenset(x) for x in funnel["relevant"]]
answerable = [i for i, rel in enumerate(relevant) if rel]
assert len(answerable) == N_DEV, len(answerable)
pool_2way = funnel["rrf"]
rerank_2way = np.load(OUT / "rerank_tei_v1.npy")
rerank_true3 = np.load(OUT / "rerank_tei_true3.npy")

outcomes = {"blend_2way": [], "blend_true3": []}
for i in answerable:
    rrf2 = L2D.fused_scores(funnel["bm25"][i], funnel["dense"][i])
    rrf3 = L2D.fused_scores(funnel["bm25"][i], funnel["dense"][i], qwen3[i])
    for name, docs, scores in (
        ("blend_2way", pool_2way[i], {"rerank": {d: float(rerank_2way[i][j])
                                                 for j, d in enumerate(pool_2way[i])}, "rrf": rrf2}),
        ("blend_true3", true3[i], {"rerank": {d: float(rerank_true3[i][j])
                                              for j, d in enumerate(true3[i])}, "rrf": rrf3}),
    ):
        outcomes[name].append(SimpleNamespace(relevant=relevant[i],
                                              rank=L2D.order(docs, scores, "blend")))

anchor = {label: round(float(np.mean(P._metric_values(outcomes["blend_2way"], "rank", m, k))), 4)
          for label, m, k in METRICS}
log("C0 anchors on the dev set: %s" % anchor)
assert anchor["Recall@10"] == 0.7643 and anchor["NDCG@10"] == 0.6279, anchor

diffs, report = {}, {}
for label, metric, k in METRICS:
    new = np.asarray(P._metric_values(outcomes["blend_true3"], "rank", metric, k))
    old = np.asarray(P._metric_values(outcomes["blend_2way"], "rank", metric, k))
    diff = new - old
    diffs[label] = diff
    draws = np.random.default_rng(SEED).integers(0, len(diff), size=(SAMPLES, len(diff)))
    means = diff[draws].mean(axis=1)
    low_two_sided, high_two_sided = np.quantile(means, [0.025, 0.975])
    low_one_sided = float(np.quantile(means, 0.05))
    se = float(means.std(ddof=1))
    se_holdout = se * np.sqrt(N_DEV / N_HOLDOUT)
    # Power under H0 (true difference = 0): P(delta_hat - z*SE > -DELTA).
    threshold = -DELTA + Z_ONE_SIDED_95 * se_holdout
    power = float(norm.cdf((DELTA / se_holdout) - Z_ONE_SIDED_95))
    report[label] = {
        "delta_on_dev": round(float(diff.mean()), 4),
        "ci95_two_sided": [round(float(low_two_sided), 4), round(float(high_two_sided), 4)],
        "ci_95_one_sided_lower": round(low_one_sided, 4),
        "se_dev": round(se, 6),
        "se_holdout_n330": round(se_holdout, 6),
        "single_metric_power_at_delta_0.020": round(power, 4),
        "delta_hat_must_exceed_under_h0": round(threshold, 6),
    }
    log("%-10s SE_dev=%.5f SE_holdout=%.5f  single-metric power=%.3f"
        % (label, se, se_holdout, power))

# Joint power for gate two: resample the three difference vectors together under H0.
stack = np.vstack([diffs[label] - diffs[label].mean() for label in GATE_TWO])
se_hold = np.array([report[label]["se_holdout_n330"] for label in GATE_TWO])
rng = np.random.default_rng(SEED + 1)
idx = rng.integers(0, N_DEV, size=(SIM_REPLICATES, N_DEV))
means = np.stack([stack[j][idx].mean(axis=1) for j in range(len(GATE_TWO))], axis=1)
# Resampling a 280-query vector reproduces the dev-set SE; rescale to the holdout size.
means = means * np.sqrt(N_DEV / N_HOLDOUT)
passed = np.all(means > (-DELTA + Z_ONE_SIDED_95 * se_hold), axis=1)
joint_power = float(passed.mean())
marginal_product = float(np.prod([report[m]["single_metric_power_at_delta_0.020"]
                                  for m in GATE_TWO]))
corr = np.corrcoef(stack)
report["_joint"] = {
    "gate_two_metrics": list(GATE_TWO),
    "joint_power_under_h0": round(joint_power, 4),
    "product_of_marginal_powers": round(marginal_product, 4),
    "residual_correlation_matrix": [[round(float(c), 3) for c in row] for row in corr],
    "simulation_replicates": SIM_REPLICATES,
    "delta": DELTA,
    "z": round(Z_ONE_SIDED_95, 4),
    "note": ("H0 = true paired difference is exactly zero for all three metrics; "
             "joint power is the share of simulated runs where all three one-sided "
             "95% lower bounds exceed -delta"),
}
log("joint power (gate two, all three metrics) = %.4f  [product of marginals = %.4f]"
    % (joint_power, marginal_product))
log("residual correlations:\n%s" % np.round(corr, 3))

(OUT / "prereg_power.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
log("wrote %s" % (OUT / "prereg_power.json"))
