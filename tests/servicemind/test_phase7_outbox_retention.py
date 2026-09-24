"""What the outbox does with the events it has already delivered.

``tool_outbox`` guarantees that an event is raised in the same transaction as the change
that caused it, published to Redis, retried on failure and dead-lettered after enough
attempts. What it did not do was stop: published rows were never removed, so the table's
steady state was "every approved action this tenant ever took", and the Redis stream had
no bound either, so the same unboundedness existed a second time on the other side of the
publish. Neither is visible in a test suite that runs for a minute, which is why both
survived.

The retention is a delivery record's retention, not a ledger's: the answer to "what did
this run do" is an ``audit_events`` row and the answer to "was this event delivered" is a
``published`` row inside the window. Two properties make the sweep safe, and both are
asserted here -- it removes only what it has delivered and only when that delivery has
aged out, and it never removes a ``dead`` row, because a dead row is the only place an
undelivered event is recorded at all.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from servicemind.reliability.outbox import PUBLISHED_RETENTION, OutboxRelay
from servicemind.reliability.worker import PRUNE_BATCHES_PER_SWEEP, _prune_expired

pytestmark = pytest.mark.asyncio

TENANT = UUID("11111111-1111-4111-8111-111111111111")
LIVE_TENANT = UUID("11111111-1111-4111-8111-111111111111")


class _ScriptedRelay:
    """A relay whose prune answers a scripted sequence of batch sizes.

    The loop under test is the one thing the production relay cannot show: it repeats
    until a batch comes back short, and it stops repeating at a ceiling. A relay that
    really queries the database would make each assertion depend on how many rows happen
    to be in a live tenant.
    """

    def __init__(self, batches: list[int], *, explode_on: UUID | None = None) -> None:
        self.batches = list(batches)
        self.explode_on = explode_on
        self.calls: list[UUID] = []

    async def prune(self, tenant_id: UUID, *, retention: timedelta = PUBLISHED_RETENTION) -> int:
        self.calls.append(tenant_id)
        if tenant_id == self.explode_on:
            raise RuntimeError("retention is unavailable for this tenant")
        return self.batches.pop(0) if self.batches else 0


async def test_the_sweep_repeats_until_a_batch_comes_back_short() -> None:
    """One page per hour would take weeks to catch up on a tenant with history.

    ``prune_published`` is bounded per call so the delivery loop never stalls behind a
    backlog, which means a single call is not a sweep. A tenant that has been accumulating
    since the platform was installed holds more aged-out rows than one page, and a sweep
    that removed one page an hour would be overtaken by the rate at which approvals create
    them -- so the sweep keeps going while full batches keep coming, and stops the moment
    one comes back short.
    """
    relay = _ScriptedRelay([1_000, 1_000, 3])

    await _prune_expired(relay, (TENANT,))

    assert relay.calls == [TENANT, TENANT, TENANT]


async def test_the_sweep_gives_up_between_batches_rather_than_inside_one() -> None:
    """The ceiling is what keeps a sweep from becoming a pause in a publishing loop.

    A tenant with an unbounded backlog would otherwise keep the loop here until it had
    drained, and the events waiting to be published behind it are the ones the platform
    promises to deliver. Stopping at the ceiling costs nothing: the next sweep is an hour
    away and takes the next batch.
    """
    relay = _ScriptedRelay([1_000] * 500)

    await _prune_expired(relay, (TENANT,))

    assert len(relay.calls) == PRUNE_BATCHES_PER_SWEEP


async def test_one_tenants_retention_failure_does_not_stop_the_others() -> None:
    """The worker is the only thing draining every tenant, and it must not be all-or-nothing.

    A sweep that raised out of the tenant loop would leave every later tenant's table
    growing for as long as one tenant's sweep kept failing -- the failure of the job that
    bounds growth would itself be unbounded.
    """
    other = uuid4()
    relay = _ScriptedRelay([1, 1], explode_on=TENANT)

    await _prune_expired(relay, (TENANT, other))

    assert other in relay.calls


@pytest.mark.docker
async def test_the_sweep_retires_delivered_rows_and_keeps_everything_else() -> None:
    """Asserted against PostgreSQL, because the claim is about what survives a DELETE.

    Five rows, because the interesting failures are a sweep that matches too much and one
    that measures the wrong clock. A predicate that forgot the status would take the
    pending row that has not been delivered; one that forgot the age would take the
    published row delivered a second ago; one that measured from ``created_at`` would take
    ``published-slow``, a row that sat in the queue for weeks and has been delivered for a
    minute -- charging it that wait would retire exactly the records that took longest to
    deliver. The dead row is the one that matters most: it is the only place an
    undelivered event is recorded at all, so a sweep that removed it would erase the
    evidence rather than the backlog.

    This test cleans up after itself. ``tool_outbox`` is a queue, not a ledger, so unlike
    the governance tables it accepts DELETE -- which is exactly what makes a retention
    possible, and is why the assertions can be about absence rather than about counts.
    """
    from sqlalchemy import select

    from servicemind.persistence.database import tenant_session
    from servicemind.persistence.models import ToolOutboxRecord

    marker = uuid4().hex[:12]
    relay = OutboxRelay.__new__(OutboxRelay)
    now = datetime.now(UTC)
    stale = now - PUBLISHED_RETENTION - timedelta(days=1)
    rows = {
        "published-stale": ("published", stale, stale),
        "published-fresh": ("published", now, now),
        "published-slow": ("published", now, stale),
        "pending-stale": ("pending", stale, stale),
        "dead-stale": ("dead", stale, stale),
    }
    ids: dict[str, UUID] = {}
    async with tenant_session(LIVE_TENANT) as session:
        for name, (status, updated_at, created_at) in rows.items():
            row = ToolOutboxRecord(
                id=uuid4(),
                tenant_id=LIVE_TENANT,
                aggregate_type="ActionIntent",
                aggregate_id=f"retention-{marker}-{name}",
                event_type="action.approved",
                payload={"marker": marker},
                idempotency_key=f"retention-{marker}-{name}",
                status=status,
                updated_at=updated_at,
                created_at=created_at,
            )
            session.add(row)
            ids[name] = row.id
        await session.flush()

    try:
        removed = await relay.prune(LIVE_TENANT)
        assert removed == 1, "exactly the aged-out published row is retired"

        async with tenant_session(LIVE_TENANT) as session:
            surviving = set(
                (
                    await session.execute(
                        select(ToolOutboxRecord.aggregate_id).where(
                            ToolOutboxRecord.aggregate_id.like(f"retention-{marker}-%")
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert surviving == {
            f"retention-{marker}-published-fresh",
            f"retention-{marker}-published-slow",
            f"retention-{marker}-pending-stale",
            f"retention-{marker}-dead-stale",
        }
    finally:
        async with tenant_session(LIVE_TENANT) as session:
            for row_id in ids.values():
                row = (
                    await session.execute(
                        select(ToolOutboxRecord).where(ToolOutboxRecord.id == row_id)
                    )
                ).scalar_one_or_none()
                if row is not None:
                    await session.delete(row)
            await session.flush()


@pytest.mark.docker
async def test_the_sweep_leaves_another_tenants_rows_alone() -> None:
    """RLS is what makes a DELETE safe here, and a delete is the one operation it protects least.

    Every other outbox operation reads or updates a row it already identified by id. This
    one *selects* what to delete, so the tenant predicate is doing load-bearing work
    rather than confirming a lookup -- if it were absent or wrong, the sweep would retire
    another tenant's delivery records and nothing else in the platform would notice.
    """
    from sqlalchemy import select

    from servicemind.persistence.database import tenant_session
    from servicemind.persistence.models import ToolOutboxRecord

    ghost = UUID("99999999-9999-4999-8999-999999999999")
    marker = uuid4().hex[:12]
    stale = datetime.now(UTC) - PUBLISHED_RETENTION - timedelta(days=1)
    async with tenant_session(LIVE_TENANT) as session:
        session.add(
            ToolOutboxRecord(
                id=uuid4(),
                tenant_id=LIVE_TENANT,
                aggregate_type="ActionIntent",
                aggregate_id=f"retention-{marker}-tenancy",
                event_type="action.approved",
                payload={"tenant": str(ghost)},
                idempotency_key=f"retention-{marker}-tenancy",
                status="published",
                updated_at=stale,
                created_at=stale,
            )
        )
        await session.flush()

    # A relay pointed at a tenant that is not the row's owner must find nothing of ours
    # to remove. The ghost tenant has no rows at all, so the assertion is that the
    # operation is a no-op rather than that it errors.
    removed = await OutboxRelay.__new__(OutboxRelay).prune(ghost)
    assert removed == 0

    try:
        async with tenant_session(LIVE_TENANT) as session:
            remaining = (
                await session.execute(
                    select(ToolOutboxRecord.id).where(
                        ToolOutboxRecord.aggregate_id == f"retention-{marker}-tenancy"
                    )
                )
            ).scalar_one_or_none()
        assert remaining is not None
    finally:
        async with tenant_session(LIVE_TENANT) as session:
            row = (
                await session.execute(
                    select(ToolOutboxRecord).where(
                        ToolOutboxRecord.aggregate_id == f"retention-{marker}-tenancy"
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                await session.delete(row)
            await session.flush()


async def test_the_sweep_does_not_run_on_every_delivery() -> None:
    """The worker's two cadences are different on purpose, and this pins the difference.

    Delivery runs about once a second; retention is measured in weeks. A sweep on the
    delivery cadence would scan an indexed table sixty times a minute to remove nothing,
    and the cost of publishing one event would grow with the tenant's history. The check
    is on the source rather than on a running loop because the loop only ends when the
    process is signalled -- the schedule is the claim.
    """
    from pathlib import Path

    source = Path("src/servicemind/reliability/worker.py").read_text(encoding="utf-8")
    assert "PRUNE_INTERVAL_SECONDS" in source
    assert "await _prune_expired(relay, tenant_ids)" in source
    assert source.index("if loop.time() >= next_prune:") < source.index(
        "await _prune_expired(relay, tenant_ids)"
    )


async def test_a_sweep_that_is_cancelled_propagates_the_cancellation() -> None:
    """Cancellation is not a failure to log and retry; it is the process shutting down.

    The sweep sits inside the same loop that the signal handler stops, so a cancellation
    swallowed here would keep the worker alive past its shutdown and leave the Redis
    client open behind it.
    """

    class Hanging:
        async def prune(self, tenant_id, *, retention=PUBLISHED_RETENTION):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _prune_expired(Hanging(), (TENANT,))
