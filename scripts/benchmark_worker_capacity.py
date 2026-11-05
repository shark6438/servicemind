"""Reproduce Worker capacity and process-fault observations without paid providers.

The driver creates its own disposable PostgreSQL database and runs the real HTTP
admission endpoint plus real RunWorker/RunQueue in separate processes. Only auth
and the graph body are replaced: the graph performs fixed asynchronous waits and
one idempotent synthetic database effect. This measures orchestration overhead and
isolation, not LLM quality, GLPI behavior, or production capacity.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import platform
import signal
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, LiteralString
from uuid import UUID, uuid4

import httpx
import psycopg
from psycopg.rows import dict_row
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
APP_USER = "capacity_app"
APP_PASSWORD = "capacity-app-local"
ADMIN_PASSWORD = "capacity-admin-local"
DATABASE = "capacity"
FAKE_MODEL_SECONDS = 0.25
FAKE_TOOL_SECONDS = 0.05
TERMINAL = {"succeeded", "failed", "cancelled"}


def percentile(values: list[float], fraction: float) -> float:
    """Linear interpolation, equivalent to the common inclusive sample quantile."""
    if not values:
        raise ValueError("percentile requires observations")
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _stats(values: list[float]) -> dict[str, float]:
    return {
        "p50": round(percentile(values, 0.5), 6),
        "p95": round(percentile(values, 0.95), 6),
        "max": round(max(values), 6),
    }


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _environment(port: int, *, admin: bool = False) -> dict[str, str]:
    user, password = ("postgres", ADMIN_PASSWORD) if admin else (APP_USER, APP_PASSWORD)
    url = f"postgresql+psycopg://{user}:{password}@127.0.0.1:{port}/{DATABASE}"
    return os.environ | {
        "PYTHON_DOTENV_DISABLED": "1",
        "SERVICEMIND_DATABASE_URL": url,
        "SERVICEMIND_MIGRATION_DATABASE_URL": (
            f"postgresql+psycopg://postgres:{ADMIN_PASSWORD}@127.0.0.1:{port}/{DATABASE}"
        ),
        "OPENAI_API_KEY": "sk-fake-capacity-benchmark",
        "SERVICEMIND_JEV_ROUTER_MODE": "off",
        "SERVICEMIND_OTEL_ENDPOINT": "",
        "USE_FAKE_MODEL": "true",
    }


def _bootstrap(container: str, port: int) -> None:
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "-p",
            f"127.0.0.1:{port}:5432",
            "-e",
            f"POSTGRES_PASSWORD={ADMIN_PASSWORD}",
            "-e",
            f"POSTGRES_DB={DATABASE}",
            "postgres:16",
        ],
        check=True,
        capture_output=True,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(
                host="127.0.0.1",
                port=port,
                dbname=DATABASE,
                user="postgres",
                password=ADMIN_PASSWORD,
                connect_timeout=1,
            ):
                break
        except psycopg.OperationalError:
            time.sleep(0.2)
    else:
        raise RuntimeError("Disposable PostgreSQL did not become ready")
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=_environment(port, admin=True),
        check=True,
        capture_output=True,
    )
    with (
        psycopg.connect(
            host="127.0.0.1",
            port=port,
            dbname=DATABASE,
            user="postgres",
            password=ADMIN_PASSWORD,
        ) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "CREATE ROLE capacity_app LOGIN PASSWORD 'capacity-app-local' NOSUPERUSER NOBYPASSRLS"
        )
        cursor.execute(
            """CREATE TABLE benchmark_effects (
                run_id uuid PRIMARY KEY REFERENCES agent_runs(id),
                tenant_id uuid NOT NULL,
                created_at timestamptz NOT NULL DEFAULT clock_timestamp()
            )"""
        )
        cursor.execute("ALTER TABLE benchmark_effects ENABLE ROW LEVEL SECURITY")
        cursor.execute("ALTER TABLE benchmark_effects FORCE ROW LEVEL SECURITY")
        cursor.execute(
            """CREATE POLICY benchmark_effect_tenant ON benchmark_effects
               USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
               WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)"""
        )
        cursor.execute("GRANT USAGE ON SCHEMA public TO capacity_app")
        cursor.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO capacity_app"
        )
        cursor.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO capacity_app")


async def _api_child(port: int) -> None:
    import uvicorn
    from fastapi import FastAPI

    from servicemind.api import phase2_router
    from servicemind.observability.http import HttpMetricsMiddleware
    from servicemind.orchestration.execution_config import (
        RunExecutionConfig,
        configure_run_execution,
    )
    from servicemind.security.auth import TenantContext, get_tenant_context

    app = FastAPI()
    app.add_middleware(HttpMetricsMiddleware)
    app.include_router(phase2_router)
    configure_run_execution(
        RunExecutionConfig(mode="worker", per_tenant_limit=4, queue_limit=10000)
    )

    def context(request: Request) -> TenantContext:
        return TenantContext(
            tenant_id=UUID(request.headers["X-Benchmark-Tenant"]),
            user_id="benchmark-user",
            username="benchmark-user",
            roles={"viewer", "analyst"},
            allowed_glpi_entity_ids={1},
        )

    app.dependency_overrides[get_tenant_context] = context

    @app.get("/health")
    async def health():
        return {"status": "ok", "pid": os.getpid()}

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    await server.serve()


async def _worker_child(args: argparse.Namespace) -> None:
    from sqlalchemy import text

    from servicemind.orchestration.run_worker import RunWorker
    from servicemind.persistence.database import close_database, tenant_session
    from servicemind.persistence.models import RunStatus
    from servicemind.persistence.repository import ServiceMindRepository
    from servicemind.persistence.run_queue import guard_engine

    class FixedGraphWorker(RunWorker):
        async def _execute_graph(self, run) -> None:
            repository = ServiceMindRepository(run.tenant_id)
            await repository.update_run(run.id, RunStatus.RUNNING)
            await repository.append_event(
                run.id,
                "benchmark.started",
                {"worker_pid": os.getpid(), "worker_id": self.worker_id},
            )
            # A longer model wait is used only in the process-fault scenarios.
            model_seconds = 2.0 if run.goal.startswith("fault:") else FAKE_MODEL_SECONDS
            await asyncio.sleep(model_seconds)
            await asyncio.sleep(FAKE_TOOL_SECONDS)
            await asyncio.sleep(FAKE_TOOL_SECONDS)
            async with tenant_session(run.tenant_id) as session:
                await session.execute(
                    text(
                        "INSERT INTO benchmark_effects (run_id, tenant_id) VALUES (:run, :tenant) "
                        "ON CONFLICT (run_id) DO NOTHING"
                    ),
                    {"run": run.id, "tenant": run.tenant_id},
                )
            await repository.update_run(run.id, RunStatus.SUCCEEDED, result={"benchmark": True})
            await repository.append_event(run.id, "run.succeeded", {"status": "succeeded"})

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    worker = FixedGraphWorker(
        concurrency=args.concurrency,
        per_tenant_limit=args.tenant_limit,
        lease_seconds=args.lease_seconds,
        poll_seconds=0.02,
        worker_id=f"benchmark-worker-{os.getpid()}",
        shutdown_timeout=5,
    )
    print("READY", flush=True)
    try:
        await worker.run(stop)
    finally:
        await close_database()
        await guard_engine.dispose()


class Experiment:
    def __init__(self, port: int, output: Path) -> None:
        self.port = port
        self.output = output
        self.api_port = _free_port()
        self.api: asyncio.subprocess.Process | None = None
        self.workers: list[asyncio.subprocess.Process] = []
        self.connection: psycopg.AsyncConnection[Any] | None = None
        self.logs: list[Any] = []
        self.samples: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []

    async def sql(self, statement: LiteralString, params: Any = None) -> list[dict[str, Any]]:
        assert self.connection is not None
        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(statement, params)
            return list(await cursor.fetchall()) if cursor.description else []

    async def connect(self) -> None:
        self.connection = await psycopg.AsyncConnection.connect(
            host="127.0.0.1",
            port=self.port,
            dbname=DATABASE,
            user="postgres",
            password=ADMIN_PASSWORD,
            autocommit=True,
        )

    async def spawn(self, mode: str, *options: str) -> asyncio.subprocess.Process:
        logfile = (self.output / f"{mode}-{time.time_ns()}.log").open("wb")
        self.logs.append(logfile)
        return await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(__file__).resolve()),
            mode,
            *options,
            cwd=ROOT,
            env=_environment(self.port),
            stdout=asyncio.subprocess.PIPE,
            stderr=logfile,
        )

    async def start_api(self) -> None:
        self.api = await self.spawn("api", "--port", str(self.api_port))
        async with httpx.AsyncClient(trust_env=False) as client:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if self.api.returncode is not None:
                    raise RuntimeError("Benchmark API exited before readiness")
                try:
                    response = await client.get(f"http://127.0.0.1:{self.api_port}/health")
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.1)
        raise RuntimeError("Benchmark API readiness timed out")

    async def start_workers(self, count: int, concurrency: int, tenant_limit: int) -> None:
        self.workers = []
        for _ in range(count):
            process = await self.spawn(
                "worker",
                "--concurrency",
                str(concurrency),
                "--tenant-limit",
                str(tenant_limit),
                "--lease-seconds",
                "3",
            )
            assert process.stdout is not None
            if await asyncio.wait_for(process.stdout.readline(), 45) != b"READY\n":
                raise RuntimeError("Benchmark Worker failed readiness")
            self.workers.append(process)

    @staticmethod
    async def stop_process(process: asyncio.subprocess.Process | None) -> None:
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 10)
        except TimeoutError:
            process.kill()
            await process.wait()

    async def stop_workers(self) -> None:
        for process in self.workers:
            await self.stop_process(process)
        self.workers = []

    async def reset(self) -> tuple[str, str]:
        # This is the disposable database created by this script, never a supplied URL.
        await self.sql("TRUNCATE tenants, agent_runs, run_events, benchmark_effects CASCADE")
        tenants = (str(uuid4()), str(uuid4()))
        for index, tenant in enumerate(tenants):
            await self.sql(
                "INSERT INTO tenants(id, slug, name) VALUES (%s, %s, %s)",
                (tenant, f"benchmark-{index}", f"Synthetic tenant {index}"),
            )
        return tenants

    async def submit(
        self,
        client: httpx.AsyncClient,
        tenant: str,
        index: int,
        scenario: str,
        *,
        fault: bool = False,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        sent_at = time.time()
        async with client.stream(
            "POST",
            f"http://127.0.0.1:{self.api_port}/v1/servicemind/runs",
            headers={"X-Benchmark-Tenant": tenant},
            json={
                "ticket_id": index + 1,
                "goal": f"{'fault' if fault else 'capacity'}:{scenario}:{index}",
                "request_write": False,
            },
        ) as response:
            header_ms = (time.perf_counter() - started) * 1000
            body = json.loads(await response.aread())
        observation = {
            "scenario": scenario,
            "index": index,
            "tenant_id": tenant,
            "sent_at_epoch": sent_at,
            "response_header_ms": header_ms,
            "http_status": response.status_code,
            "run_id": body.get("id"),
        }
        self.requests.append(observation)
        if response.status_code != 202:
            raise RuntimeError(f"Admission returned HTTP {response.status_code}: {body}")
        return observation

    async def rows(self) -> list[dict[str, Any]]:
        return await self.sql(
            """SELECT r.id::text AS run_id, r.tenant_id::text AS tenant_id, r.status,
                extract(epoch FROM r.created_at)::float8 AS created_at_epoch,
                extract(epoch FROM min(e.created_at) FILTER(WHERE e.event_type='benchmark.started'))::float8 AS started_at_epoch,
                extract(epoch FROM max(e.created_at) FILTER(WHERE e.event_type='run.succeeded'))::float8 AS finished_at_epoch,
                count(e.id) FILTER(WHERE e.event_type='benchmark.started')::int AS attempts,
                (SELECT count(*) FROM benchmark_effects b WHERE b.run_id=r.id)::int AS effects
                FROM agent_runs r LEFT JOIN run_events e ON e.run_id=r.id
                GROUP BY r.id ORDER BY r.created_at, r.id"""
        )

    async def wait_complete(self, expected: int, *, timeout: float = 120) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = await self.rows()
            if len(rows) == expected and all(row["finished_at_epoch"] is not None for row in rows):
                return rows
            if any(process.returncode not in (None, -signal.SIGKILL) for process in self.workers):
                raise RuntimeError("A Worker exited unexpectedly")
            await asyncio.sleep(0.1)
        raise RuntimeError(f"Tasks did not drain: {expected} expected")

    async def sample(self, scenario: str, stop: asyncio.Event) -> None:
        while not stop.is_set():
            rows = await self.sql(
                "SELECT status, count(*)::int AS count FROM agent_runs GROUP BY status"
            )
            self.samples.append(
                {
                    "scenario": scenario,
                    "epoch": time.time(),
                    "counts": {row["status"]: row["count"] for row in rows},
                }
            )
            try:
                await asyncio.wait_for(stop.wait(), 1)
            except TimeoutError:
                pass

    @staticmethod
    def summarize(rows: list[dict[str, Any]], requests: list[dict[str, Any]]) -> dict[str, Any]:
        start = min(request["sent_at_epoch"] for request in requests)
        finish = max(row["finished_at_epoch"] for row in rows)
        last_request = max(request["sent_at_epoch"] for request in requests)
        return {
            "accepted": len(requests),
            "succeeded": sum(row["status"] == "succeeded" for row in rows),
            "synthetic_effects": sum(row["effects"] for row in rows),
            "attempts": sum(row["attempts"] for row in rows),
            "elapsed_seconds": round(finish - start, 6),
            "throughput_per_second": round(len(rows) / (finish - start), 6),
            "drain_after_last_submission_seconds": round(max(finish - last_request, 0), 6),
            "api_response_header_ms": _stats(
                [request["response_header_ms"] for request in requests]
            ),
            "queue_wait_seconds": _stats(
                [row["started_at_epoch"] - row["created_at_epoch"] for row in rows]
            ),
            "run_latency_seconds": _stats(
                [row["finished_at_epoch"] - row["created_at_epoch"] for row in rows]
            ),
            "steady_five_second_completion_windows": [
                {
                    "from_second": offset,
                    "completed": sum(
                        start + offset <= row["finished_at_epoch"] < start + offset + 5
                        for row in rows
                    ),
                }
                for offset in (5, 10, 15)
            ],
        }

    async def capacity(self, count: int, offer_seconds: int, offer_rate: int) -> dict[str, Any]:
        scenario = f"capacity_{count}_workers"
        tenants = await self.reset()
        await self.start_workers(count, concurrency=2, tenant_limit=4)
        async with httpx.AsyncClient(trust_env=False, timeout=15) as client:
            # Warm the API, query pools and worker paths before collecting the fixed workload.
            warmups = await asyncio.gather(
                *[self.submit(client, tenants[i % 2], i, "warmup") for i in range(4)]
            )
            await self.wait_complete(len(warmups))
            await self.sql("TRUNCATE agent_runs, run_events, benchmark_effects CASCADE")
            self.requests = [
                request for request in self.requests if request["scenario"] != "warmup"
            ]
            stop = asyncio.Event()
            sampler = asyncio.create_task(self.sample(scenario, stop))
            begin = time.monotonic()
            pending = []
            for index in range(offer_seconds * offer_rate):
                await asyncio.sleep(max(begin + index / offer_rate - time.monotonic(), 0))
                pending.append(
                    asyncio.create_task(self.submit(client, tenants[index % 2], index, scenario))
                )
            requests = await asyncio.gather(*pending)
            rows = await self.wait_complete(len(requests))
            stop.set()
            await sampler
        result = self.summarize(rows, requests)
        result |= {
            "workers": count,
            "per_worker_concurrency": 2,
            "per_tenant_execution_limit": 4,
            "offer_seconds": offer_seconds,
            "offered_per_second": offer_rate,
            "runs": rows,
        }
        await self.stop_workers()
        return result

    async def fairness(self) -> dict[str, Any]:
        scenario = "noisy_tenant_isolation"
        noisy, steady = await self.reset()
        await self.start_workers(2, concurrency=1, tenant_limit=1)
        async with httpx.AsyncClient(trust_env=False, timeout=15) as client:
            noisy_requests = await asyncio.gather(
                *[self.submit(client, noisy, i, scenario) for i in range(24)]
            )
            steady_requests = []
            for index in range(8):
                steady_requests.append(await self.submit(client, steady, index + 24, scenario))
                await asyncio.sleep(0.5)
            rows = await self.wait_complete(32)
        by_tenant = {}
        for label, tenant, requests in (
            ("noisy", noisy, noisy_requests),
            ("steady", steady, steady_requests),
        ):
            selected = [row for row in rows if row["tenant_id"] == tenant]
            events = [(row["started_at_epoch"], 1) for row in selected] + [
                (row["finished_at_epoch"], -1) for row in selected
            ]
            active = maximum = 0
            for _, change in sorted(events, key=lambda event: (event[0], event[1])):
                active += change
                maximum = max(maximum, active)
            by_tenant[label] = self.summarize(selected, requests) | {
                "max_observed_graph_concurrency": maximum
            }
        await self.stop_workers()
        return {"per_tenant_limit": 1, "workers": 2, "tenants": by_tenant, "runs": rows}

    async def wait_started(self, run_id: str) -> list[dict[str, Any]]:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            rows = await self.sql(
                "SELECT payload, extract(epoch FROM created_at)::float8 AS epoch FROM run_events WHERE run_id=%s AND event_type='benchmark.started' ORDER BY sequence",
                (run_id,),
            )
            if rows:
                return rows
            await asyncio.sleep(0.05)
        raise RuntimeError("Task did not start")

    async def api_restart(self) -> dict[str, Any]:
        tenant, _ = await self.reset()
        await self.start_workers(1, concurrency=1, tenant_limit=2)
        async with httpx.AsyncClient(trust_env=False, timeout=15) as client:
            request = await self.submit(client, tenant, 1, "api_restart", fault=True)
        await self.wait_started(request["run_id"])
        assert self.api is not None
        old_pid = self.api.pid
        killed_at = time.time()
        self.api.kill()
        await self.api.wait()
        await self.start_api()
        ready_at = time.time()
        rows = await self.wait_complete(1)
        await self.stop_workers()
        return {
            "old_api_pid": old_pid,
            "new_api_pid": self.api.pid,
            "api_unavailable_seconds": ready_at - killed_at,
            "completed_while_api_unavailable": killed_at
            <= rows[0]["finished_at_epoch"]
            <= ready_at,
            "attempts": rows[0]["attempts"],
            "synthetic_effects": rows[0]["effects"],
            "run": rows[0],
        }

    async def worker_kill(self) -> dict[str, Any]:
        tenant, _ = await self.reset()
        await self.start_workers(2, concurrency=1, tenant_limit=2)
        async with httpx.AsyncClient(trust_env=False, timeout=15) as client:
            request = await self.submit(client, tenant, 2, "worker_sigkill", fault=True)
        first = (await self.wait_started(request["run_id"]))[0]
        killed_pid = first["payload"]["worker_pid"]
        killed = next(process for process in self.workers if process.pid == killed_pid)
        killed_at = time.time()
        killed.kill()
        await killed.wait()
        rows = await self.wait_complete(1, timeout=30)
        starts = await self.sql(
            "SELECT payload, extract(epoch FROM created_at)::float8 AS epoch FROM run_events WHERE run_id=%s AND event_type='benchmark.started' ORDER BY sequence",
            (request["run_id"],),
        )
        result = {
            "killed_worker_pid": killed_pid,
            "signal": "SIGKILL",
            "test_lease_seconds": 3,
            "attempts": rows[0]["attempts"],
            "synthetic_effects": rows[0]["effects"],
            "reclaimed_after_kill_seconds": starts[1]["epoch"] - killed_at
            if len(starts) > 1
            else None,
            "finished_after_kill_seconds": rows[0]["finished_at_epoch"] - killed_at,
            "start_events": starts,
            "run": rows[0],
        }
        await self.stop_workers()
        return result

    async def close(self) -> None:
        await self.stop_workers()
        await self.stop_process(self.api)
        if self.connection is not None:
            await self.connection.close()
        for logfile in self.logs:
            logfile.close()


async def _driver(args: argparse.Namespace) -> None:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output = (
        args.output or ROOT / f"evaluation/experiments/worker_capacity_v1/run_{timestamp}"
    ).resolve()
    output.mkdir(parents=True, exist_ok=False)
    container = f"servicemind-capacity-{os.getpid()}"
    port = _free_port()
    sources = [
        "scripts/benchmark_worker_capacity.py",
        "src/servicemind/orchestration/run_worker.py",
        "src/servicemind/persistence/run_queue.py",
        "src/servicemind/api.py",
        "src/servicemind/persistence/repository.py",
    ]
    plan = {
        "schema_version": 1,
        "seed": 20261105,
        "capacity": {
            "workers": [1, 2],
            "per_worker_concurrency": 2,
            "per_tenant_limit": 4,
            "tenants": 2,
            "offer_seconds": args.offer_seconds,
            "offer_rate_per_second": args.offer_rate,
        },
        "fake_graph": {
            "model_wait_seconds": FAKE_MODEL_SECONDS,
            "tool_wait_seconds": FAKE_TOOL_SECONDS,
            "tool_wait_count": 2,
            "synthetic_idempotent_effect": True,
        },
        "fairness": {
            "noisy_burst": 24,
            "steady_requests": 8,
            "steady_interval_seconds": 0.5,
            "per_tenant_limit": 1,
        },
        "faults": ["api_process_SIGKILL", "worker_process_SIGKILL"],
        "source_sha256": {
            path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in sources
        },
    }
    (output / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    experiment = Experiment(port, output)
    try:
        _bootstrap(container, port)
        print(
            json.dumps(
                {
                    "container": container,
                    "port": port,
                    "database": DATABASE,
                    "application_role": APP_USER,
                    "output": str(output),
                }
            ),
            flush=True,
        )
        await experiment.connect()
        await experiment.start_api()
        one = await experiment.capacity(1, args.offer_seconds, args.offer_rate)
        print("capacity 1 Worker complete", flush=True)
        two = await experiment.capacity(2, args.offer_seconds, args.offer_rate)
        print("capacity 2 Workers complete", flush=True)
        fairness = await experiment.fairness()
        print("tenant isolation complete", flush=True)
        api_restart = await experiment.api_restart()
        print("API restart complete", flush=True)
        worker_kill = await experiment.worker_kill()
        print("Worker SIGKILL complete", flush=True)
        postgres = (await experiment.sql("SHOW server_version"))[0]["server_version"]
        checks = {
            "all_capacity_requests_accepted_and_completed": all(
                row["accepted"] == row["succeeded"] == row["synthetic_effects"]
                for row in (one, two)
            ),
            "no_duplicate_capacity_attempts": all(
                row["attempts"] == row["accepted"] for row in (one, two)
            ),
            "tenant_limits_respected": all(
                row["max_observed_graph_concurrency"] <= 1 for row in fairness["tenants"].values()
            ),
            "api_restart_does_not_restart_task": api_restart["attempts"]
            == api_restart["synthetic_effects"]
            == 1,
            "worker_kill_recovered_once": worker_kill["attempts"] == 2
            and worker_kill["synthetic_effects"] == 1,
        }
        result = {
            "schema_version": 1,
            "observed_at": datetime.now(UTC).isoformat(),
            "plan_sha256": hashlib.sha256((output / "plan.json").read_bytes()).hexdigest(),
            "environment": {
                "python": sys.version,
                "postgres": postgres,
                "platform": platform.platform(),
                "logical_cpus": os.cpu_count(),
                "cpu_affinity_count": len(os.sched_getaffinity(0)),
                "load_average": os.getloadavg(),
                "container": container,
                "postgres_port": port,
                "database_role": "NOSUPERUSER NOBYPASSRLS",
            },
            "capacity": [one, two],
            "throughput_ratio_two_over_one": two["throughput_per_second"]
            / one["throughput_per_second"],
            "fairness": fairness,
            "api_restart": api_restart,
            "worker_sigkill": worker_kill,
            "checks": checks,
            "paid_model_calls": 0,
            "provider_cost_usd": 0,
            "limitations": [
                "Single host, short controlled synthetic workload; not certified production capacity.",
                "Real HTTP admission, PostgreSQL RLS, Worker scheduling, lease, locks and heartbeat are used. Authentication and graph body are replaced, so this excludes model quality, identity-provider latency, actual LangGraph checkpoint recovery and GLPI writes.",
                "The synthetic effect has its own unique run_id constraint; it does not prove external systems execute exactly once.",
                "API response-header timing excludes background execution and response-body transfer.",
                "Fault recovery uses a three-second test lease, not the production default120seconds.",
                "Results depend on host load and data shape; throughput improvement is observed rather than a preregistered expected value.",
            ],
        }
        (output / "requests.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in experiment.requests)
        )
        (output / "backlog.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in experiment.samples)
        )
        (output / "report.json").write_text(json.dumps(result, indent=2) + "\n")
        latest = ROOT / "evaluation/reports/worker_capacity_v1.json"
        latest.write_text(
            json.dumps(
                {"artifact": str((output / "report.json").relative_to(ROOT)), "report": result},
                indent=2,
            )
            + "\n"
        )
        print(
            json.dumps(
                {
                    "checks": checks,
                    "throughput_ratio": result["throughput_ratio_two_over_one"],
                    "report": str(output / "report.json"),
                }
            ),
            flush=True,
        )
        if not all(checks.values()):
            raise RuntimeError("A benchmark correctness check failed; report retained")
    finally:
        await experiment.close()
        if not args.keep_postgres:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    driver = commands.add_parser("run")
    driver.add_argument("--offer-seconds", type=int, default=20)
    driver.add_argument("--offer-rate", type=int, default=12)
    driver.add_argument("--output", type=Path)
    driver.add_argument("--keep-postgres", action="store_true")
    api = commands.add_parser("api")
    api.add_argument("--port", type=int, required=True)
    worker = commands.add_parser("worker")
    worker.add_argument("--concurrency", type=int, required=True)
    worker.add_argument("--tenant-limit", type=int, required=True)
    worker.add_argument("--lease-seconds", type=int, required=True)
    args = parser.parse_args()
    if args.mode == "run":
        if args.offer_seconds < 5 or args.offer_rate < 1:
            parser.error("offer-seconds must be>=5 and offer-rate>=1")
        asyncio.run(_driver(args))
    elif args.mode == "api":
        asyncio.run(_api_child(args.port))
    else:
        asyncio.run(_worker_child(args))


if __name__ == "__main__":
    main()
