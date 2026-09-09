# Phase 4 retrieval evaluation — servicemind-phase4-gold-v1

Top-k cutoffs: 5, 10, 20.

| baseline | Recall@5 | Recall@10 | Recall@20 | MRR@10 | NDCG@10 | Precision@5 | answered-unanswerable | abstention | latency(ms) |
|---|---|---|---|---|---|---|---|---|---|
| dense | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.267 | 3 | 0.000 | 124 |
| bm25 | 1.000 | 1.000 | 1.000 | 1.000 | 0.993 | 0.267 | 3 | 0.000 | 36 |
| hybrid | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.267 | 3 | 0.000 | 120 |
| hybrid_rerank | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.267 | 3 | 0.000 | 2987 |
