from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import BackgroundTasks, FastAPI
from pydantic import ValidationError

from servicemind import api as api_module
from servicemind.api import CreateRunRequest, phase2_router
from servicemind.domain.task import EVENT_SEQUENCE_MAX, TICKET_ID_MAX
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


def test_a_ticket_id_the_run_table_cannot_hold_never_creates_a_run() -> None:
    """``agent_runs.ticket_id`` is a 32-bit column; the request body is the boundary.

    Without the ceiling the body validated, the run row was built, and the driver
    rejected it four layers down -- a 500 for a request the API had accepted, and an
    operator with no way to tell a bad ticket id from a broken database.
    """
    with pytest.raises(ValidationError):
        CreateRunRequest(ticket_id=TICKET_ID_MAX + 1, goal="Investigate VPN incident")

    assert CreateRunRequest(ticket_id=TICKET_ID_MAX, goal="Investigate VPN incident").ticket_id


class _EventRepository:
    """Records the cursor the endpoint derived, so the boundary itself is observable."""

    cursors: list[int] = []

    def __init__(self, tenant_id: UUID) -> None:
        self.tenant_id = tenant_id

    async def get_run(self, run_id: UUID) -> SimpleNamespace:
        return SimpleNamespace(id=run_id)

    async def list_events(self, run_id: UUID, after: int) -> list[SimpleNamespace]:
        type(self).cursors.append(after)
        return [
            SimpleNamespace(
                sequence=after + 1,
                event_type="run.succeeded",
                payload={"status": "succeeded"},
                created_at=datetime(2026, 9, 22, tzinfo=UTC),
            )
        ]


async def _stream(
    monkeypatch: pytest.MonkeyPatch, last_event_id: str | None
) -> tuple[httpx.Response, list[int]]:
    """Fetch the stream and report the cursor(s) this one request handed the repository."""
    monkeypatch.setattr(api_module, "ServiceMindRepository", _EventRepository)
    headers = {} if last_event_id is None else {"Last-Event-ID": last_event_id}
    seen = len(_EventRepository.cursors)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(_context({"analyst"}))),
        base_url="http://test",
    ) as client:
        response = await client.get(f"/v1/servicemind/runs/{uuid4()}/events", headers=headers)
    return response, _EventRepository.cursors[seen:]


@pytest.mark.asyncio
async def test_an_absent_cursor_replays_the_run_log_from_the_beginning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response, cursors = await _stream(monkeypatch, None)

    assert response.status_code == 200
    assert cursors == [0]
    assert "event: run.succeeded" in response.text


@pytest.mark.asyncio
async def test_the_stream_resumes_at_the_cursor_the_client_echoes_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response, cursors = await _stream(monkeypatch, "7")

    assert response.status_code == 200
    assert cursors == [7]


@pytest.mark.asyncio
async def test_the_sequence_the_stream_emits_is_a_cursor_the_stream_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two halves of the resume contract have to agree on the same format.

    A client replays what it was last sent. That is only resumable if the ``id:`` line
    this endpoint writes is something this endpoint will read back -- so the test drives
    the round trip instead of asserting each half against a literal.
    """
    first, first_cursors = await _stream(monkeypatch, None)
    emitted = next(line for line in first.text.splitlines() if line.startswith("id: "))

    resumed, resumed_cursors = await _stream(monkeypatch, emitted.removeprefix("id: "))

    assert resumed.status_code == 200
    assert (first_cursors, resumed_cursors) == ([0], [1])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header", ["abc", "1e9", "-5", "1.5", str(EVENT_SEQUENCE_MAX + 1), "9" * 30]
)
async def test_a_cursor_that_is_not_a_position_in_the_log_is_a_400_not_a_500(
    monkeypatch: pytest.MonkeyPatch, header: str
) -> None:
    """``Last-Event-ID`` is client text read straight into a 32-bit ``sequence`` column.

    ``int(last_event_id or 0)`` let the header decide the response: text that is not a
    number raised ``ValueError`` past every handler, and a value past the column travelled
    to the driver as a parameter it cannot bind. Both arrived as a 500 on a request that
    had already authenticated, and the header is CORS-exposed, so a browser could send it.
    """
    response, cursors = await _stream(monkeypatch, header)

    assert response.status_code == 400
    assert cursors == []


@pytest.mark.asyncio
async def test_a_blank_cursor_is_the_start_of_the_log_not_a_rejected_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response, cursors = await _stream(monkeypatch, "")

    assert response.status_code == 200
    assert cursors == [0]


@pytest.mark.asyncio
async def test_the_last_cursor_the_log_column_can_hold_is_still_a_valid_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response, cursors = await _stream(monkeypatch, str(EVENT_SEQUENCE_MAX))

    assert response.status_code == 200
    assert cursors == [EVENT_SEQUENCE_MAX]
