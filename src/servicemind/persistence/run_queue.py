"""PostgreSQL leases for independently deployed Agent run workers.

The run row is the durable queue item. A tenant transaction lock serializes capacity
checks across workers; a row lease prevents two processes from claiming one run. The
worker never obtains a requester's authority from these columns.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import UUID

from sqlalchemy import and_, exists, func, or_, select, text, update
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from servicemind.persistence.database import engine, tenant_session
from servicemind.persistence.models import AgentRun, Approval, RunStatus

ACTIVE_STATUSES = (RunStatus.PENDING.value, RunStatus.RUNNING.value)
# Long-lived execution locks must not exhaust the repository's query pool. Closing
# a NullPool connection also releases every session advisory lock after cancellation.
guard_engine = create_async_engine(engine.url, poolclass=NullPool, pool_pre_ping=True)


class RunQueue:
    def __init__(self, tenant_id: UUID) -> None:
        self.tenant_id = tenant_id

    def _runnable(self):
        # A decision can commit just before the API process exits. Undecided human
        # waits consume no execution slot; decided waits must remain recoverable.
        return or_(
            AgentRun.status.in_(ACTIVE_STATUSES),
            and_(
                AgentRun.status == RunStatus.WAITING_APPROVAL.value,
                exists(
                    select(Approval.id).where(
                        Approval.run_id == AgentRun.id,
                        Approval.tenant_id == self.tenant_id,
                    )
                ),
            ),
        )

    @asynccontextmanager
    async def execution_guard(self, run_id: UUID, *, per_tenant_limit: int) -> AsyncIterator[bool]:
        """Keep one PostgreSQL session lock for the whole graph invocation.

        A heartbeat may be delayed beyond its lease while the original process is
        alive. The next claimant cannot execute concurrently because this lock is
        released only when that process exits or finishes the graph.
        """
        if per_tenant_limit < 1:
            raise ValueError("tenant limit must be positive")
        key = f"servicemind:run-execute:{run_id}"
        async with guard_engine.connect() as connection:
            obtained = await connection.scalar(
                text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"),
                {"key": key},
            )
            slot_key: str | None = None
            if obtained:
                for slot in range(per_tenant_limit):
                    candidate = f"servicemind:tenant-slot:{self.tenant_id}:{slot}"
                    if await connection.scalar(
                        text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"),
                        {"key": candidate},
                    ):
                        slot_key = candidate
                        break
            # Advisory locks are session scoped and survive COMMIT. Do not leave an
            # idle database transaction open during a minutes-long graph invocation.
            await connection.commit()
            try:
                yield bool(obtained and slot_key is not None)
            finally:
                if slot_key is not None:
                    await connection.scalar(
                        text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"),
                        {"key": slot_key},
                    )
                if obtained:
                    await connection.scalar(
                        text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"),
                        {"key": key},
                    )
                await connection.commit()

    async def claim(
        self, owner: str, *, lease_seconds: int, per_tenant_limit: int
    ) -> AgentRun | None:
        """Atomically reserve one runnable run when this tenant has a free slot."""
        if lease_seconds < 1 or per_tenant_limit < 1:
            raise ValueError("lease and tenant limit must be positive")
        async with tenant_session(self.tenant_id) as session:
            # The row lock protects a selected run. This transaction lock also
            # serializes the count-and-claim across different runs of one tenant.
            obtained = await session.scalar(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"servicemind:run-capacity:{self.tenant_id}"},
            )
            if not obtained:
                return None
            active = await session.scalar(
                select(func.count(AgentRun.id)).where(
                    AgentRun.tenant_id == self.tenant_id,
                    self._runnable(),
                    AgentRun.worker_lease_owner.is_not(None),
                    AgentRun.worker_lease_expires_at > func.now(),
                )
            )
            if int(active or 0) >= per_tenant_limit:
                return None
            run = (
                await session.execute(
                    select(AgentRun)
                    .where(
                        AgentRun.tenant_id == self.tenant_id,
                        self._runnable(),
                        or_(
                            AgentRun.worker_lease_expires_at.is_(None),
                            AgentRun.worker_lease_expires_at <= func.now(),
                        ),
                    )
                    .order_by(AgentRun.created_at, AgentRun.id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if run is None:
                return None
            database_now = await session.scalar(select(func.now()))
            assert database_now is not None
            run.worker_lease_owner = owner
            run.worker_lease_expires_at = database_now + timedelta(seconds=lease_seconds)
            await session.flush()
            return run

    async def renew(self, run_id: UUID, owner: str, *, lease_seconds: int) -> bool:
        """A lost lease cannot be revived by the former owner."""
        async with tenant_session(self.tenant_id) as session:
            renewed = (
                await session.execute(
                    update(AgentRun)
                    .where(
                        AgentRun.id == run_id,
                        AgentRun.tenant_id == self.tenant_id,
                        AgentRun.worker_lease_owner == owner,
                        AgentRun.worker_lease_expires_at > func.now(),
                    )
                    .values(worker_lease_expires_at=func.now() + timedelta(seconds=lease_seconds))
                    .returning(AgentRun.id)
                )
            ).scalar_one_or_none()
            return renewed is not None

    async def release(self, run_id: UUID, owner: str, *, delay_seconds: int = 0) -> bool:
        """Release a finished run, or defer a temporarily blocked one."""
        if delay_seconds < 0:
            raise ValueError("delay must be nonnegative")
        async with tenant_session(self.tenant_id) as session:
            released = (
                await session.execute(
                    update(AgentRun)
                    .where(
                        AgentRun.id == run_id,
                        AgentRun.tenant_id == self.tenant_id,
                        AgentRun.worker_lease_owner == owner,
                    )
                    .values(
                        worker_lease_owner=None,
                        worker_lease_expires_at=(
                            func.now() + timedelta(seconds=delay_seconds) if delay_seconds else None
                        ),
                    )
                    .returning(AgentRun.id)
                )
            ).scalar_one_or_none()
            return released is not None
