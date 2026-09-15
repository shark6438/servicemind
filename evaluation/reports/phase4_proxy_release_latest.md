# Phase 4 proxy release evaluation

Status: **failed**

Queries: 400 (280 answerable / 120 impossible).

| run | Recall@5 | Recall@10 | Recall@20 | MRR@10 | NDCG@10 |
| --- | ---: | ---: | ---: | ---: | ---: |
| BM25 | 0.5286 | 0.5643 | 0.6500 | 0.4414 | 0.4713 |
| BGE-M3 dense | 0.7143 | 0.7536 | 0.7679 | 0.5691 | 0.6142 |
| RRF hybrid | 0.6464 | 0.7357 | 0.7893 | 0.5406 | 0.5869 |
| RRF hybrid + BGE rerank | 0.6821 | 0.7286 | 0.7857 | 0.5699 | 0.6087 |

## Gates

- FAIL: recall_at_5 0.6821 >= 0.85
- FAIL: recall_at_10 0.7286 >= 0.9
- FAIL: mrr_at_10 0.5699 >= 0.75
- FAIL: ndcg_at_10 0.6087 >= 0.8
- FAIL: impossible_abstention_rate 0.78 >= 0.9
- FAIL: answerable_answer_rate 0.3333 >= 0.9

## Scope

This is a reproducible external silver benchmark. It replaces unavailable private logs for engineering closure, but does not claim tenant-domain human gold.
