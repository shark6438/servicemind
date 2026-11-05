"""Execution isolation and authority checks for the independent worker."""

import asyncio
import hashlib
import hmac
import json
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import BackgroundTasks, HTTPException, Request

from servicemind import api
from servicemind.orchestration import run_worker as module
from servicemind.orchestration.execution_config import RunExecutionConfig
from servicemind.persistence.repository import RunQueueFull
from servicemind.security.auth import TenantContext
from servicemind.security.entitlements import EntitlementOutcome, EntitlementResult


def _run(**overrides):
    values = dict(
        id=uuid4(),
        tenant_id=uuid4(),
        user_id="analyst",
        status="pending",
        thread_id=str(uuid4()),
        request_write=False,
        created_at=datetime.now(UTC),
        access_scope={"entity_ids": [1], "group_ids": [2]},
    )
    return SimpleNamespace(**(values | overrides))


def _worker():
    return module.RunWorker(per_tenant_limit=1, concurrency=2, lease_seconds=30, poll_seconds=0.01)


@pytest.mark.asyncio
async def test_worker_mode_admits_durable_run_without_api_background_task(monkeypatch):
    run = _run(
        ticket_id=42, goal="inspect VPN", result=None, error=None, updated_at=datetime.now(UTC)
    )
    calls = []

    class Repository:
        def __init__(self, tenant):
            pass

        async def create_run(self, **kwargs):
            calls.append(kwargs)
            return run

    monkeypatch.setattr(api, "get_run_execution_config", lambda: RunExecutionConfig(mode="worker"))
    monkeypatch.setattr(api, "ServiceMindRepository", Repository)
    background = BackgroundTasks()
    context = TenantContext(
        tenant_id=run.tenant_id, user_id="analyst", username="analyst", roles={"analyst"}
    )
    result = await api.create_run(
        api.CreateRunRequest(ticket_id=42, goal="inspect VPN"), background, context
    )
    assert result.id == run.id
    assert background.tasks == []
    assert calls[0]["event_payload"] == {"ticket_id": 42, "request_write": False}
    assert calls[0]["queue_limit"] == 100


@pytest.mark.asyncio
async def test_worker_queue_full_returns_retryable_429(monkeypatch):
    class Repository:
        def __init__(self, tenant):
            pass

        async def create_run(self, **kwargs):
            assert kwargs["queue_limit"] == 1
            raise RunQueueFull("full")

    monkeypatch.setattr(
        api, "get_run_execution_config", lambda: RunExecutionConfig(mode="worker", queue_limit=1)
    )
    monkeypatch.setattr(api, "ServiceMindRepository", Repository)
    context = TenantContext(
        tenant_id=uuid4(), user_id="analyst", username="analyst", roles={"analyst"}
    )
    with pytest.raises(HTTPException) as error:
        await api.create_run(
            api.CreateRunRequest(ticket_id=42, goal="inspect VPN"), BackgroundTasks(), context
        )
    assert error.value.status_code == 429
    assert error.value.headers == {"Retry-After": "5"}


@pytest.mark.asyncio
async def test_signed_webhook_queue_full_returns_retryable_429(monkeypatch):
    tenant = uuid4()
    body = json.dumps(
        {"tenant_id": str(tenant), "event": "new", "item": {"id": 42, "entity": {"id": 1}}}
    ).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(b"test-secret", body + timestamp.encode(), hashlib.sha256).hexdigest()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    class Repository:
        def __init__(self, value):
            pass

        async def get_glpi_integration(self):
            return SimpleNamespace(entity_id=1, webhook_secret_encrypted="ciphertext")

        async def claim_webhook_run(self, **kwargs):
            assert kwargs["queue_limit"] == 1
            raise RunQueueFull("full")

    monkeypatch.setattr(
        api, "get_run_execution_config", lambda: RunExecutionConfig(mode="worker", queue_limit=1)
    )
    monkeypatch.setattr(api, "ServiceMindRepository", Repository)
    monkeypatch.setattr(
        api, "CredentialCipher", lambda: SimpleNamespace(decrypt=lambda value: "test-secret")
    )
    request = Request(
        {"type": "http", "method": "POST", "path": "/webhooks/glpi", "headers": []}, receive
    )
    with pytest.raises(HTTPException) as error:
        await api.receive_glpi_webhook(request, BackgroundTasks(), signature, timestamp)
    assert error.value.status_code == 429
    assert error.value.headers == {"Retry-After": "5"}


