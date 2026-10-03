# Reviewer semantic layer — replay of the recorded verdicts

- producer: scripts/replay_phase7_reviewer_semantic.py
- source report: `evaluation/reports/phase7_reviewer_semantic_latest.json` (tool revision `b385df7c2ef6818f24d5f173ca92158989ba3c66+patch(36ba5ff2bb7b)`)
- judge model: `deepseek-v4-flash`
- **model calls: 0**

## Reproduction

- cases replayed: 19
- decisions reproduced: 19
- mismatched: none

## The evasion rows

Of 5 evasion cases, 5 were cleared by the judge, and 1 of those cleared it while asserting the gap in prose: REV-EVA-05.

| case | decision | judge cleared | the gap phrase in the review |
| --- | --- | --- | --- |
| REV-EVA-01 | PASSED | yes | — |
| REV-EVA-02 | PASSED | yes | — |
| REV-EVA-03 | PASSED | yes | — |
| REV-EVA-04 | PASSED | yes | — |
| REV-EVA-05 | PASSED | yes | not carried by |

## All rows

| case | family | decision | reproduced |
| --- | --- | --- | --- |
| REV-ACC-01 | sound | PASSED | ok |
| REV-ACC-02 | sound | PASSED | ok |
| REV-ACC-03 | sound | PASSED | ok |
| REV-ACC-04 | sound | PASSED | ok |
| REV-ACC-05 | sound | PASSED | ok |
| REV-ACC-06 | sound | PASSED | ok |
| REV-ACC-07 | sound | PASSED | ok |
| REV-ACC-08 | sound | PASSED | ok |
| REV-ACC-09 | sound | PASSED | ok |
| REV-ACC-10 | sound | PASSED | ok |
| REV-ACC-11 | sound | PASSED | ok |
| REV-ACC-12 | sound | PASSED | ok |
| REV-EVA-01 | evasion | PASSED | ok |
| REV-EVA-02 | evasion | PASSED | ok |
| REV-EVA-03 | evasion | PASSED | ok |
| REV-EVA-04 | evasion | PASSED | ok |
| REV-EVA-05 | evasion | PASSED | ok |
| REV-SEM-01 | semantic_only | REPLAN | ok |
| REV-SEM-02 | semantic_only | ESCALATE | ok |

## Reading

The adjudicator is a pure function of the judge's verdict, so every decision here follows from the verdict alone. When an evasion case is cleared by the judge and still recorded as PASSED, the loss is in the verdict's vocabulary -- there is no branch that can reach a defect the verdict does not name.
