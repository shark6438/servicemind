# Phase 4 proxy release evaluation

Status: **regression_baseline_created** — this report certifies nothing about tenant-domain RAG quality. It reports proxy retrieval metrics on an external silver set and whether they held against the frozen proxy baseline. The §4.1 closure gate is listed but not evaluated; see `gates`.

Queries: 400 (280 answerable / 120 impossible).

| run | Recall@5 | Recall@10 | Recall@20 | MRR@10 | NDCG@10 |
| --- | ---: | ---: | ---: | ---: | ---: |
| BM25 | 0.5286 | 0.5643 | 0.6500 | 0.4414 | 0.4713 |
| BGE-M3 dense | 0.7143 | 0.7536 | 0.7679 | 0.5691 | 0.6142 |
| RRF hybrid | 0.6464 | 0.7357 | 0.7893 | 0.5406 | 0.5869 |
| RRF hybrid + BGE rerank (published: top-30, pure) | 0.6821 | 0.7286 | 0.7857 | 0.5699 | 0.6087 |
| + depth-100 pool, pure rerank | 0.6821 | 0.7357 | 0.7786 | 0.5662 | 0.6074 |
| **production shape (depth-100 + blend)** | 0.6786 | 0.7571 | 0.8071 | 0.5852 | 0.6266 |

The production arm applies `rerank_weight * rerank_score + (1 - rerank_weight) * minmax(fused_rrf_score)` with rerank_weight=0.85, which is what `src/servicemind/rag/service.py` actually runs. The published arm is kept so the historical numbers stay comparable.

## Proxy regression (§6.4)

Verdict: **baseline created by this run**

| metric | baseline | actual | tolerance | held |
| --- | ---: | ---: | ---: | :---: |
| recall_at_5 | 0.6786 | 0.6786 | 0.0000 | yes |
| recall_at_10 | 0.7571 | 0.7571 | 0.0000 | yes |
| recall_at_20 | 0.8071 | 0.8071 | 0.0000 | yes |
| mrr_at_10 | 0.5852 | 0.5852 | 0.0000 | yes |
| ndcg_at_10 | 0.6266 | 0.6266 | 0.0000 | yes |

## Tenant-domain closure gate (§4.1) - not evaluated

| gate | observed on this proxy set | required on tenant release set | status |
| --- | ---: | ---: | --- |
| recall_at_5 | 0.6786 | >= 0.85 | NOT EVALUATED |
| recall_at_10 | 0.7571 | >= 0.9 | NOT EVALUATED |
| mrr_at_10 | 0.5852 | >= 0.75 | NOT EVALUATED |
| ndcg_at_10 | 0.6266 | >= 0.8 | NOT EVALUATED |
| impossible_abstention_rate | 0.78 | >= 0.9 | NOT EVALUATED |
| answerable_answer_rate | 0.3333 | >= 0.9 | NOT EVALUATED |

not evaluated: production abstains through the Reviewer's semantic ABSTAIN on evidence sufficiency, not through a retrieval-score cut; on this set the score separates answerable from impossible at ROC-AUC 0.6061 with a best-case balanced accuracy of 0.5933 over every cut.

## Scope

This is a reproducible external silver benchmark. It replaces unavailable private logs for engineering closure, but does not claim tenant-domain human gold and grants no quality certification.
