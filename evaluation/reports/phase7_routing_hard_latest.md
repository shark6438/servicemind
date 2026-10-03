# Router hard-case evaluation

- golden set: 107 cases, accuracy 1.0000 (evaluation/routing/routing.jsonl)
- hard set: 41 cases, accuracy 0.8780 (evaluation/routing/routing_hard.v1.jsonl)

## By expectation strength

| strength | cases | correct | accuracy |
|---|---|---|---|
| boundary | 9 | 4 | 0.4444 |
| contract | 32 | 32 | 1.0000 |

## By family

| family | cases | correct | accuracy |
|---|---|---|---|
| ambiguous | 8 | 8 | 1.0000 |
| compound | 8 | 6 | 0.7500 |
| data-boundary | 3 | 3 | 1.0000 |
| forbidden-boundary | 4 | 3 | 0.7500 |
| goal-change | 5 | 5 | 1.0000 |
| missing-info | 6 | 4 | 0.6667 |
| mixed-read-write | 7 | 7 | 1.0000 |

## Mismatches

### hard-cmp-06 (boundary, compound)

- text: `查询工单 2 的状态，并查找 VPN 操作手册` (request_write=False)
- expected `complex_workflow`, got `simple_knowledge_query` via `knowledge_lookup_only` (confidence 0.97)
- why the expectation: spans the data and knowledge surfaces; a single-agent fast path carries only one capability

### hard-cmp-07 (boundary, compound)

- text: `What is ticket 2's priority, and what does the priority matrix say?` (request_write=False)
- expected `complex_workflow`, got `simple_data_query` via `fact_lookup_only` (confidence 0.97)
- why the expectation: same span as cmp-06, in the other order

### hard-mis-03 (boundary, missing-info)

- text: `如何处理？` (request_write=False)
- expected `complex_workflow`, got `simple_knowledge_query` via `knowledge_lookup_only` (confidence 0.97)
- why the expectation: no subject at all, yet 如何处理 is listed as a knowledge row; the lookup has nothing to look up

### hard-mis-04 (boundary, missing-info)

- text: `该怎么办？` (request_write=False)
- expected `complex_workflow`, got `simple_knowledge_query` via `knowledge_lookup_only` (confidence 0.97)
- why the expectation: same shape as mis-03 via 怎么办

### hard-neg-03 (boundary, forbidden-boundary)

- text: `Does the runbook cover deleting old records?` (request_write=False)
- expected `unsupported`, got `complex_workflow` via `ambiguous_request_requires_review` (confidence 0.7)
- why the expectation: forbidden verb inside a knowledge question

## Reading

contract rows are those the router's own documented rules decide outright; boundary rows are design trade-offs the contract does not settle. A contract mismatch is an implementation defect; a boundary mismatch is a decision for a maintainer. The two are reported apart so neither hides behind the other.
