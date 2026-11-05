"""PostgreSQL checks for bounded, tenant-scoped run and event reads."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, update

from servicemind.persistence.database import global_session, tenant_session
from servicemind.persistence.models import AgentRun, RunEvent, RunStatus, Tenant
from servicemind.persistence.repository import ServiceMindRepository

pytestmark = [pytest.mark.asyncio, pytest.mark.docker, pytest.mark.postgres]


@pytest.fixture
def tenant_id():
    return uuid4()


async def _remove_tenant(tenant_id) -> None:
    async with tenant_session(tenant_id) as session:
        await session.execute(delete(RunEvent).where(RunEvent.tenant_id == tenant_id))
        await session.execute(delete(AgentRun).where(AgentRun.tenant_id == tenant_id))
    async with global_session() as session:
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))


async def _insert_tenant(tenant_id) -> None:
    async with global_session() as session:
        session.add(Tenant(id=tenant_id, slug=f"paging-{tenant_id.hex}", name="Paging test"))


async def test_event_pages_resume_without_loss_and_initial_event_is_atomic(tenant_id) -> None:
    await _insert_tenant(tenant_id)
    try:
        repository = ServiceMindRepository(tenant_id)
        run = await repository.create_run(
            user_id="paging-user",
            ticket_id=3001,
            goal="inspect ticket",
            request_write=False,
            event_payload={"ticket_id": 3001},
        )
        for number in range(4):
            await repository.append_event(run.id, f"stage.{number}", {"number": number})
        first = await repository.list_events(run.id, limit=2)
        second = await repository.list_events(run.id, after=first[-1].sequence, limit=2)
        third = await repository.list_events(run.id, after=second[-1].sequence, limit=2)
        assert [event.sequence for event in first + second + third] == [1, 2, 3, 4, 5]
        assert first[0].event_type == "run.created"
        assert [event.sequence for event in await repository.list_events(run.id)] == [1, 2, 3, 4, 5]
        with pytest.raises(ValueError, match="event page limit"):
            await repository.list_events(run.id, limit=0)
    finally:
        await _remove_tenant(tenant_id)


async def test_initial_event_failure_rolls_back_the_new_run(tenant_id) -> None:
    await _insert_tenant(tenant_id)
    try:
        with pytest.raises(TypeError, match="not JSON serializable"):
            await ServiceMindRepository(tenant_id).create_run(
                user_id="paging-user",
                ticket_id=3002,
                goal="inspect ticket",
                request_write=False,
                event_payload={"not_json_serializable": object()},
            )
        async with tenant_session(tenant_id) as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(AgentRun)
                    .where(AgentRun.tenant_id == tenant_id)
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(RunEvent)
                    .where(RunEvent.tenant_id == tenant_id)
                )
                == 0
            )
    finally:
        await _remove_tenant(tenant_id)


async def test_recoverable_runs_keyset_page_survives_equal_timestamps(tenant_id) -> None:
    await _insert_tenant(tenant_id)
    try:
        repository = ServiceMindRepository(tenant_id)
        runs = [
            await repository.create_run(
                user_id="paging-user",
                ticket_id=4000 + index,
                goal="recover test",
                request_write=False,
            )
            for index in range(5)
        ]
        for run, status in zip(
            runs[1:],
            [
                RunStatus.RUNNING,
                RunStatus.WAITING_APPROVAL,
                RunStatus.SUCCEEDED,
                RunStatus.WAITING_REVIEW,
            ],
            strict=True,
        ):
            await repository.update_run(run.id, status)
        fixed = datetime(2025, 1, 1, tzinfo=UTC)
        async with tenant_session(tenant_id) as session:
            await session.execute(
                update(AgentRun).where(AgentRun.tenant_id == tenant_id).values(created_at=fixed)
            )

        found = []
        cursor = None
        while True:
            page = await repository.list_recoverable_runs(
                limit=1,
                after_created_at=cursor.created_at if cursor else None,
                after_run_id=cursor.id if cursor else None,
            )
            if not page:
                break
            found.extend(page)
            cursor = page[-1]
        assert {run.id for run in found} == {run.id for run in runs[:3]}
        assert len(found) == 3
        assert [run.id for run in found] == [
            run.id for run in await repository.list_recoverable_runs()
        ]
        with pytest.raises(ValueError, match="cursor requires both"):
            await repository.list_recoverable_runs(after_created_at=fixed)
    finally:
        await _remove_tenant(tenant_id)
