# Which ceiling stops the production prompt at 19 documents

Status: **MEASUREMENT_DIAGNOSTIC** (400 documents re-chunked, 639 parents; packing read from `evaluation/reports/phase4_techqa_production_latest.json`)

The production run packs at most 19 documents per answerable query
against a 8000-token budget. This report asks which ceiling
produced that number. A recall cutoff the pack never reached counts the whole pack, so how
wide the pack got is also how wide the recall columns are -- see
`scripts/audit_rag_quality_state.py`, which derives that per arm.

## 1. How large is a parent

Rebuilt with the production chunker, so these are in the units the budget is spent in.

| quantile | tokens |
| --- | ---: |
| p50 | 1107 |
| p90 | 1501 |
| p99 | 1778 |
| mean | 1014.3 |
| max | 3114 |

## 2. What the budget would have admitted

One more parent is refused by the budget only when the parents already packed plus it
exceed 8000. So the largest one that still fits, and the
share of observed parents at or below that size:

| scenario | headroom for one more parent (tokens) | share of parents that still fit |
| --- | ---: | ---: |
| 19 parents at p50 | -13033 | 0.0000 |
| 19 parents at p90 | -20519 | 0.0000 |
| 19 parents at p99 | -25782 | 0.0000 |

## 3. What the run actually packed

| arm | mean | min | max | max vs the 4-parent source ceiling |
| --- | ---: | ---: | ---: | --- |
| dense | 7.439 | 4 | **16** | reaches |
| bm25 | 8.104 | 4 | **19** | reaches |
| hybrid | 7.857 | 5 | **17** | reaches |
| hybrid_rerank | 7.496 | 4 | **16** | reaches |

The production run counted 1 distinct `provenance.source` over the
whole corpus (this sample names 1). `source` is a property of the *ingester*, not of
the document: in `rag/sources.py` four of the five ingesters hardcode it
(`pagerduty_incident_response_docs`, `mendeley_help_desk_tickets`, `glpi_knowledge_base`,
`servicemind_internal_runbook`) and `AttachmentSource` takes it at construction. So one
ingester instance stamps the same source on every document it loads, and a guard whose
comment states its purpose as "no single source may drown every other source" has no second
source to balance -- which is exactly the case `rag.service.source_ceiling_applies` exists
to recognise.

`SERVICEMIND_RAG_MAX_PARENTS_PER_SOURCE` is **not in force** on this corpus: it names 1 source, so there is no second source to balance and the packer does not apply the ceiling at all. Any arm whose maximum reads the ceiling's number passed through it rather than being stopped by it.

**Attributed to: the token budget.** `final_k` is 24.

## What this does and does not say

every arm packs at most 19 documents, so every recall cutoff at or above 19 counts the whole pack: comparing such a figure against a threshold written for a depth-100 pool compares a cut list against a ranked one. Whether the budget ended the list is the arithmetic below -- one more parent at the median size would leave -13033 tokens of the 8000-token budget, the budget answering no -- and the verdict below is the other half of the answer: the token budget

Raising whichever ceiling this report names is a configuration change and is **not**
approved here. Before it could be approved it needs an answer to a question this script
cannot ask: does a longer prompt make the reviewer's judgments better, worse, or merely
longer? Recall over a cut list cannot answer that, because moving the cut moves *which*
cutoff each threshold names -- with more documents packed, Recall@5 becomes a statement
about the first five of a longer list rather than about the whole of a short one.

## Limitations

- the parent sizes are a sample of 400 documents, not all 28481; the observed maximum is not sampled -- it is read from the production run, which measured every query
- the sample is the first 400 entries of the archive in archive order, which is not a random draw. It chunks into 1.5975 parents per document against 1.6394 over the whole corpus, which is a cross-check on the sample rather than a guarantee about it: the parent counts agree, but nothing here rules out an order effect on parent *sizes*
- the packed parents are the top-ranked ones, not a random draw from this distribution. If ranking correlates with size, the headroom moves; the size table bounds the shape of the answer, not its exact value
- ``build_parents`` closes a parent before the block that would overflow its 1500-token target, so one long parsed block can produce a larger parent than the target. The measured maximum above is one of those, and it is larger than every quantile in the table
- nothing here evaluates what a larger packed list does to answer quality. A longer prompt is a change to what the reviewer reads, and that is measured by the end-to-end answerability work, not here
