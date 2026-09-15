"""Exercise the deployed MCP HTTP boundary with the local Keycloak and GLPI stack."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
from dotenv import dotenv_values

from core import settings
from servicemind.mcp.server import (
    GOVERNED_EXECUTION_EXTENSION,
    MCP_PROTOCOL_VERSION,
    TASK_EXTENSION,
)
from servicemind.persistence.database import close_database
from servicemind.persistence.repository import ServiceMindRepository

ROOT = Path(__file__).resolve().parents[1]
ACME = UUID("11111111-1111-4111-8111-111111111111")


def request_meta(run_id: UUID | None = None) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientInfo": {
            "name": "phase6-live-verifier",
            "version": "1.0",
        },
        "io.modelcontextprotocol/clientCapabilities": {
            "extensions": {TASK_EXTENSION: {}, GOVERNED_EXECUTION_EXTENSION: {}}
        },
    }
    if run_id:
        meta["com.servicemind/execution"] = {
            "requestId": str(uuid4()),
            "runId": str(run_id),
            "taskId": "phase6-http-live",
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
        }
    return meta


def rpc(rpc_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": method,
        "params": params,
    }


def headers(method: str, *, token: str | None = None, name: str | None = None) -> dict[str, str]:
    result = {"Mcp-Protocol-Version": MCP_PROTOCOL_VERSION, "Mcp-Method": method}
    if token:
        result["Authorization"] = f"Bearer {token}"
    if name:
        result["Mcp-Name"] = name
    return result


async def post(
    client: httpx.AsyncClient,
    resource: str,
    *,
    rpc_id: int,
    method: str,
    params: dict[str, Any],
    token: str,
    name: str | None = None,
) -> dict[str, Any]:
    response = await client.post(
        resource,
        headers=headers(method, token=token, name=name),
        json=rpc(rpc_id, method, params),
    )
    response.raise_for_status()
    document = response.json()
    assert document.get("jsonrpc") == "2.0" and document.get("id") == rpc_id
    assert "error" not in document, document.get("error")
    return document["result"]


async def main() -> None:
    resource = settings.SERVICEMIND_MCP_PUBLIC_URL
    issuer = settings.SERVICEMIND_OIDC_ISSUER
    if not resource or not issuer:
        raise RuntimeError("MCP public URL and OIDC issuer must be configured")
    values = dotenv_values(ROOT / "deploy" / "glpi" / ".env")
    password = values.get("ACME_ANALYST_PASSWORD")
    if not password:
        raise RuntimeError("ACME_ANALYST_PASSWORD is required for local acceptance")
    origin = resource.split("/v1/servicemind/mcp", 1)[0]
    metadata_url = (
        f"{origin}/.well-known/oauth-protected-resource/v1/servicemind/mcp"
    )
    async with httpx.AsyncClient(timeout=40, trust_env=False) as client:
        metadata_response = await client.get(metadata_url)
        metadata_response.raise_for_status()
        metadata = metadata_response.json()
        assert metadata["resource"] == resource
        assert metadata["authorization_servers"] == [issuer]

        unauthorized = await client.post(
            resource,
            headers=headers("server/discover"),
            json=rpc(
                1,
                "server/discover",
                {"_meta": request_meta()},
            ),
        )
        assert unauthorized.status_code == 401
        assert f'resource_metadata="{metadata_url}"' in unauthorized.headers[
            "www-authenticate"
        ]

        token_response = await client.post(
            f"{issuer}/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": settings.SERVICEMIND_OIDC_AUDIENCE,
                "username": "acme-analyst",
                "password": password,
                "resource": resource,
            },
        )
        token_response.raise_for_status()
        token = str(token_response.json()["access_token"])

        discovery = await post(
            client,
            resource,
            rpc_id=2,
            method="server/discover",
            params={"_meta": request_meta()},
            token=token,
        )
        assert discovery["supportedVersions"] == [MCP_PROTOCOL_VERSION]
        tools = await post(
            client,
            resource,
            rpc_id=3,
            method="tools/list",
            params={"_meta": request_meta()},
            token=token,
        )
        names = {item["name"] for item in tools["tools"]}
        assert {
            "search_tickets",
            "get_ticket_context",
            "search_knowledge",
            "query_cmdb_dependencies",
            "submit_action_intent",
        } <= names

        repository = ServiceMindRepository(ACME)
        search_run = await repository.create_run(
            user_id="phase6-http-live",
            ticket_id=1,
            goal="MCP HTTP GLPI acceptance",
            request_write=False,
        )
        search = await post(
            client,
            resource,
            rpc_id=4,
            method="tools/call",
            name="search_tickets",
            params={
                "name": "search_tickets",
                "arguments": {"query": "phase6-live-no-match", "limit": 1},
                "_meta": request_meta(search_run.id),
            },
            token=token,
        )
        assert search["isError"] is False
        assert search["_meta"]["com.servicemind/receipt"]["verified"] is True

        task_run = await repository.create_run(
            user_id="phase6-http-live",
            ticket_id=1,
            goal="MCP durable Tasks acceptance",
            request_write=False,
        )
        task = await post(
            client,
            resource,
            rpc_id=5,
            method="tools/call",
            name="query_cmdb_dependencies",
            params={
                "name": "query_cmdb_dependencies",
                "arguments": {"identifier": "phase6-no-match", "max_hops": 1},
                "_meta": request_meta(task_run.id),
            },
            token=token,
        )
        assert task["resultType"] == "task" and task["status"] == "working"
        task_id = str(task["taskId"])
        terminal: dict[str, Any] = task
        for rpc_id in range(6, 21):
            await asyncio.sleep(task["pollIntervalMs"] / 1000)
            terminal = await post(
                client,
                resource,
                rpc_id=rpc_id,
                method="tasks/get",
                name=task_id,
                params={"taskId": task_id, "_meta": request_meta()},
                token=token,
            )
            if terminal["status"] != "working":
                break
        assert terminal["status"] == "completed", terminal
        assert terminal["result"]["isError"] is False
        assert set(terminal["result"]["structuredContent"]) == {"nodes", "edges"}
        print(
            "PASS phase6 MCP HTTP: RFC9728 challenge, OIDC, discovery, five governed tools, "
            "GLPI v2 call, server-directed durable Task"
        )
    await close_database()


if __name__ == "__main__":
    asyncio.run(main())
