# Phase 7 — Multi-agent coordination (recorded runs)

Generated 2026-10-02T16:33:28.340595+00:00 from 200 recorded observations.

## What was read, and what was not

- Replays: `/data/shihongye/servicemind/evaluation/quality/replays` — 200 observations.
  - `b385df7c2ef6818f24d5f173ca92158989ba3c66+patch(bb9010dda000)` × 200
- Scorer revision: `b385df7c2ef6818f24d5f173ca92158989ba3c66+patch(6b0306cfada2)`
- Resolved to recorded runs: **200**
- Model calls made: **0**. Runs created: **0**.

The event vocabulary is imported from `scripts/verify_phase7_coordination_live.py`, so a
renamed event appears in both reports as a count that fell to zero rather than as one
report still matching a literal the other stopped emitting.

## Participation

| Reading | Value |
|---|---|
| Agents completing per run (min / mean / max) | 4 / 5.985 / 10 |
| `agent.completed` events per agent | `analysis`=334, `data`=211, `knowledge`=318, `reviewer`=334 |
| Runs with no agent participation recorded | 0.0% |

## Supervisor arbitration

| Reading | Value |
|---|---|
| Runs with a supervisor decision | 100.0% |
| Runs where the supervisor rejected its own agent | 2.0% |
| Policy rejections, total | 4 |
| Runs where a replan proposal was rejected | 3.0% |
| Confidence p50 / p05 | 0.9 / 0.82 |
| Lowest confidence observed | 0.7 |

| Action | Count |
|---|---|
| `analyze` | 334 |
| `dispatch` | 337 |
| `escalate` | 1 |
| `finalize` | 200 |
| `join_evidence` | 337 |
| `plan` | 200 |
| `replan` | 9 |
| `retrieve_more` | 125 |
| `review` | 334 |

## Going around again

A second entry into a phase the supervisor already completed. This is the shape a loop takes
before it becomes a livelock, and it is the reading an outcome-only measurement cannot see:

| Reading | Value |
|---|---|
| Runs that re-entered a phase | 66.0% |
| Entries beyond the first | `analyze`=134, `dispatch`=137, `review`=134 |

## Evidence handoff — what retrieval gathered against what reached the answer

| Reading | Value |
|---|---|
| Evidence rows gathered | 4240 |
| Rows delivered to the analysis join | 3528 |
| Rows dropped at the join | 0 |
| Runs where the join dropped a row | 0.0% |
| Runs citing fewer sources than the analysis was handed | 20.5% |

## Contention between parallel branches

Branches whose recorded intervals intersect *and* whose evidence sets overlap: they ran
at the same time and came back with the same row, which is duplicated retrieval. A pair
in different phases of the graph is excluded, because those are handed shared evidence
by design and counting them would measure the handoff chain, not the fan-out:

| Reading | Value |
|---|---|
| Runs with branches sharing evidence | 0.0% |
| Sharing pairs | — |

## Retrieval declines

A declined lookup is the platform refusing to answer from evidence it judged too thin, so the
score that caused it belongs in the record:

| Reading | Value |
|---|---|
| Runs with a decline | 13.5% |
| Declines, total | 27 |
| Reasons | `no_evidence_above_floor`=27 |

| Best score | Floor |
|---|---|
| 0.010489091 | 0.05 |
| 1.8631747e-05 | 0.05 |
| 9.993032e-05 | 0.05 |
| 0.0001313518 | 0.05 |
| 0.00021654405 | 0.05 |
| 0.00076729245 | 0.05 |
| 0.00017265156 | 0.05 |
| 0.00010391067 | 0.05 |
| 0.00074953143 | 0.05 |
| 5.1845658e-05 | 0.05 |
| 6.5028165e-05 | 0.05 |
| 0.004433765 | 0.05 |
| 0.00015720345 | 0.05 |
| 0.0011116682 | 0.05 |
| 0.000113234375 | 0.05 |
| 2.2650353e-05 | 0.05 |
| 2.6480842e-05 | 0.05 |
| 2.411114e-05 | 0.05 |
| 0.00016219282 | 0.05 |
| 0.00068785116 | 0.05 |
| 0.0007524628 | 0.05 |
| 0.0017891593 | 0.05 |
| 0.00022341637 | 0.05 |
| 9.387641e-05 | 0.05 |
| 0.00036829791 | 0.05 |
| 0.00015843622 | 0.05 |
| 3.5356254e-05 | 0.05 |

## Routing and memory

| Route | Runs |
|---|---|
| `complex_workflow` | 173 |
| `simple_knowledge_query` | 27 |

| Reading | Value |
|---|---|
| Runs writing a memory record | 99.5% |
| Memory records written, total | 199 |

## Anomalies

None.

## Limitations

- Every reading is of the runs' own timeline. A node that did work without emitting an event is invisible here, and this report cannot tell that apart from a node that did nothing.
- Contention is measured only between branches whose recorded intervals intersect, so a duplicated retrieval by two branches that ran one after the other is not counted.
- "Runs citing fewer sources than the analysis was handed" is not by itself a defect: the citation writer is entitled to cite fewer. It is reported as a distribution, not a rate with a threshold.
- The supervisor's confidence is the model's own number. It is reported as recorded, and nothing here checks it against the decision that carried it.
- This corpus is 200 observations of one tenant at one revision; the distributions describe that corpus and are not population estimates.
