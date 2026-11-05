"""HTTP retry contracts must not schedule duplicate work or bypass current access."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from servicemind import api
from servicemind.orchestration.execution_config import RunExecutionConfig
from servicemind.persistence.repository import RunSubmissionConflict
from servicemind.security.auth import TenantContext, get_tenant_context


def _context():
    return TenantContext(
        tenant_id=uuid4(),
        user_id="requester",
        username="requester",
        roles={"analyst"},
        allowed_glpi_entity_ids={1},
    )


def _run(context):
    return SimpleNamespace(
        id=uuid4(),
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        ticket_id=42,
        goal="inspect VPN ticket",
        request_write=False,
        status="pending",
        access_scope={"entity_ids": [1], "group_ids": []},
        result=None,
        error=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _app(context):
    app = FastAPI()
    app.include_router(api.phase2_router)
    app.dependency_overrides[get_tenant_context] = lambda: context
    return app


@pytest.mark.asyncio
async def test_replayed_submission_schedules_only_the_original_background_task(monkeypatch):
    context = _context()
    run = _run(context)
    requests = []
    executions = []

    class Repository:
        def __init__(self, tenant):
            assert tenant == context.tenant_id

        async def create_run_idempotent(self, **kwargs):
            requests.append(kwargs)
            return run, len(requests) == 1

        async def append_event(self, *args):
            raise AssertionError("the initial event is already in the submission transaction")

    async def execute(value, principal):
        executions.append(value.id)

    monkeypatch.setattr(api, "ServiceMindRepository", Repository)
    monkeypatch.setattr(api, "get_run_execution_config", lambda: RunExecutionConfig())
    monkeypatch.setattr(api, "_process_webhook_run", execute)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(context)), base_url="http://test"
    ) as client:
        first = await client.post(
            "/v1/servicemind/runs",
            headers={"Idempotency-Key": "same-request"},
            json={"ticket_id": 42, "goal": "inspect VPN ticket"},
        )
        second = await client.post(
            "/v1/servicemind/runs",
            headers={"Idempotency-Key": "same-request"},
            json={"goal": "inspect VPN ticket", "request_write": False, "ticket_id": 42},
        )
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"] == str(run.id)
    assert executions == [run.id]
    assert requests[0]["event_payload"] == {"ticket_id": 42, "request_write": False}


@pytest.mark.asyncio
async def test_replay_rechecks_current_entity_scope_and_returns_no_stale_run(monkeypatch):
    context = _context()
    run = _run(context)
    context.allowed_glpi_entity_ids.clear()

    class Repository:
        def __init__(self, tenant):
            pass

        async def create_run_idempotent(self, **kwargs):
            return run, False

    monkeypatch.setattr(api, "ServiceMindRepository", Repository)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(context)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/servicemind/runs",
            headers={"Idempotency-Key": "same-request"},
            json={"ticket_id": 42, "goal": "inspect VPN ticket"},
        )
    assert response.status_code == 404
    assert str(run.id) not in response.text


@pytest.mark.asyncio
async def test_conflicting_retry_key_returns_409(monkeypatch):
    context = _context()

    class Repository:
        def __init__(self, tenant):
            pass

        async def create_run_idempotent(self, **kwargs):
            raise RunSubmissionConflict("Idempotency-Key was reused with different request content")

    monkeypatch.setattr(api, "ServiceMindRepository", Repository)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(context)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/servicemind/runs",
            headers={"Idempotency-Key": "same-request"},
            json={"ticket_id": 42, "goal": "inspect VPN ticket"},
        )
    assert response.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [b"", b" ", b"has space", b"x" * 129, b"\xff"])
async def test_invalid_idempotency_header_is_rejected_before_admission(monkeypatch, key):
    def forbidden_repository(tenant):
        raise AssertionError("invalid keys must not enter the repository")

    monkeypatch.setattr(api, "ServiceMindRepository", forbidden_repository)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(_context())), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/servicemind/runs",
            headers=[(b"Idempotency-Key", key)],
            json={"ticket_id": 42, "goal": "inspect VPN ticket"},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_analyst_role_is_required_even_for_known_retry_keys(monkeypatch):
    context = _context()
    context.roles.clear()

    def forbidden_repository(tenant):
        raise AssertionError("role denial must happen before the receipt lookup")

    monkeypatch.setattr(api, "ServiceMindRepository", forbidden_repository)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(context)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/servicemind/runs",
            headers={"Idempotency-Key": "known-key"},
            json={"ticket_id": 42, "goal": "inspect VPN ticket"},
        )
    assert response.status_code == 403
