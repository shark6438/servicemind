from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import BackgroundTasks, FastAPI

from servicemind import api as api_module
from servicemind.api import CreateRunRequest, phase2_router
from servicemind.interfaces.http import operations as operations_module
from servicemind.security.auth import TenantContext, get_tenant_context

TENANT_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def _context(roles: set[str]) -> TenantContext:
    return TenantContext(
        tenant_id=TENANT_ID,
        user_id="operator-1",
        username="operator-1",
        roles=roles,
    )


def _app(context: TenantContext) -> FastAPI:
    app = FastAPI()
    app.include_router(phase2_router)
    app.dependency_overrides[get_tenant_context] = lambda: context
    return app


class _Repository:
    tenant_ids: list[UUID] = []

    def __init__(self, tenant_id: UUID) -> None:
        self.tenant_ids.append(tenant_id)

    async def list_runs(self, **_: object) -> list[SimpleNamespace]:
        now = datetime(2026, 9, 22, tzinfo=UTC)
        return [
            SimpleNamespace(
                id=uuid4(),
                ticket_id=42,
                goal="Investigate VPN incident",
                request_write=False,
                status="succeeded",
                created_at=now,
                updated_at=now,
            )
        ]

    async def get_run(self, run_id: UUID) -> SimpleNamespace:
        return SimpleNamespace(id=run_id)

    async def list_events(self, run_id: UUID, after: int) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                sequence=after + 1,
                event_type="run.succeeded",
                payload={"status": "succeeded"},
                created_at=datetime(2026, 9, 22, tzinfo=UTC),
            )
        ]

    async def list_audit_events(self, **_: object) -> list[SimpleNamespace]:
        return []


@pytest.mark.asyncio
async def test_operations_views_are_tenant_scoped_and_cursor_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Repository.tenant_ids.clear()
    monkeypatch.setattr(operations_module, "ServiceMindRepository", _Repository)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(_context({"viewer"}))),
        base_url="http://test",
    ) as client:
        response = await client.get("/v1/servicemind/runs")
        assert response.status_code == 200
        assert response.json()["items"][0]["ticket_id"] == 42
        assert _Repository.tenant_ids == [TENANT_ID]

        invalid = await client.get(
            "/v1/servicemind/runs",
            params={"after_created_at": "2026-09-22T00:00:00Z"},
        )
        assert invalid.status_code == 422


@pytest.mark.asyncio
async def test_audit_ledger_requires_an_operational_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(operations_module, "ServiceMindRepository", _Repository)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(_context({"viewer"}))),
        base_url="http://test",
    ) as client:
        denied = await client.get("/v1/servicemind/audit-events")
        assert denied.status_code == 403

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(_context({"operator"}))),
        base_url="http://test",
    ) as client:
        allowed = await client.get("/v1/servicemind/audit-events")
        assert allowed.status_code == 200
        assert allowed.json()["items"] == []


@pytest.mark.asyncio
async def test_create_run_returns_a_durable_identity_before_workflow_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 9, 22, tzinfo=UTC)
    created = SimpleNamespace(
        id=uuid4(),
        tenant_id=TENANT_ID,
        user_id="analyst-1",
        ticket_id=42,
        goal="Investigate VPN incident",
        request_write=False,
        status="pending",
        result=None,
        error=None,
        created_at=now,
        updated_at=now,
    )

    class Repository:
        def __init__(self, tenant_id: UUID) -> None:
            assert tenant_id == TENANT_ID

        async def create_run(self, **_: object) -> SimpleNamespace:
            return created

        async def append_event(self, *_: object) -> None:
            return None

    monkeypatch.setattr(api_module, "ServiceMindRepository", Repository)
    background = BackgroundTasks()
    response = await api_module.create_run(
        CreateRunRequest(ticket_id=42, goal="Investigate VPN incident"),
        background,
        _context({"analyst"}),
    )

    assert response.id == created.id
    assert response.status == "pending"
    assert len(background.tasks) == 1
