# Phase 7 — Trajectory scoring (recorded runs)

Generated 2026-10-02T16:33:26.518078+00:00 from 200 recorded observations.

## What was read, and what was not

- Replays: `/data/shihongye/servicemind/evaluation/quality/replays` — 200 observations.
  - `b385df7c2ef6818f24d5f173ca92158989ba3c66+patch(bb9010dda000)` × 200
- Scorer revision: `b385df7c2ef6818f24d5f173ca92158989ba3c66+patch(6b0306cfada2)`
- Resolved to recorded runs: **200**
- Model calls made: **0**. Runs created: **0**.

The two revisions differ on purpose and are not interchangeable. `evaluation/` and `docs/`
are exempt from the source fingerprint; `scripts/` is not, so a scorer written after a
batch carries its own revision. The data revision above is the platform's; the scorer
revision is this report's.

## Shape

| Reading | Value |
|---|---|
| Trajectory length (min / mean / max) | 24 / 36.96 / 63 |
| Widest parallel branch fan-out | 2 |
| Runs with a stalled or degraded branch | 4.5% |
| Model calls across the corpus | 3316 |
| Tool calls across the corpus | 889 |
| Runs with a provider-level retry | 12.5% |
| Model calls that did not succeed | 9 |
| `agent.completed` attempts, as each agent reports it | `analysis:1`=334, `data:2`=62, `data:3`=149, `knowledge:1`=318, `reviewer:1`=334 |

## Plan conformance (internal to the run — no outside contract)

The plan is the run's own declaration of the work, so this reading needs no outside opinion:
it asks whether every planned task ran and whether anything ran that no plan asked for.

| Reading | Value |
|---|---|
| Runs where every planned task ran and nothing else did | 100.0% |
| Runs with a step no plan called for | 0.0% |
| Runs that planned a task and never ran it | 0.0% |
| Runs that executed a task more than once | 0.0% |
| Runs that revised their plan (retrieve-more or replan) | 65.0% |
| Runs that replanned | 4.5% |

## The participation surface, and its one trap

Participation is read from `run_events`' `agent.completed` events. `agent_runs.result`'s
`agent_invocations` list holds *subagent envelopes*, and the knowledge node builds none,
so scored from that list retrieval would appear never to run:

| Reading | Value |
|---|---|
| Runs where `agent_invocations` is short of the timeline | 100.0% |
| Records in the timeline but absent from `agent_invocations` | 318 |

## Supervisor arbitration

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

## Reviewer outcome, as the trajectory recorded it

| Outcome | Runs |
|---|---|
| `escalate` | 1 |
| `passed` | 69 |
| `replan` | 9 |
| `retrieve_more` | 121 |

## Authored contract — the one reading that assumes something

Required agents per case kind, declared in `scripts/report_phase7_trajectory.py`:

| Kind | Must pass through |
|---|---|
| `answerable` | knowledge, analysis, reviewer |
| `insufficient-evidence` | knowledge, reviewer |

Runs failing that contract: **0** of 200.

Nodes forbidden on a read-only run: `execute`, `action`, `glpi.followup.created`.

## Anomalies

None: every recorded run matched its plan, its kind's contract, and its own terminal state.

## Limitations

- The trajectory is the token list the run recorded, not an independent trace: a node that ran without emitting a token is invisible here.
- 'Distinct agents' is read from the trajectory's agent-step tokens; one agent working two tasks counts once.
- Plan conformance compares execution to the run's *own* plan. A run whose plan was itself wrong conforms perfectly, and this reading will not notice.
- REQUIRED_AGENTS is an authored contract, not a measurement of the platform.
- An agent envelope's `attempts` is not a retry count and is not comparable between agents: the DataAgent sums its tool calls into it (`agents/data.py`), so a three-tool data task reports attempts=3 while the other three agents report 1 in every run. Retries are read from `model_invocations.retries`, which is provider-level and means the same thing for every role.
- `agent_invocations` is not a complete participation record: the knowledge node emits `agent.completed` and a `task_completions` entry but no subagent envelope, so it is absent from that list in every run. Participation here is read from `run_events` instead, and the size of the gap is reported above.
