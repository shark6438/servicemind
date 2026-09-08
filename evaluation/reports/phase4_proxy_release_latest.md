# Phase 4 proxy release evaluation

Status: **failed**

Queries: 400 (280 answerable / 120 impossible).

| run | Recall@5 | Recall@10 | Recall@20 | MRR@10 | NDCG@10 |
| --- | ---: | ---: | ---: | ---: | ---: |
| BM25 | 0.5429 | 0.5821 | 0.6464 | 0.4527 | 0.4839 |
| BGE-M3 dense | 0.6607 | 0.6964 | 0.7429 | 0.5485 | 0.5848 |
| RRF hybrid | 0.6357 | 0.6929 | 0.7536 | 0.5369 | 0.5742 |
| RRF hybrid + BGE rerank | 0.6536 | 0.7000 | 0.7536 | 0.5541 | 0.5896 |

## Gates

- FAIL: recall_at_5 0.6536 >= 0.85
- FAIL: recall_at_10 0.7 >= 0.9
- FAIL: mrr_at_10 0.5541 >= 0.75
- FAIL: ndcg_at_10 0.5896 >= 0.8

## Scope

This is a reproducible external silver benchmark. It replaces unavailable private logs for engineering closure, but does not claim tenant-domain human gold.
