# Rule and Jev routing comparison

Status: **COMPLETED**

| Dataset | Planned | Rule correct | Jev observed | Jev correct |
| --- | ---: | ---: | ---: | ---: |
| routing.jsonl | 107 | 107 | 107 | 106 |
| routing_hard.v1.jsonl | 41 | 36 | 41 | 35 |
| routing_semantic_probe.v1.jsonl | 64 | 30 | 64 | 64 |

## Interpretation

- Routing labels only; no retrieval, generation, review, writes or end-to-end savings measured.
- Existing regression cases co-evolved with the rules; boundary labels are design choices.
- New semantic probes are assistant-authored and unreviewed, not human gold or a blind holdout.
- Jev probabilities and rule confidence constants are not interchangeable or calibrated here.
- Failed and unattempted rows stay in the planned denominator; no automatic retries.

## Labels and coverage

| Label strength | Planned | Rule correct | Jev observed | Jev correct |
| --- | ---: | ---: | ---: | ---: |
| boundary | 9 | 4 | 9 | 5 |
| contract | 32 | 32 | 32 | 30 |
| proposed_semantics | 64 | 30 | 64 | 64 |
| rule_regression | 107 | 107 | 107 | 106 |

## Latency and paired changes

Times are milliseconds; Jev includes network round trips. Changes count paired observations only.

| Dataset | Rule P50 / P95 | Jev P50 / P95 | Improved / regressed |
| --- | --- | --- | --- |
| routing.jsonl | 0.030 / 0.060 | 1196.090 / 1713.127 | 0 / 1 |
| routing_hard.v1.jsonl | 0.026 / 0.060 | 1180.254 / 1693.547 | 4 / 5 |
| routing_semantic_probe.v1.jsonl | 0.048 / 0.073 | 1155.904 / 1738.286 | 34 / 0 |

Priced usage: USD 0.006630792; unresolved reservations: USD 0.002752512; cap: USD 0.70.
