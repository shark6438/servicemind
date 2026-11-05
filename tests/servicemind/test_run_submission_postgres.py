"""Real PostgreSQL request deduplication and transactional replay evidence."""

import asyncio
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete, func, select
from sqlalchemy.exc import StatementError

from servicemind import api
from servicemind.orchestration.execution_config import RunExecutionConfig
from servicemind.persistence.database import engine, global_session, tenant_session
from servicemind.persistence.models import AgentRun, RunEvent, RunSubmissionRecord, Tenant
from servicemind.persistence.repository import (
    RunQueueFull,
    RunSubmissionConflict,
    ServiceMindRepository,
)
from servicemind.security.auth import TenantContext, get_tenant_context

pytestmark = [pytest.mark.asyncio, pytest.mark.docker, pytest.mark.postgres]
BODY: dict[str, Any] = {"ticket_id": 42, "goal": "inspect VPN ticket", "request_write": False}
SCOPE = {"entity_ids": [1], "group_ids": []}


@pytest_asyncio.fixture
async def submission_tenants():
    # pytest-asyncio gives each test its own loop. A saturated async pool binds its
    # wait queue to that loop, so do not carry it into the next 100-request burst.
    await engine.dispose()
    tenants = [uuid4(), uuid4()]
    async with global_session() as session:
        session.add_all(
            [
                Tenant(id=value, slug=f"submission-{value.hex}", name="Submission test")
                for value in tenants
            ]
        )
    try:
        yield tenants
    finally:
        for tenant in tenants:
            async with tenant_session(tenant) as session:
                await session.execute(
                    delete(RunSubmissionRecord).where(RunSubmissionRecord.tenant_id == tenant)
                )
                await session.execute(delete(RunEvent).where(RunEvent.tenant_id == tenant))
                await session.execute(delete(AgentRun).where(AgentRun.tenant_id == tenant))
        async with global_session() as session:
            await session.execute(delete(Tenant).where(Tenant.id.in_(tenants)))
        await engine.dispose()


def _app(tenant, monkeypatch, queue_limit=1):
    app = FastAPI()
    app.include_router(api.phase2_router)
    context = TenantContext(
        tenant_id=tenant,
        user_id="requester",
        username="requester",
        roles={"analyst"},
        allowed_glpi_entity_ids={1},
    )
    app.dependency_overrides[get_tenant_context] = lambda: context
    monkeypatch.setattr(
        api,
        "get_run_execution_config",
        lambda: RunExecutionConfig(mode="worker", queue_limit=queue_limit),
    )
    return app


async def test_100_concurrent_http_retries_create_one_task_event_and_receipt(
    submission_tenants, monkeypatch
):
    tenant = submission_tenants[0]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(tenant, monkeypatch)), base_url="http://test"
    ) as client:
        responses = await asyncio.gather(
            *[
                client.post(
                    "/v1/servicemind/runs", headers={"Idempotency-Key": "concurrent-key"}, json=BODY
                )
                for _ in range(100)
            ]
        )
        assert {response.status_code for response in responses} == {202}
        ids = {response.json()["id"] for response in responses}
        assert len(ids) == 1
        # Explicit/default false and JSON member ordering share a canonical body.
        replay = await client.post(
            "/v1/servicemind/runs",
            headers={"Idempotency-Key": "concurrent-key"},
            json={"goal": BODY["goal"], "ticket_id": 42},
        )
        assert replay.status_code == 202 and replay.json()["id"] in ids
    async with tenant_session(tenant) as session:
        for model in (AgentRun, RunEvent, RunSubmissionRecord):
            assert (
                await session.scalar(
                    select(func.count()).select_from(model).where(model.tenant_id == tenant)
                )
                == 1
            )


async def test_100_repeated_submissions_before_and_after_keyed_admission(
    submission_tenants, monkeypatch
):
    """Same payload/load/capacity: optional retry key changes 100 tasks into one."""
    task_counts = []
    for tenant, headers in zip(
        submission_tenants, [{}, {"Idempotency-Key": "before-after-key"}], strict=True
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app(tenant, monkeypatch, queue_limit=200)),
            base_url="http://test",
        ) as client:
            responses = await asyncio.gather(
                *[
                    client.post("/v1/servicemind/runs", headers=headers, json=BODY)
                    for _ in range(100)
                ]
            )
        assert {response.status_code for response in responses} == {202}
        task_counts.append(len({response.json()["id"] for response in responses}))
    assert task_counts == [100, 1]


