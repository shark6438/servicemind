"""Run a zero-fee model control with private, disposable persistence and wire traces."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

from dotenv import dotenv_values
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
STATE = Path(
    os.environ.get(
        "LOCAL_EVALUATION_STATE", Path.home() / ".local/state/servicemind/local-evaluation-r3"
    )
)
BATCH = Path(
    os.environ.get("LOCAL_EVALUATION_BATCH", ROOT / "evaluation/security/completion_2026-10-22_r3")
)
PREFIX = "servicemind-local-eval-r3-"
MODEL = "security-local-qwen-instruct"
API_PORT = 18083
PROXY_PORT = 11441


def save(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def command(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(args, check=False, capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise RuntimeError(f"{Path(args[0]).name} failed with exit {result.returncode}")
    return result


def environment() -> dict[str, str]:
    env = json.loads((STATE / "environment.json").read_text())
    manifest = json.loads((BATCH / "isolation-manifest.json").read_text())
    from urllib.parse import urlsplit

    expected = {
        "SERVICEMIND_DATABASE_URL": ("pg", "/local_evaluation"),
        "SERVICEMIND_MIGRATION_DATABASE_URL": ("pg", "/local_evaluation"),
        "SERVICEMIND_REDIS_URL": ("redis", ""),
        "NEO4J_URI": ("neo4j", ""),
        "SERVICEMIND_OPENSEARCH_URL": ("os", ""),
    }
    for name, (service, path) in expected.items():
        parsed = urlsplit(env[name])
        if (parsed.hostname, parsed.port, parsed.path) != (
            "127.0.0.1",
            manifest["ports"][service],
            path,
        ):
            raise RuntimeError(f"{name} does not point at the disposable evaluation service")
    if (
        env["POSTGRES_PORT"] != str(manifest["ports"]["pg"])
        or env["POSTGRES_DB"] != "local_evaluation"
    ):
        raise RuntimeError("Checkpoint persistence is not isolated")
    if (
        env["COMPATIBLE_BASE_URL"] != f"http://127.0.0.1:{PROXY_PORT}/v1"
        or env["COMPATIBLE_MODEL"] != MODEL
    ):
        raise RuntimeError("Evaluation requires the fixed local inference observer")
    if (
        env["DEFAULT_MODEL"] != "openai-compatible"
        or env["SERVICEMIND_MODEL_ALLOWED_MODELS"] != MODEL
        or env["SERVICEMIND_MODEL_ALLOWED_PROVIDERS"] != "openai"
    ):
        raise RuntimeError("Evaluation model allowlist was changed")
    return env


def register_process(role: str) -> None:
    pid = os.getpid()
    save(
        STATE / f"{role}-process.json",
        {
            "pid": pid,
            "start_ticks": Path(f"/proc/{pid}/stat").read_text().split()[21],
        },
    )


def start_model() -> int:
    source = json.loads(command(["docker", "inspect", "servicemind-security-llama"]).stdout)[0]
    args = source["Config"]["Cmd"]
    if MODEL not in args or "--api-key-file" not in args or "--api-key" in args:
        raise RuntimeError("Expected the authenticated, qualified local model runtime")
    mounts = []
    for mount in source["Mounts"]:
        if mount["RW"]:
            raise RuntimeError("Model resources must be mounted read-only")
        location = mount.get("Name") if mount["Type"] == "volume" else mount["Source"]
        mounts.extend(
            ["--mount", f"type={mount['Type']},src={location},dst={mount['Destination']},readonly"]
        )
    name = PREFIX + "model"
    command(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--label",
            "servicemind.purpose=local-evaluation-r3",
            "--cpus",
            "12",
            "--memory",
            "32g",
            "--read-only",
            "--tmpfs",
            "/tmp:size=512m,mode=1777",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            source["Config"]["User"],
            "-p",
            "127.0.0.1::8080",
            *mounts,
            source["Image"],
            *args,
        ]
    )
    port = int(command(["docker", "port", name, "8080"]).stdout.rsplit(":", 1)[1])
    save(
        BATCH / "dedicated-model-runtime.json",
        {
            "name": name,
            "image_id": source["Image"],
            "qualified_reference_container": "servicemind-security-llama",
            "port": port,
            "model_command": args,
            "resources_read_only": True,
            "shares_inference_process": False,
            "preserved_named_model_volume": "servicemind-security-models",
        },
    )
    return port


def cleanup() -> None:
    for role in ("api", "proxy"):
        path = STATE / f"{role}-process.json"
        if not path.exists():
            continue
        recorded = json.loads(path.read_text())
        proc = Path(f"/proc/{recorded['pid']}")
        if (
            proc.exists()
            and proc.joinpath("stat").read_text().split()[21] == recorded["start_ticks"]
        ):
            cmdline = proc.joinpath("cmdline").read_bytes().decode().replace("\0", " ")
            if str(Path(__file__).resolve()) not in cmdline or f" {role}" not in cmdline:
                raise RuntimeError("Refusing to stop a process outside this evaluation")
            os.kill(recorded["pid"], signal.SIGTERM)
            for _ in range(100):
                if not proc.exists():
                    break
                time.sleep(0.1)
            if proc.exists():
                raise RuntimeError("Evaluation process did not stop; persistence was retained")
        path.unlink()
    removed = []
    for key in ("pg", "redis", "neo4j", "os", "model"):
        name = PREFIX + key
        inspected = subprocess.run(["docker", "inspect", name], capture_output=True, text=True)
        if inspected.returncode:
            continue
        metadata = json.loads(inspected.stdout)[0]
        if metadata["Config"]["Labels"].get("servicemind.purpose") != "local-evaluation-r3":
            raise RuntimeError("Refusing to delete an unowned container")
        removed.extend(
            m["Name"]
            for m in metadata["Mounts"]
            if m["Type"] == "volume" and m["Name"] != "servicemind-security-models"
        )
        command(["docker", "rm", "-fv", name])
    (STATE / "environment.json").unlink(missing_ok=True)
    save(
        BATCH / "cleanup.json",
        {
            "containers_removed": True,
            "anonymous_volumes_removed": removed,
            "private_environment_removed": True,
            "production_services_modified": False,
        },
    )


def setup() -> None:
    if (BATCH / "SHA256SUMS.json").exists():
        raise RuntimeError("Evidence is sealed; choose a fresh LOCAL_EVALUATION_BATCH directory")
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE.chmod(0o700)
    BATCH.mkdir(parents=True, exist_ok=True)
    if (STATE / "environment.json").exists():
        raise RuntimeError("An evaluation already exists; inspect it or clean it up first")
    password = secrets.token_urlsafe(24)
    configs = {
        "pg": (
            "postgres:16",
            5432,
            [
                "-e",
                "POSTGRES_USER=eval_admin",
                "-e",
                f"POSTGRES_PASSWORD={password}",
                "-e",
                "POSTGRES_DB=local_evaluation",
                "--memory",
                "768m",
            ],
        ),
        "redis": ("redis:8.2-alpine", 6379, ["--memory", "256m"]),
        "neo4j": (
            "neo4j:5.26",
            7687,
            [
                "-e",
                f"NEO4J_AUTH=neo4j/{password}",
                "-e",
                "NEO4J_server_memory_heap_initial__size=256m",
                "-e",
                "NEO4J_server_memory_heap_max__size=512m",
                "-e",
                "NEO4J_server_memory_pagecache_size=256m",
                "--memory",
                "1536m",
            ],
        ),
        "os": (
            "opensearchproject/opensearch:3.8.0",
            9200,
            [
                "-e",
                "discovery.type=single-node",
                "-e",
                "DISABLE_SECURITY_PLUGIN=true",
                "-e",
                "DISABLE_INSTALL_DEMO_CONFIG=true",
                "-e",
                "OPENSEARCH_JAVA_OPTS=-Xms512m -Xmx512m",
                "--memory",
                "2g",
            ],
        ),
    }
    ports = {}
    for key, (image, port, opts) in configs.items():
        command(
            [
                "docker",
                "run",
                "-d",
                "--name",
                PREFIX + key,
                "--label",
                "servicemind.purpose=local-evaluation-r3",
                "--cpus",
                "2",
                "-p",
                f"127.0.0.1::{port}",
                *opts,
                image,
            ]
        )
        ports[key] = int(
            command(["docker", "port", PREFIX + key, str(port)]).stdout.rsplit(":", 1)[1]
        )
    ports["model"] = start_model()
    source = dotenv_values(ROOT / ".env")
    retained = ("GLPI_", "SERVICEMIND_", "AUTH_SECRET")
    env = {k: v for k, v in source.items() if v is not None and k.startswith(retained)}
    env.update(
        {
            "PATH": os.environ["PATH"],
            "HOME": str(STATE),
            "PYTHONPATH": f"{ROOT / 'src'}:{ROOT / 'scripts'}",
            "PYTHON_DOTENV_DISABLED": "1",
            "DEFAULT_MODEL": "openai-compatible",
            "COMPATIBLE_MODEL": MODEL,
            "COMPATIBLE_BASE_URL": f"http://127.0.0.1:{PROXY_PORT}/v1",
            "COMPATIBLE_API_KEY": (Path.home() / ".config/servicemind/local-model.key")
            .read_text()
            .strip(),
            "USE_FAKE_MODEL": "false",
            "SERVICEMIND_GENERIC_API_ENABLED": "false",
            "SERVICEMIND_MODEL_ALLOWED_PROVIDERS": "openai",
            "SERVICEMIND_MODEL_ALLOWED_MODELS": MODEL,
            "SERVICEMIND_TENANT_MODEL_ALLOWLIST_JSON": "{}",
            "SERVICEMIND_MODEL_GATEWAY_AUDIT_ENABLED": "true",
            "SERVICEMIND_SEMANTIC_CACHE_ENABLED": "false",
            "SERVICEMIND_MODEL_TRACE_HEADERS_ENABLED": "true",
            "SERVICEMIND_MODEL_TIMEOUT_SECONDS": "90",
            "SERVICEMIND_MODEL_MAX_RETRIES": "1",
            "SERVICEMIND_DATABASE_URL": f"postgresql+psycopg://eval_app:{password}@127.0.0.1:{ports['pg']}/local_evaluation",
            "SERVICEMIND_MIGRATION_DATABASE_URL": f"postgresql+psycopg://eval_admin:{password}@127.0.0.1:{ports['pg']}/local_evaluation",
            "DATABASE_TYPE": "postgres",
            "POSTGRES_USER": "eval_admin",
            "POSTGRES_PASSWORD": password,
            "POSTGRES_HOST": "127.0.0.1",
            "POSTGRES_PORT": str(ports["pg"]),
            "POSTGRES_DB": "local_evaluation",
            "POSTGRES_AUTO_SETUP": "true",
            "SERVICEMIND_REDIS_URL": f"redis://127.0.0.1:{ports['redis']}",
            "NEO4J_URI": f"bolt://127.0.0.1:{ports['neo4j']}",
            "NEO4J_USER": "neo4j",
            "NEO4J_PASSWORD": password,
            "SERVICEMIND_OPENSEARCH_URL": f"http://127.0.0.1:{ports['os']}",
            "SERVICEMIND_OPENSEARCH_PASSWORD": password,
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "LOCAL_EVALUATION_STATE": str(STATE),
            "LOCAL_EVALUATION_BATCH": str(BATCH),
        }
    )
    save(STATE / "environment.json", env)
    (STATE / "environment.json").chmod(0o600)
    save(
        BATCH / "isolation-manifest.json",
        {
            "ports": ports,
            "containers": [PREFIX + key for key in [*configs, "model"]],
            "application_role": "NOSUPERUSER NOBYPASSRLS",
            "checkpoint_store": "dedicated PostgreSQL; no shared sqlite files",
            "external_read_only_fixtures": [
                "GLPI tickets",
                "Keycloak subjects",
                "embedding/reranker services",
            ],
            "model_backend": f"http://127.0.0.1:{ports['model']}/v1",
            "model": MODEL,
            "api_fee": 0,
            "production_configuration_modified": False,
        },
    )
    for _ in range(60):
        ready = subprocess.run(
            ["docker", "exec", PREFIX + "pg", "pg_isready", "-U", "eval_admin"], capture_output=True
        )
        if ready.returncode == 0:
            break
        time.sleep(0.5)
    sql = f"CREATE ROLE eval_app LOGIN PASSWORD '{password}' NOSUPERUSER NOBYPASSRLS;"
    command(
        [
            "docker",
            "exec",
            "-i",
            PREFIX + "pg",
            "psql",
            "-U",
            "eval_admin",
            "-d",
            "local_evaluation",
        ],
        input=sql,
    )
    command([str(ROOT / ".venv/bin/alembic"), "upgrade", "head"], cwd=ROOT, env=env)
    sql = "GRANT USAGE ON SCHEMA public TO eval_app; GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO eval_app; GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO eval_app;"
    command(
        [
            "docker",
            "exec",
            "-i",
            PREFIX + "pg",
            "psql",
            "-U",
            "eval_admin",
            "-d",
            "local_evaluation",
        ],
        input=sql,
    )
    print(json.dumps({"state": str(STATE), "ports": ports}))


async def seed() -> None:
    from uuid import UUID

    from seed_phase2 import main as seed_tenants
    from seed_phase7_acceptance_fixtures import (
        declared_ticket_refs,
        documents,
        load_manifest,
        recorded_ticket_ids,
    )

    from servicemind.agents.knowledge import KnowledgeAgent
    from servicemind.evaluation.graph_probe import graph_fixture_batch
    from servicemind.graphrag.build import build_graph_store
    from servicemind.persistence.database import close_database

    await seed_tenants()
    manifest = load_manifest()
    tenant = UUID(manifest["tenant_id"])
    rag = KnowledgeAgent().rag
    await rag.ingest(tenant, documents(manifest))
    for entry in manifest["documents"]:
        if not entry["is_active"]:
            await rag.set_document_active(tenant, entry["source_record_id"], is_active=False)
    graph = build_graph_store()
    assert graph is not None
    refs, _ = declared_ticket_refs()
    tickets = recorded_ticket_ids()
    await graph.apply_batch(
        graph_fixture_batch(
            tenant,
            entity_id=int(manifest["glpi_entity_id"]),
            ticket_ids=sorted(tickets[r] for r in refs),
        )
    )
    await graph.close()
    if hasattr(rag.index, "client"):
        await rag.index.client.close()
    await close_database()
    save(
        BATCH / "seed.json",
        {
            "knowledge_documents": len(manifest["documents"]),
            "ticket_creation": False,
            "shared_database_read_or_write": False,
        },
    )


def serve_proxy() -> None:
    import httpx
    import uvicorn
    from fastapi import FastAPI, HTTPException
    from starlette.responses import StreamingResponse

    from servicemind.model_gateway.contracts import stable_hash

    app = FastAPI()
    key = os.environ["COMPATIBLE_API_KEY"]
    backend_port = json.loads((BATCH / "isolation-manifest.json").read_text())["ports"]["model"]

    @app.post("/v1/chat/completions")
    async def completions(request: Request):
        if request.headers.get("authorization") != "Bearer " + key:
            raise HTTPException(401)
        body = await request.json()
        if body.get("model") != MODEL:
            raise HTTPException(403, "Model outside the local evaluation allowlist")
        if not all(
            request.headers.get(name)
            for name in (
                "x-servicemind-request-id",
                "x-servicemind-run-id",
                "x-servicemind-agent-role",
            )
        ):
            raise HTTPException(422, "Scoped inference trace headers are required")
        started = time.perf_counter()
        record = {
            "request_id": request.headers.get("x-servicemind-request-id"),
            "run_id": request.headers.get("x-servicemind-run-id"),
            "agent_role": request.headers.get("x-servicemind-agent-role"),
            "model": body["model"],
            "started_at": time.time(),
            "prompt_hash": stable_hash(
                [
                    {
                        "type": {"user": "human", "assistant": "ai"}.get(m["role"], m["role"]),
                        "content": m.get("content", ""),
                    }
                    for m in body.get("messages", [])
                ]
            ),
            "stream": body.get("stream", False),
        }
        with (BATCH / "wire-starts.jsonl").open("a") as f:
            f.write(json.dumps(record) + "\n")
        client = httpx.AsyncClient(timeout=180, trust_env=False)
        upstream = await client.send(
            client.build_request(
                "POST",
                f"http://127.0.0.1:{backend_port}/v1/chat/completions",
                json=body,
                headers={"Authorization": "Bearer " + key},
            ),
            stream=True,
        )
        record["http_status"] = upstream.status_code

        async def chunks():
            digest = hashlib.sha256()
            data = bytearray()
            pending = b""
            try:
                async for chunk in upstream.aiter_bytes():
                    record.setdefault("first_byte_seconds", time.perf_counter() - started)
                    digest.update(chunk)
                    data.extend(chunk)
                    pending += chunk
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        try:
                            event = json.loads(line.removeprefix(b"data: "))
                        except ValueError:
                            continue
                        for choice in event.get("choices", []):
                            delta = choice.get("delta", {})
                            if (
                                delta.get("content")
                                or delta.get("tool_calls")
                                or delta.get("reasoning_content")
                            ):
                                record.setdefault(
                                    "first_token_seconds", time.perf_counter() - started
                                )
                    yield chunk
                record["transport_status"] = "completed"
            finally:
                record.setdefault("transport_status", "cancelled")
                record["elapsed_seconds"] = time.perf_counter() - started
                record["response_sha256"] = digest.hexdigest()
                for line in data.decode(errors="replace").splitlines():
                    try:
                        payload = json.loads(line.removeprefix("data: "))
                    except ValueError:
                        continue
                    if payload.get("usage"):
                        record["usage"] = payload["usage"]
                    if payload.get("model"):
                        record["response_model"] = payload["model"]
                    for choice in payload.get("choices", []):
                        if choice.get("finish_reason"):
                            record["finish_reason"] = choice["finish_reason"]
                with (BATCH / "wire-traces.jsonl").open("a") as f:
                    f.write(json.dumps(record) + "\n")
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            chunks(),
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
        )

    uvicorn.run(app, host="127.0.0.1", port=PROXY_PORT, log_level="warning")


async def probe(label: str) -> None:
    from datetime import UTC, datetime
    from uuid import UUID

    from sqlalchemy import select
    from verify_phase7_acceptance_live import Stack, environment_of, run_case

    from servicemind.evaluation.acceptance import case_set_digest, load_acceptance_cases
    from servicemind.evaluation.acceptance_grader import grade
    from servicemind.evaluation.model_provenance import grade_model_provenance
    from servicemind.evaluation.source_revision import source_revision
    from servicemind.persistence.database import close_database, tenant_session
    from servicemind.persistence.models import ModelInvocationRecord, RunEvent

    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", label) or (BATCH / label).exists():
        raise RuntimeError("Use a new, simple observation label; previous evidence is immutable")
    stack = Stack(
        base_url=f"http://127.0.0.1:{API_PORT}",
        tenant_id=UUID("22222222-2222-4222-8222-222222222222"),
        timeout_scale=1,
    )
    try:
        await stack.await_health()
        before = (await stack.client.get(stack.base_url + "/health/build")).json()
        assert before["source_revision"] == source_revision(ROOT)
        assert before["configuration"]["compatible_model"] == MODEL
        stack.build_identity = before
        cases = load_acceptance_cases(ROOT / "evaluation/acceptance/cases.v1.json")
        tickets = json.loads(
            (ROOT / "evaluation/acceptance/fixtures/globex/tickets.json").read_text()
        )["tickets"]
        execution = await run_case(
            next(c for c in cases.cases if c.id == "ACC-01"),
            stack,
            tickets,
            environment_of(stack, source_revision(ROOT)),
            case_set_digest(cases),
            BATCH / label,
        )
        after = (await stack.client.get(stack.base_url + "/health/build")).json()
        assert before == after and before["source_revision"] == source_revision(ROOT)
        result = next(
            c
            for c in grade(cases, [execution], generated_at=datetime.now(UTC)).cases
            if c.case_id == "ACC-01"
        )
        async with tenant_session(stack.tenant_id) as session:
            calls = (
                (
                    await session.execute(
                        select(ModelInvocationRecord)
                        .where(ModelInvocationRecord.run_id == execution.run_id)
                        .order_by(ModelInvocationRecord.created_at)
                    )
                )
                .scalars()
                .all()
            )
            events = (
                (
                    await session.execute(
                        select(RunEvent)
                        .where(RunEvent.run_id == execution.run_id)
                        .order_by(RunEvent.sequence)
                    )
                )
                .scalars()
                .all()
            )
            fields = [
                "request_id",
                "run_id",
                "agent_role",
                "purpose",
                "model",
                "provider",
                "prompt_hash",
                "schema_hash",
                "latency_ms",
                "attempts",
                "status",
                "error_code",
                "input_tokens",
                "output_tokens",
                "created_at",
            ]
            save(
                BATCH / label / "model-calls.json",
                [{k: getattr(c, k) for k in fields} for c in calls],
            )
            save(
                BATCH / label / "events.json",
                [
                    {"event_type": e.event_type, "payload": e.payload, "created_at": e.created_at}
                    for e in events
                ],
            )
        save(
            BATCH / label / "summary.json",
            {
                "run_id": execution.run_id,
                "utility_verdict": result.verdict.value,
                "seconds": execution.total_seconds,
                "runtime_identity": before,
                "identity_unchanged": before == after,
                "api_fee": 0,
            },
        )
        await asyncio.sleep(0.5)
        wire_path = BATCH / "wire-traces.jsonl"
        wires = (
            [json.loads(line) for line in wire_path.read_text().splitlines()]
            if wire_path.exists()
            else []
        )
        calls = json.loads((BATCH / label / "model-calls.json").read_text())
        provenance = grade_model_provenance(
            run_id=str(execution.run_id),
            calls=calls,
            wires=wires,
            expected_model=MODEL,
            starts=[
                json.loads(line) for line in (BATCH / "wire-starts.jsonl").read_text().splitlines()
            ],
        )
        if str(execution.terminal_status) not in {
            "succeeded",
            "failed",
            "cancelled",
            "waiting_approval",
            "waiting_review",
        }:
            provenance["verdict"] = "BLOCKED"
            provenance["issues"].append("The Run was still active when the observation ended")
        save(BATCH / label / "provenance.json", provenance)
        save(BATCH / label / "grade.json", result.model_dump(mode="json"))
        if str(execution.terminal_status) in {"pending", "running"}:
            token = await stack.token("globex-analyst-g3")
            response = await stack.client.post(
                stack.base_url + f"/v1/servicemind/runs/{execution.run_id}:cancel",
                headers={"Authorization": "Bearer " + token},
            )
            save(
                BATCH / label / "cleanup.json",
                {
                    "run_id": str(execution.run_id),
                    "http_status": response.status_code,
                    "observation_modified": False,
                },
            )
            response.raise_for_status()
        print(
            json.dumps(
                {
                    "label": label,
                    "verdict": result.verdict.value,
                    "provenance": provenance["verdict"],
                    "seconds": execution.total_seconds,
                }
            )
        )
    finally:
        await stack.aclose()
        await close_database()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["setup", "seed", "api", "proxy", "probe", "exec", "cleanup", "profile"]
    )
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.args[:1] == ["--"]:
        args.args = args.args[1:]
    if args.action == "setup":
        setup()
        return
    if (
        args.action in {"api", "proxy", "seed", "probe", "profile"}
        and (BATCH / "SHA256SUMS.json").exists()
    ):
        raise RuntimeError("Evidence is sealed; choose a fresh LOCAL_EVALUATION_BATCH directory")
    if args.action == "cleanup":
        cleanup()
        return
    if args.action == "profile":
        profiles = {
            "baseline": {
                "SERVICEMIND_MODEL_TIMEOUT_SECONDS": "90",
                "SERVICEMIND_CONTEXT_MAX_INPUT_TOKENS": "12000",
                "SERVICEMIND_CONTEXT_EVIDENCE_TOKEN_CAP": "5000",
            },
            "bounded-context": {
                "SERVICEMIND_MODEL_TIMEOUT_SECONDS": "120",
                "SERVICEMIND_CONTEXT_MAX_INPUT_TOKENS": "4096",
                "SERVICEMIND_CONTEXT_EVIDENCE_TOKEN_CAP": "1200",
            },
        }
        if len(args.args) != 1 or args.args[0] not in profiles:
            raise RuntimeError("Choose profile baseline or bounded-context")
        env = environment()
        env.update(profiles[args.args[0]])
        save(STATE / "environment.json", env)
        save(BATCH / f"profile-{args.args[0]}.json", profiles[args.args[0]])
        return
    if not os.environ.get("LOCAL_EVALUATION_STATE"):
        os.execve(
            sys.executable, [sys.executable, __file__, args.action, *args.args], environment()
        )
    if args.action == "seed":
        asyncio.run(seed())
    elif args.action == "proxy":
        register_process("proxy")
        serve_proxy()
    elif args.action == "api":
        register_process("api")
        import uvicorn

        from service.service import create_app

        uvicorn.run(create_app(), host="127.0.0.1", port=API_PORT, timeout_graceful_shutdown=10)
    elif args.action == "probe":
        asyncio.run(probe(args.args[0]))
    elif args.action == "exec":
        os.execvpe(args.args[0], args.args, dict(os.environ))


if __name__ == "__main__":
    main()
