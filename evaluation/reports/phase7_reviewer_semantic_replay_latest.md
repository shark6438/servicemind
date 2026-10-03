# Reviewer semantic layer — replay of the recorded verdicts

- producer: scripts/replay_phase7_reviewer_semantic.py
- source report: `evaluation/reports/phase7_reviewer_semantic_latest.json` (tool revision `b385df7c2ef6818f24d5f173ca92158989ba3c66+patch(34a08e5934da)`)
- judge model: `deepseek-v4-flash`
- **model calls: 0**

## Reproduction

- cases replayed: 18
- decisions reproduced: 18
- mismatched: none

## The evasion rows

Of 4 evasion cases, 0 were cleared by the judge, and 0 of those cleared it while asserting the gap in prose: none.

| case | decision | judge cleared | the gap phrase in the review |
| --- | --- | --- | --- |
| REV-EVA-01 | RETRIEVE_MORE | no | — |
| REV-EVA-03 | RETRIEVE_MORE | no | — |
| REV-EVA-04 | REPLAN | no | — |
| REV-EVA-05 | RETRIEVE_MORE | no | not carried by |

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
| REV-EVA-01 | evasion | RETRIEVE_MORE | ok |
| REV-EVA-03 | evasion | RETRIEVE_MORE | ok |
| REV-EVA-04 | evasion | REPLAN | ok |
| REV-EVA-05 | evasion | RETRIEVE_MORE | ok |
| REV-SEM-01 | semantic_only | REPLAN | ok |
| REV-SEM-02 | semantic_only | ESCALATE | ok |

## Reading

The adjudicator is a pure function of the judge's verdict, so every decision here follows from the verdict alone. When an evasion case is cleared by the judge and still recorded as PASSED, the loss is in the verdict's vocabulary -- there is no branch that can reach a defect the verdict does not name. That is what the pre-widening replay shows and what the widened contract removed: the same judge readings now carry the defects the adjudicator needs to see them.
