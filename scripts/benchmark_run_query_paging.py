"""Compare task queries before and after paging/indexes on disposable tables.

Supply BENCH_DATABASE_URL for an isolated PostgreSQL instance. Only TEMP tables are
created; no ServiceMind table is read or modified. The fixed data distribution
contains five tenants and 6% live runs, plus one 30k-event timeline.
"""

from __future__ import annotations

import json
import os
import statistics
import time
from typing import Any

import psycopg

RUNS = 120_000
EVENTS = 30_000
REPEATS = 5


def _explain(cur: psycopg.Cursor[Any], query: str) -> dict[str, Any]:
    samples = []
    plan = None
    for _ in range(REPEATS):
        cur.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {query}")
        result = cur.fetchone()
        assert result is not None
        plan = result[0][0]
        samples.append(float(plan["Execution Time"]))
    assert plan is not None
    return {
        "median_ms": statistics.median(samples),
        "plan": _plan_shape(plan["Plan"]),
        "rows": int(plan["Plan"]["Actual Rows"]),
    }


def _plan_shape(plan: dict[str, Any]) -> str:
    nodes = [str(plan["Node Type"])]
    for child in plan.get("Plans", []):
        nodes.append(_plan_shape(child))
    return ">".join(nodes)


def _fetch_timeline(cur: psycopg.Cursor[Any], *, limit: int | None) -> dict[str, Any]:
    query = "SELECT sequence, payload FROM bench_events WHERE run_id = 1 AND sequence > 0 ORDER BY sequence"
    if limit is not None:
        query += f" LIMIT {limit}"
    samples = []
    result = []
    for _ in range(REPEATS):
        started = time.perf_counter()
        cur.execute(query)
        result = cur.fetchall()
        samples.append((time.perf_counter() - started) * 1000)
    return {
        "median_fetch_ms": statistics.median(samples),
        "rows_materialized": len(result),
        "payload_bytes_materialized": sum(len(payload) for _, payload in result),
    }


def main() -> None:
    url = os.environ.get("BENCH_DATABASE_URL")
    if not url:
        raise SystemExit("BENCH_DATABASE_URL must point to an isolated PostgreSQL instance")
    with psycopg.connect(url) as connection, connection.cursor() as cur:
        cur.execute("SET jit = off")
        cur.execute(
            """CREATE TEMP TABLE bench_runs (
                id uuid PRIMARY KEY,
                tenant_id integer NOT NULL,
                status text NOT NULL,
                created_at timestamptz NOT NULL,
                payload text NOT NULL
            )"""
        )
        cur.execute(
            """INSERT INTO bench_runs
                SELECT md5(i::text)::uuid, i %% 5,
                       CASE i %% 50 WHEN 0 THEN 'pending'
                                   WHEN 1 THEN 'running'
                                   WHEN 2 THEN 'waiting_approval'
                                   ELSE 'succeeded' END,
                       '2025-01-01'::timestamptz + ((i / 4) * interval '1 second'),
                       repeat('x', 256)
                FROM generate_series(1, %s) AS i""",
            (RUNS,),
        )
        cur.execute("CREATE INDEX bench_runs_tenant ON bench_runs(tenant_id)")
        cur.execute("CREATE INDEX bench_runs_status ON bench_runs(status)")
        cur.execute("ANALYZE bench_runs")
        cur.execute(
            """CREATE TEMP TABLE bench_events (
                run_id integer NOT NULL,
                sequence integer NOT NULL,
                payload text NOT NULL,
                UNIQUE(run_id, sequence)
            )"""
        )
        cur.execute(
            "INSERT INTO bench_events SELECT 1, i, repeat('x', 768) FROM generate_series(1, %s) AS i",
            (EVENTS,),
        )
        cur.execute("ANALYZE bench_events")
        recent = """SELECT * FROM bench_runs
                    WHERE tenant_id = 1
                    ORDER BY created_at DESC, id DESC LIMIT 100"""
        recovery = """SELECT * FROM bench_runs
                      WHERE tenant_id = 1
                        AND status IN ('pending', 'running', 'waiting_approval')
                      ORDER BY created_at, id LIMIT 100"""
        before = {
            "recent_runs": _explain(cur, recent),
            "recovery_runs": _explain(cur, recovery),
            "timeline": _fetch_timeline(cur, limit=None),
        }
        cur.execute(
            "CREATE INDEX bench_runs_tenant_created_id ON bench_runs(tenant_id, created_at, id)"
        )
        cur.execute(
            """CREATE INDEX bench_runs_recovery ON bench_runs(tenant_id, created_at, id)
               WHERE status IN ('pending', 'running', 'waiting_approval')"""
        )
        cur.execute("ANALYZE bench_runs")
        after = {
            "recent_runs": _explain(cur, recent),
            "recovery_runs": _explain(cur, recovery),
            "timeline": _fetch_timeline(cur, limit=100),
        }
        print(
            json.dumps(
                {
                    "postgres": connection.info.server_version,
                    "data_shape": {
                        "runs": RUNS,
                        "tenants": 5,
                        "live_run_fraction": 0.06,
                        "events_in_timeline": EVENTS,
                        "event_payload_bytes": 768,
                        "repeats": REPEATS,
                    },
                    "before": before,
                    "after": after,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
