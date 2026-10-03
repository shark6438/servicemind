"""The memory version chain, dedupe and conflict lifecycle as PostgreSQL runs them.

Every assertion in this repository about memory versioning was made against
``InMemoryMemoryRepository``. The deployment runs ``PostgresMemoryRepository``, and the
two answer "is this the same subject as something already stored?" in differently shaped
code: the in-memory one compares ``record.scope == candidate.scope``, the Postgres one
compares two columns. Two implementations of one contract with only one of them tested is
not a missing branch -- it is a whole fork hidden from the suite, and a regression in the
untested half would be reported as a pass.

This file closes the fork. It drives the production repository through the real
``tenant_session`` RLS path against PostgreSQL and asserts the behaviours the in-memory
tests assert, so a divergence between the two fails here instead of passing there.

**What the audit claimed, and what measuring it found.** The audit reported that the
Postgres predecessor lookup was dead for tenant-scoped records: ``scope_id`` is NULL for a
tenant scope (``ck_memory_scope_id``), ``NULL = NULL`` is never true, so the lookup would
find no predecessor, every write would be version 1, and subject dedupe would degrade
into "store a new record every time". The claim is false as written.
``candidate.scope.scope_id`` is a Python ``None`` at expression-build time, and SQLAlchemy
compiles ``Column == None`` to ``scope_id IS NULL`` rather than to a bound parameter, so
the tenant-scope lookup does match. The live data agrees, and a lookup that never found a
predecessor cannot produce what is in the table: tenant ``2222...`` holds 37 subjects of
which 18 carry more than one version and the deepest chain is at version 406; tenant
``1111...`` holds 316 subjects, 49 of them multi-version. ``version`` is
``previous.version + 1 if previous else 1``, so every one of those increments is a
successful predecessor lookup. ``test_a_tenant_scoped_subject_chains_versions`` pins that
as behaviour, and is the regression test whose absence made the claim plausible.

The rows written here are not cleaned up and cannot be: ``memory_events`` rejects UPDATE
and DELETE at the database, so the ledger a test writes is the ledger the tenant keeps.
Subject keys and trace ids carry a per-run marker, which is how the rows stay
identifiable, and the ``agent_runs`` rows the foreign keys require are created with the
marker in their goal -- the convention ``test_memory_event_correlation.py`` and
``scripts/verify_phase5_governance.py`` already use.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from servicemind.memory.contracts import (
    MemoryCandidate,
    MemoryEvidenceRef,
    MemoryScope,
    MemoryScopeType,
    MemoryStatus,
    MemoryType,
    MemoryWriteAction,
    MemoryWriteDecision,
    SemanticSubtype,
)
from servicemind.memory.repository import PostgresMemoryRepository

pytestmark = pytest.mark.asyncio

LIVE_TENANT = UUID("11111111-1111-4111-8111-111111111111")


def _evidence(marker: str) -> MemoryEvidenceRef:
    return MemoryEvidenceRef(
        evidence_id=f"ev-postgres-chain-{marker}",
        source_ref=f"kb://postgres-chain/{marker}",
        content_hash="a" * 64,
        verified=True,
    )


def _candidate(marker: str, **overrides: object) -> MemoryCandidate:
    payload: dict[str, object] = {
        "tenant_id": LIVE_TENANT,
        "scope": MemoryScope(),
        "memory_type": MemoryType.SEMANTIC,
        "semantic_subtype": SemanticSubtype.LEARNED_FACT,
        "subject_key": f"postgres-chain-{marker}",
        "content": "VPN incidents are handled by Network Team",
        "source_run_id": None,
        "source_trace_id": f"trace-postgres-chain-{marker}",
        "evidence_refs": (_evidence(marker),),
        "confidence": 0.95,
        "importance": 0.8,
        "created_by": "test-postgres-memory",
        "valid_from": datetime.now(UTC) - timedelta(minutes=1),
    }
    payload.update(overrides)
    return MemoryCandidate.model_validate(payload)


def _activate() -> MemoryWriteDecision:
    return MemoryWriteDecision(
        action=MemoryWriteAction.ACTIVATE, reason_codes=("SEMANTIC_LEARNED_FACT",)
    )


async def _runs(marker: str, count: int) -> list[UUID]:
    """Real ``agent_runs`` rows: ``memory_records.source_run_id`` is a foreign key.

    ``source_run_id`` is also part of the candidate's ``idempotency_key``, so it is the
    only way to give two otherwise identical candidates two different idempotency keys --
    which is what makes the write exercise subject dedupe instead of the idempotency
    lookup that sits in front of it.
    """
    from servicemind.persistence.repository import ServiceMindRepository

    repository = ServiceMindRepository(LIVE_TENANT)
    created = []
    for index in range(count):
        run = await repository.create_run(
            user_id="postgres-memory-probe",
            ticket_id=0,
            goal=f"prove the Postgres memory chain ({marker} writer {index})",
            request_write=False,
        )
        created.append(run.id)
    return created


async def _stored_versions(subject_key: str) -> list[tuple[int, str]]:
    """Read the subject back out of the table, not out of the returned domain objects."""
    from sqlalchemy import select

    from servicemind.persistence.database import tenant_session
    from servicemind.persistence.models import MemoryRecordRow

    async with tenant_session(LIVE_TENANT) as session:
        rows = (
            await session.execute(
                select(MemoryRecordRow.version, MemoryRecordRow.status)
                .where(MemoryRecordRow.subject_key == subject_key)
                .order_by(MemoryRecordRow.version)
            )
        ).all()
    return [(row.version, row.status) for row in rows]


# ------------------------------------------------------------------- the version chain


@pytest.mark.postgres
@pytest.mark.docker
async def test_a_tenant_scoped_subject_chains_versions() -> None:
    """The claim under test is about the SQL a tenant scope produces.

    A tenant scope has no ``scope_id``, so the predecessor lookup asks for a NULL column.
    The audit read that as a dead predicate; the compiled statement asks ``IS NULL``, and
    the second write of a changed subject must therefore be version 2 of the first rather
    than a first version of its own. Version 1 twice is the failure this test exists to
    catch, and it is asserted against the table so that a repository returning the right
    domain object from a wrong row cannot pass.
    """
    marker = uuid4().hex[:12]
    repository = PostgresMemoryRepository(LIVE_TENANT)
    subject = f"postgres-chain-{marker}"

    first = await repository.persist(_candidate(marker), _activate())
    assert first is not None and first.version == 1
    assert first.status is MemoryStatus.ACTIVE

    second = await repository.persist(
        _candidate(marker, content="VPN incidents are handled by Identity Team"), _activate()
    )
    assert second is not None
    assert second.version == 2
    # The predecessor is identified by its lineage, which is the whole point of the
    # version chain: two rows that are versions of one subject must share one lineage.
    assert second.lineage_id == first.lineage_id

    assert await _stored_versions(subject) == [(1, "active"), (2, "quarantine")]


@pytest.mark.postgres
@pytest.mark.docker
async def test_an_identical_reaffirmation_returns_the_stored_row_without_a_new_version() -> None:
    """Exact dedupe, reached through subject identity rather than through the key.

    The second candidate differs from the first only in the run that produced it, so its
    ``idempotency_key`` is different and the idempotency lookup cannot answer. What must
    answer is the subject rule: identical content against a live predecessor is the same
    fact restated, and restating a fact must not grow its history.
    """
    marker = uuid4().hex[:12]
    repository = PostgresMemoryRepository(LIVE_TENANT)
    run, other_run = await _runs(marker, 2)

    first = await repository.persist(_candidate(marker, source_run_id=run), _activate())
    assert first is not None

    duplicate = await repository.persist(_candidate(marker, source_run_id=other_run), _activate())

    assert duplicate is not None
    assert duplicate.memory_id == first.memory_id
    assert duplicate.version == 1
    assert await _stored_versions(f"postgres-chain-{marker}") == [(1, "active")]


@pytest.mark.postgres
@pytest.mark.docker
async def test_a_conflicting_rewrite_quarantines_the_next_version_and_review_supersedes_the_first() -> (
    None
):
    """The conflict lifecycle, which only ever ran in memory: v2 is reviewed, v1 retires.

    A changed rewrite of a live subject must not become a second live fact, and the
    version it creates must not retire the first one either -- the live fact stays live
    until a human accepts the replacement. The transition is where the retirement happens,
    so asserting only the write would leave the second half of the lifecycle unmeasured.
    """
    marker = uuid4().hex[:12]
    repository = PostgresMemoryRepository(LIVE_TENANT)
    subject = f"postgres-chain-{marker}"

    first = await repository.persist(_candidate(marker), _activate())
    assert first is not None

    conflict = await repository.persist(
        _candidate(marker, content="VPN incidents are handled by Identity Team"), _activate()
    )
    assert conflict is not None
    assert conflict.status is MemoryStatus.QUARANTINE
    assert conflict.version == 2
    # The live fact survives the conflict: a quarantined proposal is not a replacement.
    assert await _stored_versions(subject) == [(1, "active"), (2, "quarantine")]

    activated = await repository.transition(
        conflict.memory_id,
        MemoryStatus.ACTIVE,
        actor_id="expert",
        reason="conflict_resolved",
        human_review_ref="review://conflict-resolution",
    )
    assert activated.status is MemoryStatus.ACTIVE
    assert await _stored_versions(subject) == [(1, "superseded"), (2, "active")]


@pytest.mark.postgres
@pytest.mark.docker
async def test_a_revoked_subject_relearn_lands_as_a_new_quarantine_version() -> None:
    """A revoked fact must not deadlock its subject, on the implementation that stores it.

    The revocation is authoritative and the fact's content did not change, so the relearn
    is not a restatement -- it re-enters review at the next version. Returning the revoked
    row instead would be counted as a successful store of something nobody can read.
    """
    marker = uuid4().hex[:12]
    repository = PostgresMemoryRepository(LIVE_TENANT)
    subject = f"postgres-chain-{marker}"
    first_run, later_run = await _runs(marker, 2)

    original = await repository.persist(_candidate(marker, source_run_id=first_run), _activate())
    assert original is not None and original.status is MemoryStatus.ACTIVE

    revoked = await repository.revoke_by_evidence(
        _evidence(marker).evidence_id,
        tenant_id=LIVE_TENANT,
        actor_id="knowledge-base",
        reason="source revoked",
    )
    assert revoked == 1

    # Learning it again is a different request. A replay of *the same* request -- same
    # run, same content -- is answered from the idempotency key and returns the revoked
    # row, and that is the same thing the in-memory repository does: the key records that
    # this request was served, and re-serving it differently would make it not idempotent.
    relearned = await repository.persist(_candidate(marker, source_run_id=later_run), _activate())
    assert relearned is not None
    assert relearned.memory_id != original.memory_id
    assert relearned.status is MemoryStatus.QUARANTINE
    assert relearned.version == 2
    assert await _stored_versions(subject) == [(1, "revoked"), (2, "quarantine")]


@pytest.mark.postgres
@pytest.mark.docker
async def test_an_expired_subject_relearn_refreshes_its_ttl_as_a_new_active_version() -> None:
    """Lazy expiry has to run before the predecessor is read, or the lapse is invisible.

    The first row is ACTIVE in the table but already past its TTL. If the sweep ran after
    the predecessor lookup, the lookup would see ACTIVE with identical content and return
    the lapsed row as a successful store -- a memory the reader cannot see, reported as
    stored. The reaffirmation must instead be a new ACTIVE version and the lapsed row must
    reach its terminal status.
    """
    marker = uuid4().hex[:12]
    now = datetime.now(UTC)
    repository = PostgresMemoryRepository(LIVE_TENANT)
    subject = f"postgres-chain-{marker}"
    first_run, later_run = await _runs(marker, 2)

    lapsed = await repository.persist(
        _candidate(
            marker,
            source_run_id=first_run,
            valid_from=now - timedelta(days=2),
            expires_at=now - timedelta(days=1),
        ),
        _activate(),
    )
    assert lapsed is not None and lapsed.status is MemoryStatus.ACTIVE

    # A separate request, because a replay of the first would be answered by the
    # idempotency key without the TTL being looked at at all.
    refreshed = await repository.persist(
        _candidate(marker, source_run_id=later_run, expires_at=now + timedelta(days=30)),
        _activate(),
    )

    assert refreshed is not None
    assert refreshed.status is MemoryStatus.ACTIVE
    assert refreshed.version == 2
    assert await _stored_versions(subject) == [(1, "expired"), (2, "active")]


@pytest.mark.postgres
@pytest.mark.docker
async def test_the_same_subject_key_in_two_scope_types_is_two_subjects() -> None:
    """Scope is a filter on the predecessor, not decoration on the row.

    A tenant-wide fact and one person's fact may share a subject key and mean different
    things by it. The in-memory implementation gets this from comparing whole scope
    objects; the Postgres one has to compare two columns, and a lookup that compared only
    the key would quarantine the second write as a conflict with the first. Both orderings
    are written, because a filter that keys on one column and ignores the other passes in
    exactly one of them.
    """
    marker = uuid4().hex[:12]
    repository = PostgresMemoryRepository(LIVE_TENANT)
    person = MemoryScope(scope_type=MemoryScopeType.USER, scope_id=f"alice-{marker}")

    tenant_first = await repository.persist(_candidate(marker), _activate())
    user_scoped = await repository.persist(
        _candidate(marker, scope=person, content="Alice prefers the console view"), _activate()
    )
    assert tenant_first is not None and user_scoped is not None
    assert (tenant_first.version, user_scoped.version) == (1, 1)
    assert user_scoped.status is MemoryStatus.ACTIVE
    assert user_scoped.lineage_id != tenant_first.lineage_id

    other = uuid4().hex[:12]
    user_first = await repository.persist(
        _candidate(other, scope=person, content="Alice prefers the console view"), _activate()
    )
    tenant_second = await repository.persist(
        _candidate(other, content="VPN incidents are handled by Identity Team"), _activate()
    )
    assert user_first is not None and tenant_second is not None
    assert (user_first.version, tenant_second.version) == (1, 1)
    assert tenant_second.status is MemoryStatus.ACTIVE

    # Two people, one subject key. This is the case the scope *id* is for: the two rows
    # agree on scope_type, so a lookup that filtered on the type alone would chain them,
    # and the difference is only visible when both are non-tenant.
    third = uuid4().hex[:12]
    colleague = MemoryScope(scope_type=MemoryScopeType.USER, scope_id=f"bob-{third}")
    alice = await repository.persist(
        _candidate(
            third,
            scope=MemoryScope(scope_type=MemoryScopeType.USER, scope_id=f"alice-{third}"),
            content="Alice view",
        ),
        _activate(),
    )
    bob = await repository.persist(
        _candidate(third, scope=colleague, content="Bob view"), _activate()
    )
    assert alice is not None and bob is not None
    assert (alice.version, bob.version) == (1, 1)
    assert bob.status is MemoryStatus.ACTIVE
    assert bob.lineage_id != alice.lineage_id


@pytest.mark.postgres
@pytest.mark.docker
async def test_concurrent_writers_of_one_subject_store_exactly_one_row() -> None:
    """Six writers, one subject, one row -- a guard on convergence, not a proof of the lock.

    Read this test's docstring before trusting it with more than it does.

    What it establishes: submitting the same subject from six writers at once leaves one
    stored version 1 and one memory id, which is the behaviour the subject rule promises.

    What it does *not* establish, and a later reader would otherwise assume it did: that
    either ``pg_advisory_xact_lock`` is what produces that. Measured on 2026-10-01 by
    deleting each lock from the write path in turn -- the subject lock, the tenant
    governance lock, and both together -- this test stays green in all four
    configurations. The writers do not interleave between their predecessor read and
    their commit in this environment, so the race is never actually presented to the
    database and no lock is exercised. A comment claiming the lock is load-bearing here
    would be a claim about a window this test never opens.

    The two facts that do hold, and that a future concurrency test would need to work
    around, were read off the live schema rather than inferred:
    ``memory_records_tenant_id_scope_type_scope_id_memory_type_subject_key_version_key``
    is a plain unique index, so its NULLs are distinct and it does *not* cover
    tenant-scoped rows at all; and the version-1 collision the race would produce is
    therefore invisible to the constraint. Detecting it needs run-controlled
    interleaving, not more writers.
    """
    marker = uuid4().hex[:12]
    repository = PostgresMemoryRepository(LIVE_TENANT)
    runs = await _runs(marker, 6)
    assert len(set(runs)) == 6

    results = await asyncio.gather(
        *(repository.persist(_candidate(marker, source_run_id=run), _activate()) for run in runs)
    )

    stored = [record for record in results if record is not None]
    assert len(stored) == 6
    assert len({record.memory_id for record in stored}) == 1
    assert await _stored_versions(f"postgres-chain-{marker}") == [(1, "active")]
