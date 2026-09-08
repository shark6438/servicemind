# Multi-query fan-out A/B — bge

Gold: servicemind-phase4-gold-v1 (answerable=12, unanswerable=3). Rewrites: deepseek-v4-flash captured 2026-09-07T19:25:58+00:00.

| arm | Recall@k | MRR@10 | NDCG@10 | P@1 | candidates | pool parents | latency(ms) |
|---|---|---|---|---|---|---|---|
| single_query | {'1': 0.8333, '5': 1.0, '10': 1.0} | 1.0 | 0.9933 | 1.0 | 30.0 | 30.0 | 2130.6 |
| fan_out | {'1': 0.8333, '5': 1.0, '10': 1.0} | 1.0 | 0.9933 | 1.0 | 30.0 | 30.0 | 1884.7 |

Final-context Recall/MRR is saturated at the ceiling on the committed gold (MRR@10 == 1.0 in every baseline); equality on this surface is expected, not evidence of no benefit. The measurable, non-saturated surfaces are the candidate-pool breadth the reranker sees and the latency/candidate cost fan-out adds.

**Decision: keep** — fan_out never regressed final-context MRR/NDCG/P@1 (regression=False) and abstention is unchanged (answered_unanswerable 3 -> 3). Re-measure against a larger, harder gold set (or the real PagerDuty/Mendeley corpus) where single-query retrieval misses relevant parents.

