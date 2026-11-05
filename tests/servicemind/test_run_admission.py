"""Concurrent API admission obeys a tenant's durable task backlog limit."""

import asyncio
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select

from servicemind.persistence.database import global_session, tenant_session
from servicemind.persistence.models import AgentRun, IdempotencyRecord, RunEvent, RunStatus, Tenant
from servicemind.persistence.repository import RunQueueFull, ServiceMindRepository

pytestmark = [pytest.mark.asyncio, pytest.mark.docker, pytest.mark.postgres]


@pytest_asyncio.fixture
async def admission_tenant():
    tenant_id = uuid4()
    async with global_session() as session:
        session.add(Tenant(id=tenant_id, slug=f"admission-{tenant_id.hex}", name="Admission test"))
    try:
        yield tenant_id
    finally:
        async with tenant_session(tenant_id) as session:
            await session.execute(delete(RunEvent).where(RunEvent.tenant_id == tenant_id))
            await session.execute(delete(AgentRun).where(AgentRun.tenant_id == tenant_id))
            await session.execute(
                delete(IdempotencyRecord).where(IdempotencyRecord.tenant_id == tenant_id)
            )
        async with global_session() as session:
            await session.execute(delete(Tenant).where(Tenant.id == tenant_id))


async def test_concurrent_admission_cannot_overfill_the_last_tenant_slot(admission_tenant):
    repository = ServiceMindRepository(admission_tenant)

    async def submit(ticket_id):
        return await repository.create_run(
            user_id="admission-user",
            ticket_id=ticket_id,
            goal="inspect queued ticket",
            request_write=False,
            event_payload={"ticket_id": ticket_id},
            queue_limit=1,
        )

    results = await asyncio.gather(submit(5001), submit(5002), return_exceptions=True)
    assert sum(isinstance(result, AgentRun) for result in results) == 1
    assert sum(isinstance(result, RunQueueFull) for result in results) == 1
    async with tenant_session(admission_tenant) as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AgentRun)
                .where(AgentRun.tenant_id == admission_tenant)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(RunEvent)
                .where(RunEvent.tenant_id == admission_tenant)
            )
            == 1
        )


async def test_full_queue_accepts_duplicate_webhook_without_orphaning_rejected_receipt(
    admission_tenant,
):
    repository = ServiceMindRepository(admission_tenant)
    run_id = uuid4()
    submission = {
        "idempotency_key": "admission-webhook-original",
        "action_hash": "a" * 64,
        "run_id": run_id,
        "thread_id": f"admission-{run_id}",
        "ticket_id": 5003,
        "goal": "inspect webhook ticket",
        "event_payload": {"entity_id": 1},
        "queue_limit": 1,
    }
    accepted, duplicate = await repository.claim_webhook_run(**submission)
    retried, duplicate_retry = await repository.claim_webhook_run(**submission)
    assert accepted.id == retried.id == run_id
    assert duplicate is False
    assert duplicate_retry is True

    rejected_id = uuid4()
    with pytest.raises(RunQueueFull):
        await repository.claim_webhook_run(
            **{
                **submission,
                "idempotency_key": "admission-webhook-rejected",
                "run_id": rejected_id,
                "thread_id": f"admission-{rejected_id}",
            }
        )
    async with tenant_session(admission_tenant) as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AgentRun)
                .where(AgentRun.tenant_id == admission_tenant)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IdempotencyRecord)
                .where(IdempotencyRecord.tenant_id == admission_tenant)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(RunEvent)
                .where(RunEvent.tenant_id == admission_tenant)
            )
            == 1
        )


async def test_paused_run_does_not_occupy_an_admission_slot(admission_tenant):
    repository = ServiceMindRepository(admission_tenant)
    first = await repository.create_run(
        user_id="admission-user",
        ticket_id=5004,
        goal="inspect paused ticket",
        request_write=False,
        queue_limit=1,
    )
    await repository.update_run(first.id, RunStatus.WAITING_APPROVAL)
    second = await repository.create_run(
        user_id="admission-user",
        ticket_id=5005,
        goal="inspect queued ticket",
        request_write=False,
        queue_limit=1,
    )
    assert second.id != first.id
