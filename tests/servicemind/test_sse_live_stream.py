"""The event endpoint follows a run after the HTTP connection is established."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from servicemind import api as api_module
from servicemind.security.auth import TenantContext

TENANT_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
NOW = datetime(2026, 11, 5, tzinfo=UTC)


class _Request:
    disconnected = False

    async def is_disconnected(self) -> bool:
        return self.disconnected


class _Repository:
    def __init__(self, run_id: UUID) -> None:
        self.run_id = run_id
        self.status = "running"
        self.events: list[SimpleNamespace] = []
        self.calls: list[tuple[int, int]] = []
        self.queried = asyncio.Event()

    async def get_run(self, run_id: UUID) -> SimpleNamespace:
        assert run_id == self.run_id
        return SimpleNamespace(
            id=run_id,
            tenant_id=TENANT_ID,
            user_id="operator-1",
            access_scope={"entity_ids": [], "group_ids": []},
            status=self.status,
        )

    async def list_events(self, run_id: UUID, after: int, *, limit: int) -> list[SimpleNamespace]:
        assert run_id == self.run_id
        self.calls.append((after, limit))
        self.queried.set()
        return [event for event in self.events if event.sequence > after][:limit]

    def append(self, sequence: int, kind: str = "analysis.completed") -> None:
        self.events.append(
            SimpleNamespace(
                sequence=sequence,
                event_type=kind,
                payload={"status": self.status},
                created_at=NOW,
            )
        )


def _context() -> TenantContext:
    return TenantContext(
        tenant_id=TENANT_ID,
        user_id="operator-1",
        username="operator-1",
        roles={"viewer"},
    )


@pytest.mark.asyncio
async def test_stream_delivers_an_event_committed_after_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    repository = _Repository(run_id)
    request = _Request()
    monkeypatch.setattr(api_module, "ServiceMindRepository", lambda tenant: repository)
    monkeypatch.setattr(api_module, "SSE_POLL_INTERVAL_SECONDS", 0.005)

    response = await api_module.stream_run_events(run_id, request, _context(), None)
    frames = response.body_iterator
    pending = asyncio.create_task(anext(frames))
    await asyncio.wait_for(repository.queried.wait(), timeout=1)
    assert not pending.done(), "an active run must wait for future events"

    repository.append(1, "run.succeeded")
    first = await asyncio.wait_for(pending, timeout=1)
    assert "id: 1\nevent: run.succeeded" in first
    assert await asyncio.wait_for(anext(frames), timeout=1) == "event: end\ndata: {}\n\n"
    with pytest.raises(StopAsyncIteration):
        await anext(frames)


@pytest.mark.asyncio
async def test_stream_drains_large_history_in_bounded_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    repository = _Repository(run_id)
    for sequence in range(1, 206):
        repository.append(sequence, "run.succeeded" if sequence == 205 else "analysis.completed")
    repository.status = "succeeded"
    monkeypatch.setattr(api_module, "ServiceMindRepository", lambda tenant: repository)
    monkeypatch.setattr(api_module, "SSE_POLL_INTERVAL_SECONDS", 0.005)
    monkeypatch.setattr(api_module, "SSE_QUIESCENT_GRACE_SECONDS", 0.01)

    response = await api_module.stream_run_events(run_id, _Request(), _context(), None)
    frames = [frame async for frame in response.body_iterator]
    ids = [int(frame.split("\n", 1)[0].removeprefix("id: ")) for frame in frames[:-1]]

    assert ids == list(range(1, 206))
    assert frames[-1] == "event: end\ndata: {}\n\n"
    assert repository.calls[:3] == [(0, 100), (100, 100), (200, 100)]
    assert all(limit <= 100 for _, limit in repository.calls)


@pytest.mark.asyncio
async def test_reconnect_skips_already_received_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    repository = _Repository(run_id)
    for sequence in range(1, 9):
        repository.append(sequence, "run.succeeded" if sequence == 8 else "route.completed")
    monkeypatch.setattr(api_module, "ServiceMindRepository", lambda tenant: repository)

    response = await api_module.stream_run_events(run_id, _Request(), _context(), "7")
    frames = [frame async for frame in response.body_iterator]

    assert [frame.split("\n", 1)[0] for frame in frames] == ["id: 8", "event: end"]
    assert repository.calls == [(7, 100)]


@pytest.mark.asyncio
async def test_terminal_event_does_not_hide_later_committed_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    repository = _Repository(run_id)
    repository.status = "succeeded"
    repository.append(1, "run.succeeded")
    for sequence in range(2, 206):
        repository.append(sequence, "memory.post_run_completed")
    monkeypatch.setattr(api_module, "ServiceMindRepository", lambda tenant: repository)

    response = await api_module.stream_run_events(run_id, _Request(), _context(), None)
    frames = [frame async for frame in response.body_iterator]
    ids = [int(frame.split("\n", 1)[0].removeprefix("id: ")) for frame in frames[:-1]]

    assert ids == list(range(1, 206))
    assert frames[-1] == "event: end\ndata: {}\n\n"
    assert repository.calls == [(0, 100), (100, 100), (200, 100)]


@pytest.mark.asyncio
async def test_disconnected_client_stops_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    repository = _Repository(run_id)
    monkeypatch.setattr(api_module, "ServiceMindRepository", lambda tenant: repository)
    request = _Request()
    request.disconnected = True

    response = await api_module.stream_run_events(run_id, request, _context(), None)
    frames = [frame async for frame in response.body_iterator]

    assert frames == []
    assert repository.calls == []


@pytest.mark.asyncio
async def test_approval_pause_closes_after_sending_its_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    repository = _Repository(run_id)
    repository.status = "waiting_approval"
    repository.append(1, "approval.required")
    monkeypatch.setattr(api_module, "ServiceMindRepository", lambda tenant: repository)
    monkeypatch.setattr(api_module, "SSE_POLL_INTERVAL_SECONDS", 0.005)
    monkeypatch.setattr(api_module, "SSE_QUIESCENT_GRACE_SECONDS", 0.01)

    response = await api_module.stream_run_events(run_id, _Request(), _context(), None)
    frames = [frame async for frame in response.body_iterator]

    assert len(frames) == 2
    assert "event: approval.required" in frames[0]
    assert frames[1] == "event: end\ndata: {}\n\n"


@pytest.mark.asyncio
async def test_terminal_event_committed_just_after_status_is_not_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    repository = _Repository(run_id)
    repository.status = "succeeded"
    monkeypatch.setattr(api_module, "ServiceMindRepository", lambda tenant: repository)
    monkeypatch.setattr(api_module, "SSE_POLL_INTERVAL_SECONDS", 0.005)
    monkeypatch.setattr(api_module, "SSE_QUIESCENT_GRACE_SECONDS", 0.005)

    response = await api_module.stream_run_events(run_id, _Request(), _context(), None)
    frames = response.body_iterator
    pending = asyncio.create_task(anext(frames))
    await asyncio.wait_for(repository.queried.wait(), timeout=1)
    # The former implementation stopped once this grace elapsed, before the
    # terminal event's separate transaction committed.
    await asyncio.sleep(0.03)
    assert not pending.done()
    repository.append(1, "run.succeeded")

    assert "event: run.succeeded" in await asyncio.wait_for(pending, timeout=1)
    assert await asyncio.wait_for(anext(frames), timeout=1) == "event: end\ndata: {}\n\n"


@pytest.mark.asyncio
async def test_unmarked_terminal_session_reconnects_without_claiming_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    repository = _Repository(run_id)
    repository.status = "failed"
    monkeypatch.setattr(api_module, "ServiceMindRepository", lambda tenant: repository)
    monkeypatch.setattr(api_module, "SSE_POLL_INTERVAL_SECONDS", 0.002)
    monkeypatch.setattr(api_module, "SSE_SESSION_MAX_SECONDS", 0.01)

    response = await api_module.stream_run_events(run_id, _Request(), _context(), "7")
    frames = [frame async for frame in response.body_iterator]

    assert frames == ["retry: 1000\n\n"]
    assert all(cursor == 7 for cursor, _ in repository.calls)


@pytest.mark.asyncio
async def test_idle_heartbeat_has_no_event_id_and_disconnect_releases_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    repository = _Repository(run_id)
    request = _Request()
    monkeypatch.setattr(api_module, "ServiceMindRepository", lambda tenant: repository)
    monkeypatch.setattr(api_module, "SSE_HEARTBEAT_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(api_module, "SSE_POLL_INTERVAL_SECONDS", 0.002)

    response = await api_module.stream_run_events(run_id, request, _context(), None)
    frames = response.body_iterator
    assert await anext(frames) == ": keep-alive\n\n"
    request.disconnected = True
    with pytest.raises(StopAsyncIteration):
        await anext(frames)