async def test_retry_after_postcommit_response_loss_returns_original_task(
    submission_tenants, monkeypatch
):
    tenant = submission_tenants[0]

    class LoseFirstResponse(httpx.AsyncBaseTransport):
        def __init__(self):
            self.backend = httpx.ASGITransport(app=_app(tenant, monkeypatch))
            self.first = True

        async def handle_async_request(self, request):
            response = await self.backend.handle_async_request(request)
            await response.aread()
            if self.first:
                self.first = False
                raise httpx.ReadError("simulated loss after admission committed", request=request)
            return response

    async with httpx.AsyncClient(transport=LoseFirstResponse(), base_url="http://test") as client:
        with pytest.raises(httpx.ReadError, match="after admission committed"):
            await client.post(
                "/v1/servicemind/runs", headers={"Idempotency-Key": "lost-response"}, json=BODY
            )
        stored = await ServiceMindRepository(tenant).list_runs()
        assert len(stored) == 1
        response = await client.post(
            "/v1/servicemind/runs", headers={"Idempotency-Key": "lost-response"}, json=BODY
        )
        assert response.status_code == 202 and UUID(response.json()["id"]) == stored[0].id


async def test_retry_namespaces_are_user_and_tenant_scoped_with_real_rls(submission_tenants):
    one, two = submission_tenants
    results = []
    for tenant, user in [(one, "alice"), (one, "bob"), (two, "alice")]:
        run, created = await ServiceMindRepository(tenant).create_run_idempotent(
            idempotency_key="shared-key", user_id=user, access_scope=SCOPE, **BODY
        )
        assert created
        results.append(run)
    assert len({run.id for run in results}) == 3
    async with tenant_session(two) as session:
        # No tenant predicate: PostgreSQL itself excludes the two foreign receipts.
        receipts = list((await session.scalars(select(RunSubmissionRecord))).all())
        assert len(receipts) == 1 and receipts[0].run_id == results[2].id


async def test_failed_initial_event_rolls_back_task_and_retry_receipt(submission_tenants):
    tenant = submission_tenants[0]
    repository = ServiceMindRepository(tenant)
    with pytest.raises((TypeError, StatementError)):
        await repository.create_run_idempotent(
            idempotency_key="rollback-key",
            user_id="requester",
            event_payload={"invalid": object()},
            **BODY,
        )
    async with tenant_session(tenant) as session:
        for model in (AgentRun, RunEvent, RunSubmissionRecord):
            assert (
                await session.scalar(
                    select(func.count()).select_from(model).where(model.tenant_id == tenant)
                )
                == 0
            )
    _, created = await repository.create_run_idempotent(
        idempotency_key="rollback-key", user_id="requester", **BODY
    )
    assert created


async def test_replay_precedes_full_queue_admission_and_conflicts_leave_no_new_rows(
    submission_tenants,
):
    tenant = submission_tenants[0]
    repository = ServiceMindRepository(tenant)
    run, created = await repository.create_run_idempotent(
        idempotency_key="original", user_id="requester", queue_limit=1, **BODY
    )
    assert created
    same, created = await repository.create_run_idempotent(
        idempotency_key="original", user_id="requester", queue_limit=1, **BODY
    )
    assert not created and same.id == run.id
    with pytest.raises(RunSubmissionConflict):
        await repository.create_run_idempotent(
            idempotency_key="original",
            user_id="requester",
            queue_limit=1,
            ticket_id=42,
            goal="inspect VPN ticket",
            request_write=True,
        )
    with pytest.raises(RunQueueFull):
        await repository.create_run_idempotent(
            idempotency_key="new-key", user_id="requester", queue_limit=1, **BODY
        )
    async with tenant_session(tenant) as session:
        assert await session.scalar(select(func.count()).select_from(RunSubmissionRecord)) == 1
