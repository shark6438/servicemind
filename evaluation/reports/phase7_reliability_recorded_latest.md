# Phase 7 — Stochastic reliability, from recorded repeats

Generated 2026-10-02T16:25:32.992615+00:00 from 120 recorded observations of 20 cases.

## What was read, and what was not

- Replays: `/data/shihongye/servicemind/evaluation/load/replays` — 120 observations.
  - `b385df7c2ef6818f24d5f173ca92158989ba3c66+patch(96133a9537b6)` × 120
- Scorer revision: `b385df7c2ef6818f24d5f173ca92158989ba3c66+patch(ccfc9889821c)`
- Model calls made: **0**. Runs created: **0**.

These repeats were recorded by the load batch, not by a repeat experiment. The confounds are
in the limitations below and in `docs/PHASE7_REMAINING_EVALUATION_2026-10-02.md`; this report
is a floor, not a designed measurement.

## Agreement within each stratum

Agreement means all four axes matched: terminal status, reviewer decision, citation set and
plan digest.

| Revision | Concurrency | Repeats/case | Cases with repeats | Agreeing on every axis | Agreement |
|---|---|---|---|---|---|
| `b385df7c2ef6…` | 1 | 1–1 | 0 | 20 | — (no repeats) |
| `b385df7c2ef6…` | 5 | 2–2 | 20 | 10 | 50.0% |
| `b385df7c2ef6…` | 10 | 3–3 | 20 | 6 | 30.0% |

## Latency and retrieval per stratum

| Revision | Concurrency | p50 (s) | p95 (s) | max (s) | Expected citation hit | Unreadable citations | Runs with errors |
|---|---|---|---|---|---|---|---|
| `b385df7c2ef6…` | 1 | 29.64 | 33.76 | 33.76 | 95.0% | 0 | 0 |
| `b385df7c2ef6…` | 5 | 19.8 | 35.24 | 38.12 | 95.0% | 0 | 0 |
| `b385df7c2ef6…` | 10 | 20.32 | 35.83 | 38.57 | 95.0% | 0 | 0 |

## Disagreements, enumerated

| Case | Axes that moved |
|---|---|
| Q-002 | citations |
| Q-004 | citations |
| Q-005 | citations |
| Q-006 | citations |
| Q-008 | citations |
| Q-009 | citations |
| Q-013 | citations |
| Q-016 | citations |
| Q-017 | citations |
| Q-020 | citations |
| Q-001 | citations |
| Q-002 | citations |
| Q-004 | citations |
| Q-005 | citations |
| Q-006 | citations |
| Q-008 | citations |
| Q-009 | citations |
| Q-011 | citations |
| Q-014 | citations |
| Q-015 | citations |
| Q-016 | citations |
| Q-017 | citations |
| Q-019 | citations |
| Q-020 | citations |

Every disagreement above is in the citation set: terminal status, reviewer decision and
plan digest never moved across any repeat. In each one the document the case requires was
still cited — the expected-citation hit rate is 100% in every stratum — so what varies is
the surrounding context, not whether the question was answered.

## The same case at two concurrency tiers, within one revision

One revision holds two tiers, so those two can be compared without the revision changing
under the comparison. The third tier sits on a different revision and is excluded rather
than pooled.

| Revision | Tiers | Cases compared | First repeat moved between tiers |
|---|---|---|---|
| `b385df7c2ef6…` | 1, 5, 10 | 20 | 0 |

## Floor

| Reading | Value |
|---|---|
| Repeated cases | 40 |
| Cases disagreeing on some axis | 24 |
| Axes on which any case disagreed | `citations`=24 |
| Disagreement rate | 60.0% |

## Limitations

- These repeats come from the load batch, not from a repeat experiment: concurrency varies with the number of repeats, so the tier with the most repeats is also the tier with the most contention.
- Every observation is on one revision (`b385df7c2ef6…`), so a difference between tiers is at least not a revision change. That is all it rules out: the tiers hold different numbers of repeats, so this is still not a controlled comparison of concurrency.
- Repeats per case are 1, 2, 3 across the strata. At two repeats a single disagreement is a 50% rate for that case, so per-case rates are not reported.
- The 60.0% floor is measured on the repeats that were recorded and that survived on disk. It is a floor on those repeats, not an estimate of the platform's instability -- and the same limit applies in the other direction, where a low or zero rate would be a fact about the corpus rather than evidence of determinism.
- Four axes are compared. Two runs that differ in a way none of the four records -- a different root cause in the same answer, a different agent path to the same citations -- count as agreeing.
