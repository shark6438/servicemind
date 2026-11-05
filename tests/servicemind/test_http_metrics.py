from types import SimpleNamespace

import pytest

from servicemind.observability import http as observed


@pytest.mark.asyncio
async def test_http_timer_excludes_background_work_and_uses_route_template(monkeypatch) -> None:
    clock = [0.0]
    rows = []
    monkeypatch.setattr(observed.time, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(observed, "record_http_response_start", lambda *args: rows.append(args))

    async def app(scope, receive, send):
        scope["route"] = SimpleNamespace(path="/runs/{run_id}")
        clock[0] = 0.02
        await send({"type": "http.response.start", "status": 202, "headers": []})
        clock[0] = 30.0
        await send({"type": "http.response.body", "body": b"{}"})

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        pass

    await observed.HttpMetricsMiddleware(app)(
        {"type": "http", "method": "POST", "path": "/runs/private-run-id"}, receive, send
    )
    assert rows == [(0.02, "POST", "/runs/{run_id}", 202)]


@pytest.mark.asyncio
async def test_http_failure_before_headers_is_measured_and_still_raises(monkeypatch) -> None:
    rows = []
    monkeypatch.setattr(observed, "record_http_response_start", lambda *args: rows.append(args))

    async def app(scope, receive, send):
        raise RuntimeError("failed")

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        pass

    with pytest.raises(RuntimeError, match="failed"):
        await observed.HttpMetricsMiddleware(app)(
            {"type": "http", "method": "GET", "path": "/private-ticket"}, receive, send
        )
    assert len(rows) == 1
    assert rows[0][1:] == ("GET", "unmatched", 500)