@pytest.mark.asyncio
async def test_queued_run_intersects_submitted_and_current_grants(monkeypatch):
    run = _run()
    observed = []

    async def checkpoint(*args):
        return {}

    async def authority(*args, **kwargs):
        assert kwargs == {"fresh": True}
        return EntitlementResult(
            EntitlementOutcome.VERIFIED,
            roles=frozenset({"analyst"}),
            entity_ids=frozenset({1, 9}),
            group_ids=frozenset({2, 8}),
        )

    async def start(value, context):
        observed.append(context)

    monkeypatch.setattr(module, "get_checkpoint_state", checkpoint)
    monkeypatch.setattr(module, "revalidate", authority)
    monkeypatch.setattr(module, "start_run", start)
    await _worker()._execute_graph(run)
    assert observed[0].allowed_glpi_entity_ids == {1}
    assert observed[0].allowed_glpi_group_ids == {2}


@pytest.mark.asyncio
async def test_revoked_analyst_role_defers_execution(monkeypatch):
    async def checkpoint(*args):
        return {}

    async def authority(*args, **kwargs):
        return EntitlementResult(EntitlementOutcome.VERIFIED, roles=frozenset({"viewer"}))

    monkeypatch.setattr(module, "get_checkpoint_state", checkpoint)
    monkeypatch.setattr(module, "revalidate", authority)
    with pytest.raises(module.ResumeBlocked, match="insufficient_authority"):
        await _worker()._execute_graph(_run())


@pytest.mark.asyncio
async def test_reserved_webhook_subject_without_signed_origin_requires_revalidation(monkeypatch):
    run = _run(user_id="glpi-webhook")
    checked = []

    class Repository:
        def __init__(self, tenant):
            pass

        async def list_events(self, value, *, limit):
            assert limit == 1
            return [SimpleNamespace(sequence=1, event_type="run.created")]

    async def checkpoint(*args):
        return {}

    async def authority(tenant, subject, **kwargs):
        checked.append(subject)
        return EntitlementResult(EntitlementOutcome.UNAVAILABLE)

    monkeypatch.setattr(module, "ServiceMindRepository", Repository)
    monkeypatch.setattr(module, "get_checkpoint_state", checkpoint)
    monkeypatch.setattr(module, "revalidate", authority)
    with pytest.raises(module.ResumeBlocked, match="unavailable"):
        await _worker()._execute_graph(run)
    assert checked == ["glpi-webhook"]


@pytest.mark.asyncio
async def test_signed_webhook_origin_cannot_enable_write_execution(monkeypatch):
    run = _run(user_id="glpi-webhook", request_write=True)

    class Repository:
        def __init__(self, tenant):
            pass

        async def list_events(self, value, *, limit):
            return [SimpleNamespace(sequence=1, event_type="webhook.accepted")]

    monkeypatch.setattr(module, "ServiceMindRepository", Repository)
    with pytest.raises(PermissionError, match="cannot request writes"):
        await _worker()._execute_graph(run)


@pytest.mark.asyncio
async def test_persisted_rejection_recovers_without_identity_service(monkeypatch):
    run = _run(status="waiting_approval")
    observed = []

    class Repository:
        def __init__(self, tenant):
            pass

        async def get_approval(self, value):
            return SimpleNamespace(decision="rejected", decided_by="approver", comment=None)

    async def checkpoint(*args):
        return {"tenant_id": str(run.tenant_id), "user_id": run.user_id}

    async def decline(value, context, decision):
        observed.append(decision.decision)

    async def forbidden(*args, **kwargs):
        pytest.fail("a rejection queried authority")

    monkeypatch.setattr(module, "ServiceMindRepository", Repository)
    monkeypatch.setattr(module, "get_checkpoint_state", checkpoint)
    monkeypatch.setattr(module, "decline_run", decline)
    monkeypatch.setattr(module, "resolve_resume_scope", forbidden)
    await _worker()._execute_graph(run)
    assert observed == ["rejected"]


