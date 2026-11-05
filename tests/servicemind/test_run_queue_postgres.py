"""Real PostgreSQL coordination, including a worker process killed mid-lease."""

import asyncio
import os
import sys
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete

from servicemind.persistence.database import engine, global_session, tenant_session
from servicemind.persistence.models import ActionIntentRecord, AgentRun, Approval, RunStatus, Tenant
from servicemind.persistence.repository import ServiceMindRepository
from servicemind.persistence.run_queue import RunQueue

pytestmark = [pytest.mark.asyncio, pytest.mark.docker, pytest.mark.postgres]


@pytest_asyncio.fixture
async def tenants():
    values = [uuid4(), uuid4()]
    async with global_session() as session:
        session.add_all([Tenant(id=t, slug=f"worker-{t.hex}", name="Worker test") for t in values])
    try:
        yield values
    finally:
        for tenant in values:
            async with tenant_session(tenant) as session:
                await session.execute(delete(Approval).where(Approval.tenant_id == tenant))
                await session.execute(
                    delete(ActionIntentRecord).where(ActionIntentRecord.tenant_id == tenant)
                )
                await session.execute(delete(AgentRun).where(AgentRun.tenant_id == tenant))
        async with global_session() as session:
            await session.execute(delete(Tenant).where(Tenant.id.in_(values)))


async def _run(tenant):
    return await ServiceMindRepository(tenant).create_run(
        user_id="worker-test", ticket_id=42, goal="inspect VPN", request_write=False
    )


async def test_concurrent_claims_enforce_tenant_cap_and_leave_other_tenant_free(tenants):
    first, second = tenants
    await _run(first)
    await _run(first)
    await _run(second)
    claims = await asyncio.gather(
        *[
            RunQueue(first).claim(f"owner-{index}", lease_seconds=60, per_tenant_limit=1)
            for index in range(6)
        ]
    )
    assert sum(run is not None for run in claims) == 1
    assert await RunQueue(second).claim("other", lease_seconds=60, per_tenant_limit=1) is not None


async def test_guard_enforces_same_run_and_tenant_slots_even_after_lease_expiry(tenants):
    tenant = tenants[0]
    one, two = await _run(tenant), await _run(tenant)
    queue = RunQueue(tenant)
    async with queue.execution_guard(one.id, per_tenant_limit=1) as first:
        assert first
        async with queue.execution_guard(one.id, per_tenant_limit=1) as duplicate:
            assert not duplicate
        async with queue.execution_guard(two.id, per_tenant_limit=1) as excess:
            assert not excess
    async with queue.execution_guard(two.id, per_tenant_limit=1) as released:
        assert released


async def test_only_decided_approval_wait_is_recoverable(tenants):
    tenant = tenants[0]
    run = await _run(tenant)
    repository = ServiceMindRepository(tenant)
    await repository.update_run(run.id, RunStatus.WAITING_APPROVAL)
    queue = RunQueue(tenant)
    assert await queue.claim("before", lease_seconds=60, per_tenant_limit=1) is None
    async with tenant_session(tenant) as session:
        action = ActionIntentRecord(
            tenant_id=tenant,
            run_id=run.id,
            action_type="append_ticket_followup",
            target_id=42,
            arguments={},
            risk_level="low",
            requires_approval=True,
            action_hash="a" * 64,
            status="approved",
        )
        session.add(action)
        await session.flush()
        session.add(
            Approval(
                tenant_id=tenant,
                run_id=run.id,
                action_intent_id=action.id,
                decision="approved",
                decided_by="approver",
            )
        )
    recovered = await queue.claim("after", lease_seconds=60, per_tenant_limit=1)
    assert recovered is not None and recovered.id == run.id


async def test_killed_worker_releases_execution_lock_and_survivor_reclaims(tenants):
    tenant = tenants[0]
    run = await _run(tenant)
    code = """
import asyncio, sys
from uuid import UUID
from servicemind.persistence.run_queue import RunQueue
async def main():
    queue = RunQueue(UUID(sys.argv[1]))
    run = await queue.claim('killed-process', lease_seconds=60, per_tenant_limit=1)
    assert run is not None
    async with queue.execution_guard(run.id, per_tenant_limit=1) as locked:
        assert locked
        print('LOCKED', flush=True)
        await asyncio.Event().wait()
asyncio.run(main())
"""
    env = os.environ | {
        "PYTHON_DOTENV_DISABLED": "1",
        "SERVICEMIND_DATABASE_URL": engine.url.render_as_string(hide_password=False),
        "OPENAI_API_KEY": "sk-fake-worker-test",
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        str(tenant),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        assert process.stdout is not None
        assert await asyncio.wait_for(process.stdout.readline(), timeout=20) == b"LOCKED\n"
        queue = RunQueue(tenant)
        assert await queue.claim("too-early", lease_seconds=60, per_tenant_limit=1) is None
        # Interpreter startup and CI scheduling must not consume the short expiry
        # window before the first assertion. Start that window only once the child
        # has confirmed its lock, and let PostgreSQL decide when it has elapsed.
        assert await queue.renew(run.id, "killed-process", lease_seconds=2)
        process.kill()
        await process.wait()

        async def reclaim_after_expiry():
            while True:
                reclaimed = await queue.claim("survivor", lease_seconds=60, per_tenant_limit=1)
                if reclaimed is not None:
                    return reclaimed
                await asyncio.sleep(0.1)

        reclaimed = await asyncio.wait_for(reclaim_after_expiry(), timeout=15)
        assert reclaimed is not None and reclaimed.id == run.id
        assert not await queue.renew(run.id, "killed-process", lease_seconds=60)
        async with queue.execution_guard(run.id, per_tenant_limit=1) as locked:
            assert locked
        await ServiceMindRepository(tenant).update_run(run.id, RunStatus.SUCCEEDED)
        await queue.release(run.id, "survivor")
        assert await queue.claim("duplicate", lease_seconds=60, per_tenant_limit=1) is None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
