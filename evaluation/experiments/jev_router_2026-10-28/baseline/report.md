# Rule and Jev routing comparison

Status: **BASELINE_ONLY**

| Dataset | Planned | Rule correct | Jev observed | Jev correct |
| --- | ---: | ---: | ---: | ---: |
| routing.jsonl | 107 | 107 | 0 | not measured |
| routing_hard.v1.jsonl | 41 | 36 | 0 | not measured |
| routing_semantic_probe.v1.jsonl | 64 | 30 | 0 | not measured |

## Interpretation

- Routing labels only; no retrieval, generation, review, writes or end-to-end savings measured.
- Existing regression cases co-evolved with the rules; boundary labels are design choices.
- New semantic probes are assistant-authored and unreviewed, not human gold or a blind holdout.
- Jev probabilities and rule confidence constants are not interchangeable or calibrated here.
- Failed and unattempted rows stay in the planned denominator; no automatic retries.

## Labels and coverage

| Label strength | Planned | Rule correct | Jev observed | Jev correct |
| --- | ---: | ---: | ---: | ---: |
| boundary | 9 | 4 | 0 | not measured |
| contract | 32 | 32 | 0 | not measured |
| proposed_semantics | 64 | 30 | 0 | not measured |
| rule_regression | 107 | 107 | 0 | not measured |

## Latency and paired changes

Times are milliseconds; Jev includes network round trips. Changes count paired observations only.

| Dataset | Rule P50 / P95 | Jev P50 / P95 | Improved / regressed |
| --- | --- | --- | --- |
| routing.jsonl | 0.025 / 0.056 | not measured | not measured |
| routing_hard.v1.jsonl | 0.024 / 0.060 | not measured | not measured |
| routing_semantic_probe.v1.jsonl | 0.038 / 0.063 | not measured | not measured |