@pytest.mark.asyncio
async def test_busy_worker_approval_guard_does_not_persist_decision(monkeypatch):
    run = _run(status="waiting_approval")

    class Repository:
        def __init__(self, tenant):
            pass

        async def get_run(self, value):
            return run

        async def get_action_intent(self, value):
            return SimpleNamespace(action_hash="a" * 64)

    class Queue:
        def __init__(self, tenant):
            pass

        @asynccontextmanager
        async def execution_guard(self, *args, **kwargs):
            yield False

    monkeypatch.setattr(api, "get_run_execution_config", lambda: RunExecutionConfig(mode="worker"))
    monkeypatch.setattr(api, "ServiceMindRepository", Repository)
    monkeypatch.setattr(api, "RunQueue", Queue)
    context = TenantContext(
        tenant_id=run.tenant_id,
        user_id="approver",
        username="approver",
        roles={"approver"},
        allowed_glpi_entity_ids={1},
        allowed_glpi_group_ids={2},
    )
    with pytest.raises(HTTPException) as error:
        await api.approve_run(
            run.id, api.ApprovalRequest(decision="approved", expected_action_hash="a" * 64), context
        )
    assert error.value.status_code == 503


class _Queue:
    def __init__(self, *, renew=True):
        self.valid = renew
        self.releases = []

    @asynccontextmanager
    async def execution_guard(self, *args, **kwargs):
        yield True

    async def renew(self, *args, **kwargs):
        return self.valid

    async def release(self, *args, **kwargs):
        self.releases.append(kwargs)


@pytest.mark.asyncio
async def test_expired_claim_never_starts_a_graph(monkeypatch):
    queue = _Queue(renew=False)
    worker = _worker()

    async def forbidden(*args):
        pytest.fail("stale owner executed a graph")

    monkeypatch.setattr(worker, "_execute_graph", forbidden)
    await worker.execute_claimed(queue, _run(), "stale")


@pytest.mark.asyncio
async def test_unknown_authority_releases_with_bounded_retry_delay(monkeypatch):
    run = _run()
    queue = _Queue()
    worker = _worker()

    class Repository:
        def __init__(self, tenant):
            pass

        async def get_run(self, run_id):
            return run

    async def blocked(*args):
        raise module.ResumeBlocked("unavailable")

    monkeypatch.setattr(module, "ServiceMindRepository", Repository)
    monkeypatch.setattr(worker, "_execute_graph", blocked)
    await worker.execute_claimed(queue, run, "owner")
    assert queue.releases == [{"delay_seconds": module.BLOCKED_RETRY_SECONDS}]


@pytest.mark.asyncio
async def test_tenant_round_robin_moves_after_each_claim(monkeypatch):
    tenants = [uuid4(), uuid4()]
    observed = []

    async def catalog():
        return tenants

    class Queue:
        def __init__(self, tenant):
            self.tenant = tenant

        async def claim(self, *args, **kwargs):
            observed.append(self.tenant)
            return _run(tenant_id=self.tenant)

    monkeypatch.setattr(module, "list_tenant_ids", catalog)
    monkeypatch.setattr(module, "RunQueue", Queue)
    worker = _worker()
    await worker.claim_next()
    await worker.claim_next()
    assert observed == tenants


@pytest.mark.asyncio
async def test_heartbeat_loss_cancels_graph(monkeypatch):
    worker = _worker()
    queue = _Queue(renew=False)
    executing = asyncio.create_task(asyncio.Event().wait())

    async def immediate(_):
        return None

    monkeypatch.setattr(module.asyncio, "sleep", immediate)
    await worker._heartbeat(queue, uuid4(), "lost", executing)
    with pytest.raises(asyncio.CancelledError):
        await executing
