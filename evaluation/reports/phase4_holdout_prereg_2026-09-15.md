# Phase 4 third arm — pre-registered holdout verdict

**Verdict: `FAIL`**

Protocol `docs/PHASE4_THIRD_ARM_PREREGISTRATION_2026-09-15.md` (sha256 `038d74114cbac0a9…`), frozen 2026-09-15T16:01:21Z.

Holdout: 330 answerable + 180 impossible, previously untouched (fingerprint `7d59475988f54fb3…`). C1 = C0 + Qwen3-Embedding-0.6B dense arm, δ = 0.020.

## Gate one — gain (two-sided 95%, lower quantile 0.025)

| sub-item | C0 | C1 | Δ | LCB | UCB | pass |
| --- | ---: | ---: | ---: | ---: | ---: | :---: |
| Recall@5, all answerable | 0.7364 | 0.7364 | +0.0000 | -0.0091 | +0.0091 | NO |
| Recall@5, hard subset | 0.0000 | 0.0000 | +0.0000 | +0.0000 | +0.0000 | NO |

Hard subset (protocol §4): 71 of 330 answerable queries C0 misses at 10. It is defined by the incumbent's own failures, so it is a biased target subset and not independent adoption evidence.

## Gate two — non-inferiority (one-sided 95%, lower quantile 0.05)

Non-inferiority margin δ = 0.020.

| metric | C0 | C1 | Δ | LCB | UCB | pass |
| --- | ---: | ---: | ---: | ---: | ---: | :---: |
| Recall@10 | 0.7848 | 0.8000 | +0.0152 | +0.0030 | +0.0303 | yes |
| MRR@10 | 0.6105 | 0.6240 | +0.0135 | +0.0030 | +0.0243 | yes |
| NDCG@10 | 0.6529 | 0.6665 | +0.0136 | +0.0048 | +0.0226 | yes |

## Anchor check (ran before the holdout was touched)

| metric | published C0 (dev) | recomputed C0 (dev) |
| --- | ---: | ---: |
| recall_at_5 | 0.6857 | 0.6857 |
| recall_at_10 | 0.7643 | 0.7643 |
| mrr_at_10 | 0.5848 | 0.5848 |
| ndcg_at_10 | 0.6279 | 0.6279 |

## Scope

Gates three (cost) and four (generation/abstention) are outside this protocol. A PASS here buys a controlled shadow run, not production adoption.
