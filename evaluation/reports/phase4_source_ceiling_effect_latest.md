# The per-source ceiling, before and after

Status: **MEASUREMENT_DIAGNOSTIC** — two TechQA production runs over
28481 documents and 400 queries
(280 answerable), identical in corpus, query set and model revisions.

| arm | packed mean before | packed max before | packed mean after | packed max after | delta |
| --- | ---: | ---: | ---: | ---: | ---: |
| bm25 | 3.943 | **4** | 8.104 | **19** | +15 |
| dense | 3.761 | **4** | 7.439 | **16** | +12 |
| hybrid | 3.907 | **4** | 7.857 | **17** | +13 |
| hybrid_rerank | 3.875 | **4** | 7.496 | **16** | +12 |

The ceiling is 4 parents per source and the value was **not** changed. What
changed is when it applies: SERVICEMIND_RAG_MAX_PARENTS_PER_SOURCE is applied only when the candidate pool names more than one source, through the shared predicate servicemind.rag.service.source_ceiling_applies. The ceiling is a fairness bound between sources; on a single-source corpus it had nothing to balance and became a ceiling on the whole prompt.

Before, all 4 arms stopped at exactly
4; after, the pack continues to 19 and the
token budget of 8000 is what bounds it.

## The recall columns, which are not comparable

| arm | R@5 before | R@5 after | delta | R@10 before | R@10 after | delta |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| bm25 | 0.5607 | 0.5786 | +0.0179 | 0.5607 | 0.6071 | +0.0464 |
| dense | 0.6643 | 0.6714 | +0.0071 | 0.6643 | 0.7000 | +0.0357 |
| hybrid | 0.6179 | 0.6321 | +0.0142 | 0.6179 | 0.6786 | +0.0607 |
| hybrid_rerank | 0.6536 | 0.6571 | +0.0035 | 0.6536 | 0.7214 | +0.0678 |

before, every arm packed at most 4 documents -- the ceiling of 4 -- with the budget of 8000 tokens more than a third unspent; after, the pack continues to 19 and the budget is what bounds it. The recall columns moved, and the move is not evidence of better retrieval: with a longer pack, Recall@5 counts the first five of a list that no longer stops at four. Read each recall beside the packed mean on the same row

**Every arm on the before side returned one number at all three recall cutoffs**
(`before_cutoffs_are_one_measurement` = `True`,
computed from the runs, not asserted here). On the after side that identity
no longer holds
(`False`). This is the clearest statement of what the
fix did to the *measurement*: the wider cutoffs stopped being a second name for the pack.
It is also why the delta column cannot be read as retrieval improving on its own -- where a
cutoff was previously unreachable, part of the movement is the column starting to measure
something it could not measure before.

## Limitations

- neither run records the tokens a pack actually spent, only how many documents it held. The budget arithmetic is measured separately, over parent sizes rebuilt with the production chunker, in evaluation/reports/phase4_packing_ceiling_latest.json
- the hybrid arm issues LLM-written lexical sub-queries, so it is not reproducible run to run; a repeated run of the *same* revision moved its Recall@5 by about 0.004 (one query in 280), which is the floor on any hybrid delta here, before and after alike
- one corpus, one label set, one model revision. Nothing here generalises to a corpus with more than one source, where the ceiling is in force in both halves and the fix changes nothing
