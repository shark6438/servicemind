# TechQA release set through the production retrieval path

Status: **PIPELINE_COMPARISON_ONLY** (documents indexed: 28481, ingest 3217.1s, queries 400)

Same corpus, same queries, same labels, same pinned model revisions as the proxy report.
The only thing that moved is the pipeline.

| arm (production vs proxy) | production recall_at_5 | proxy recall_at_5 | delta | production recall_at_10 | proxy recall_at_10 | delta | production recall_at_20 | proxy recall_at_20 | delta | production mrr_at_10 | proxy mrr_at_10 | delta | production ndcg_at_10 | proxy ndcg_at_10 | delta |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| bm25_vs_bm25 | 0.5786 | 0.5286 | +0.0500 | 0.6071 | 0.5643 | +0.0428 | 0.6071 | 0.6500 | -0.0429 | 0.4923 | 0.4414 | +0.0509 | 0.5203 | 0.4713 | +0.0490 |
| dense_vs_dense | 0.6714 | 0.7143 | -0.0429 | 0.7000 | 0.7536 | -0.0536 | 0.7000 | 0.7679 | -0.0679 | 0.5615 | 0.5691 | -0.0076 | 0.5955 | 0.6142 | -0.0187 |
| hybrid_vs_hybrid_rrf | 0.6321 | 0.6464 | -0.0143 | 0.6786 | 0.7357 | -0.0571 | 0.6821 | 0.7893 | -0.1072 | 0.5367 | 0.5406 | -0.0039 | 0.5711 | 0.5869 | -0.0158 |
| hybrid_rerank_vs_production | 0.6571 | 0.6857 | -0.0286 | 0.7214 | 0.7643 | -0.0429 | 0.7214 | 0.8071 | -0.0857 | 0.5684 | 0.5848 | -0.0164 | 0.6054 | 0.6279 | -0.0225 |

## §4.1 visible evidence: wrong tenant / ACL / expired version

| baseline | wrong tenant | unauthorized group | expired version | total |
|---|---|---|---|---|
| dense | 0 | 0 | 0 | 0 |
| bm25 | 0 | 0 | 0 | 0 |
| hybrid | 0 | 0 | 0 | 0 |
| hybrid_rerank | 0 | 0 | 0 | 0 |

Corpus strata this was measured over: 1 tenant(s), 0 group-restricted, 0 inactive, 0 with an effective end, out of 28481 documents. A zero here is read against those numbers.

## What the recall figures are actually counting

The proxy cuts at top-k. Production returns the packed prompt, which stops earlier: at
`final_k` = 24, at a 8000-token budget, and at
2 parents per document / 4 per source.
These are the documents that actually reached the prompt, per answerable query:

| arm | mean documents packed | min | max | vs the 4-parent source ceiling |
| --- | ---: | ---: | ---: | :---: |
| dense | 7.44 | 4 | 16 | reaches |
| bm25 | 8.10 | 4 | 19 | reaches |
| hybrid | 7.86 | 5 | 17 | reaches |
| hybrid_rerank | 7.50 | 4 | 16 | reaches |

Where a packed count is below the cutoff, that Recall@k is reporting the constraint that
bound the pack rather than the retriever's ranking, and Recall@10 and Recall@20 are the
same measurement under two names.

Every document here names the same source, so the 4-parent ceiling has no other source to balance and is not in force: the pack is bounded by the token budget and by the candidate list. The arms whose maximum nevertheless reads 4 (bm25, dense, hybrid, hybrid_rerank) pass through that number, they are not stopped by it.

## Why §4.1 still does not apply

- labels are tier silver with 0 annotator(s) and kappa None; §3.3 requires two domain annotators over a stratified 20% sample with kappa >= 0.80
- no query carries 0--4 judgments, so the set can only be scored by the binary metrics and cannot express the §4.1 wrong-ACL/expired-version count
- the set has no hard-negative stratum (§3.2 asks for 80)
- the set has no must-refuse query of kind version_conflict; a single abstention rate cannot tell the three apart
- the set has no must-refuse query of kind must_refuse_access; a single abstention rate cannot tell the three apart
- the set has no development split, so nothing could have been tuned without touching the queries that are supposed to stay hidden

## Limitations

- the labels are source-provided silver, not tenant-domain human qrels
- each answerable query carries a single relevant filename, so a rank-2 hit scores the same as a miss under Recall@10
- `abstention_rate` counts unanswerable queries that returned *any* context. It is not the Reviewer's semantic abstention and must not be reported as an answerable answer rate
- parents are held in memory rather than read under PostgreSQL row-level security, so this run exercises the expansion *call* and not the RLS policy that authorises it
- the two chunkers count tokens in different units: the proxy splits on the bge-m3 tokenizer (`AutoTokenizer` for the pinned revision), production on `ConservativeOfflineEncoding` whenever the tiktoken table is not cached, which it is not on this host. 480 production tokens and 420 proxy tokens are therefore not the same quantity. The comparison is still the production path against the proxy path, which is what it claims to be; it just cannot be decomposed into RRF's share and the chunker's share
- the two sides cut at different things: the proxy ranks passages and cuts at top-k, production returns the packed prompt, which stops at the token budget and the diversity ceilings. See `packing` for how many documents actually reached the prompt; where that is below the cutoff, the recall number is reporting the budget rather than the retriever

## Per-arm detail (production path)

# Phase 4 retrieval evaluation — techqa-rag-eval-release-v1.2

Top-k cutoffs: 5, 10, 20.

| baseline | Recall@5 | Recall@10 | Recall@20 | MRR@10 | NDCG@10 | Precision@5 | answered-unanswerable | abstention | latency(ms) |
|---|---|---|---|---|---|---|---|---|---|
| dense | 0.671 | 0.700 | 0.700 | 0.562 | 0.595 | 0.134 | 120 | 0.000 | 94 |
| bm25 | 0.579 | 0.607 | 0.607 | 0.492 | 0.520 | 0.116 | 120 | 0.000 | 195 |
| hybrid | 0.632 | 0.679 | 0.682 | 0.537 | 0.571 | 0.126 | 120 | 0.000 | 216 |
| hybrid_rerank | 0.657 | 0.721 | 0.721 | 0.568 | 0.605 | 0.131 | 120 | 0.000 | 758 |

