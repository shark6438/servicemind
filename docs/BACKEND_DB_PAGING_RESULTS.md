# Task query paging and index comparison

The benchmark ran on **2026-11-05** against a disposable PostgreSQL 16.15 container.
It created only temporary tables. No production tenant data or paid model calls were used.
The executable source is [`scripts/benchmark_run_query_paging.py`](../scripts/benchmark_run_query_paging.py);
the raw result is [`evaluation/reports/db_paging_before_after.json`](../evaluation/reports/db_paging_before_after.json).

The fixed data set has 120,000 runs across five tenants (6% nonterminal), each with a
256-byte payload, and one 30,000-event timeline with 768-byte event payloads. Five
warm `EXPLAIN (ANALYZE, BUFFERS)` runs were measured per task query, and five client
fetches per timeline query. The values below are medians for this isolated test:

| Query | Before | After | Mechanism |
| --- | ---: | ---: | --- |
| Recent 100 runs of one tenant | 28.480 ms | 0.088 ms | Tenant/time/ID index replaces bitmap scan plus sort |
| Oldest 100 recoverable runs | 11.794 ms | 0.116 ms | Partial live-run index replaces bitmap scan plus sort |
| Fetch one event timeline | 59.724 ms, 30,000 rows / 23.04 MB payload | 0.744 ms, 100 rows / 76.8 KB payload | Sequence cursor pages instead of loading all events into one response |

These are **synthetic query timings**, not the platform's end-to-end latency or a
production speedup. Data shape, cache warmth, tenant distribution, and PostgreSQL's
query planner can change the outcome. The query plans changed from bitmap scan plus
sort to an ordered index scan. The event table already has a unique `(run_id, sequence)`
index; event paging needed no new index. Indexes add write and storage cost, which
this benchmark does not measure. The timeline comparison measures the cost of the
first bounded page versus the former full fetch; it does not claim all 30,000 events
can be transferred in the time needed for one 100-event page. The task queries fetch
whole rows, matching the repository's materialization rather than an index-only ID query.
They model tenant/status/time filtering only; per-user filters, JSONB access checks,
and PostgreSQL RLS enforcement are not part of these temporary-table timings.

The repository API now accepts `list_events(..., limit=100)` and
`list_recoverable_runs(limit=100, after_created_at=..., after_run_id=...)`. The
existing calls with no limit keep their previous semantics; SSE reads 100-event pages,
and legacy startup recovery now scans 100-run pages with stable time/ID cursors. A
new `create_run(..., event_payload=...)` path writes
the initial task event in the same PostgreSQL transaction as the task row.

Worker-mode admission can additionally pass `queue_limit`. A tenant-scoped
PostgreSQL advisory transaction lock serializes the count of pending/running runs
with the new insert, so concurrent API processes cannot both consume one remaining
slot. Existing callers omit the limit. Duplicate webhook deliveries return their
existing task even when the backlog is full; a rejected new webhook rolls back its
receipt, task and event together. Paused approvals/reviews do not consume a runnable
admission slot. This is admission control, not a claim that external writes execute
exactly once.

To reproduce, start any isolated PostgreSQL instance and set `BENCH_DATABASE_URL`
to its connection URL, then run:

```bash
python scripts/benchmark_run_query_paging.py
```

The script creates and drops its temporary tables in one connection. The three live
PostgreSQL integration tests in `tests/servicemind/test_repository_paging.py` verify
that event and recovery pages neither lose nor duplicate rows when timestamps tie,
and that failure to persist an initial event rolls back the newly created run.
Legacy recovery's unit tests also verify that its caller uses bounded pages without
revisiting a still-blocked run. An empty scratch database was migrated through 0016,
downgraded to 0015, and upgraded again; `alembic check` reported no model/schema drift.
