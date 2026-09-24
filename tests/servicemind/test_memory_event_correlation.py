"""Which run caused a memory transition, as the ledger records it.

A memory record names the run that authored it (``memory_records.source_run_id``), so its
*creation* was always traceable. A later transition on it was not: ``memory_events``
recorded the actor and nothing about the run, and a revocation -- the one event an
operator has to explain -- read as "somebody or something revoked this".

Three things have to hold, and the third is the one that makes the first two mean
anything:

1. A transition a run caused carries that run.
2. A transition no run caused carries ``None`` rather than a plausible-looking guess:
   TTL expiry, procedure support revalidation and human review are not run-scoped, and
   naming a run for them would be a fabrication wearing the clothes of a correlation.
3. Both authorities record the same event. The in-memory one recorded nothing at all --
   ``transition`` and ``revoke_by_evidence`` opened with ``del actor_id`` -- so every
   offline test of memory governance was a test of a repository with no ledger.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from servicemind.memory.contracts import (
    MemoryCandidate,
    MemoryEvent,
    MemoryQuery,
    MemoryStatus,
    MemoryType,
    MemoryWriteAction,
    MemoryWriteDecision,
    SemanticSubtype,
    memory_event,
)
from servicemind.memory.repository import InMemoryMemoryRepository
from servicemind.persistence.models import MemoryEventRecord

pytestmark = pytest.mark.asyncio

TENANT = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
RUN = UUID("11111111-1111-4111-8111-111111111111")
OTHER_RUN = UUID("22222222-2222-4222-8222-222222222222")
TRACE = "thread-phase5-acceptance"
EVIDENCE = "ev-0123456789abcdef"


def _evidence_ref(evidence_id: str = EVIDENCE) -> dict[str, object]:
    return {
        "evidence_id": evidence_id,
        "source_ref": "knowledge://vpn-mfa-device-rebind",
        "content_hash": "0" * 64,
        "verified": True,
    }


def _candidate(**overrides: object) -> MemoryCandidate:
    payload: dict[str, object] = {
        "tenant_id": TENANT,
        "memory_type": MemoryType.SEMANTIC,
        "semantic_subtype": SemanticSubtype.LEARNED_FACT,
        "subject_key": "vpn-mfa-rebind",
        "content": "Rebind the authenticator device after verifying identity.",
        "source_run_id": RUN,
        "source_trace_id": TRACE,
        # A learned fact without evidence does not validate, and should not: the
        # candidate contract is what stops an unevidenced claim from becoming memory.
        "evidence_refs": (_evidence_ref(),),
        "confidence": 0.8,
        "importance": 0.7,
        "created_by": "post-run-memory-writer",
        "valid_from": datetime.now(UTC) - timedelta(minutes=1),
    }
    payload.update(overrides)
    return MemoryCandidate.model_validate(payload)


def _query(**overrides: object) -> MemoryQuery:
    payload: dict[str, object] = {"tenant_id": TENANT, "text": "vpn mfa", "user_id": "alice"}
    payload.update(overrides)
    return MemoryQuery.model_validate(payload)


def _decision(action: MemoryWriteAction, reason: str) -> MemoryWriteDecision:
    return MemoryWriteDecision(action=action, reason_codes=(reason,))


# ------------------------------------------------------------------ correlation present


async def test_a_created_memory_records_the_run_that_authored_it() -> None:
    repository = InMemoryMemoryRepository()
    record = await repository.persist(
        _candidate(), _decision(MemoryWriteAction.ACTIVATE, "SEMANTIC_LEARNED_FACT")
    )
    assert record is not None
    events = [event for event in repository.events if event.memory_id == record.memory_id]
    assert len(events) == 1
    assert events[0].run_id == RUN
    assert events[0].trace_id == TRACE


async def test_a_revocation_records_the_run_whose_evidence_caused_it() -> None:
    """The case the ledger could not answer: a revocation with no run is unexplainable."""
    repository = InMemoryMemoryRepository()
    evidence_id = EVIDENCE
    record = await repository.persist(
        _candidate(),
        _decision(MemoryWriteAction.ACTIVATE, "SEMANTIC_LEARNED_FACT"),
    )
    assert record is not None
    revoked = await repository.revoke_by_evidence(
        evidence_id,
        tenant_id=TENANT,
        actor_id="phase5-governance",
        reason="POISONED_EVIDENCE",
        run_id=OTHER_RUN,
        trace_id="thread-poisoned",
    )
    assert revoked == 1
    event = repository.events[-1]
    assert (event.event_type, event.run_id, event.trace_id) == (
        "memory.revoked",
        OTHER_RUN,
        "thread-poisoned",
    )
    assert event.payload["evidence_id"] == evidence_id
    assert event.actor_id == "phase5-governance"


async def test_a_run_caused_transition_records_that_run() -> None:
    """The other way a transition gets a run: a caller that drove it passes one in.

    ``transition`` is reached without a run from the human-review endpoint -- a person
    deciding is not a run deciding -- so this is the parameter's meaning rather than a
    path the deployment exercises today. It is pinned here because "the keyword is
    accepted" and "the keyword is recorded" are different claims, and only the second
    makes the keyword worth accepting.
    """
    repository = InMemoryMemoryRepository()
    record = await repository.persist(
        _candidate(), _decision(MemoryWriteAction.QUARANTINE, "NEEDS_REVIEW")
    )
    assert record is not None
    await repository.transition(
        record.memory_id,
        MemoryStatus.REVOKED,
        actor_id="poison-control",
        reason="EVIDENCE_WITHDRAWN",
        run_id=OTHER_RUN,
        trace_id="thread-that-noticed",
    )
    event = repository.events[-1]
    assert (event.event_type, event.run_id, event.trace_id) == (
        "memory.revoked",
        OTHER_RUN,
        "thread-that-noticed",
    )


async def test_a_human_review_records_the_actor_it_used_to_discard() -> None:
    repository = InMemoryMemoryRepository()
    record = await repository.persist(
        _candidate(), _decision(MemoryWriteAction.QUARANTINE, "NEEDS_REVIEW")
    )
    assert record is not None
    await repository.transition(
        record.memory_id,
        MemoryStatus.ACTIVE,
        actor_id="approver-7",
        reason="HUMAN_REVIEW_ACTIVATE",
        human_review_ref="review://phase5/9",
        expected_status=MemoryStatus.QUARANTINE,
    )
    event = repository.events[-1]
    assert event.event_type == "memory.active"
    assert event.actor_id == "approver-7"
    # A human decision is not run-scoped, and saying otherwise would be an invention.
    assert event.run_id is None
    assert event.trace_id is None


# ---------------------------------------------------------------- correlation absent


async def test_a_lapsed_ttl_records_an_expiry_with_no_run() -> None:
    """The clock ended this memory's life; the read that noticed did not."""
    repository = InMemoryMemoryRepository()
    record = await repository.persist(
        _candidate(expires_at=datetime.now(UTC) - timedelta(seconds=1)),
        _decision(MemoryWriteAction.ACTIVATE, "SEMANTIC_LEARNED_FACT"),
    )
    assert record is not None
    assert record.status is MemoryStatus.ACTIVE
    await repository.candidates(_query())
    expiries = [event for event in repository.events if event.event_type == "memory.expired"]
    assert len(expiries) == 1
    assert expiries[0].run_id is None
    assert expiries[0].actor_id == "ttl-maintenance"


async def test_a_procedure_that_lost_its_support_records_a_revocation_with_no_run() -> None:
    repository = InMemoryMemoryRepository()
    # The two episodes this procedure claims are not in the repository, so its support
    # does not hold -- the condition the live revalidator acts on, reached without
    # staging a full cross-ticket derivation.
    record = await repository.persist(
        _candidate(
            memory_type=MemoryType.PROCEDURAL,
            semantic_subtype=None,
            subject_key="procedure:cross-ticket:vpn",
            supporting_episode_ids=(uuid4(), uuid4()),
        ),
        _decision(MemoryWriteAction.ACTIVATE, "CROSS_TICKET_PROCEDURE"),
    )
    assert record is not None
    await repository.candidates(_query())
    revocations = [
        event
        for event in repository.events
        if event.event_type == "memory.revoked"
        and event.actor_id == "procedural-support-revalidator"
    ]
    assert len(revocations) == 1
    assert revocations[0].run_id is None


# ------------------------------------------------------------------- the two authorities


async def test_the_ledger_and_the_double_record_the_same_fields() -> None:
    """The lock on the property above: the two writers cannot drift apart.

    Both build from :func:`memory_event`, and this asserts that the keywords it produces
    are exactly the columns of the table the ledger writes and exactly the fields of the
    domain event the tests read. A field added to one side only is invisible -- both
    sides keep working, and the difference shows up as a NULL nobody expected.
    """
    columns = set(MemoryEventRecord.__table__.columns.keys()) - {"id", "created_at"}
    fields = set(MemoryEvent.model_fields) - {"created_at"}
    keywords = set(memory_event(tenant_id=TENANT, memory_id=uuid4(), actor_id="a", event_type="e"))
    assert keywords == columns == fields


async def test_an_event_rejects_a_field_the_ledger_has_no_column_for() -> None:
    with pytest.raises(ValueError):
        MemoryEvent.model_validate(
            {
                "tenant_id": TENANT,
                "memory_id": uuid4(),
                "actor_id": "a",
                "event_type": "memory.active",
                "invented": True,
            }
        )


# ----------------------------------------------------------- the ledger on the wire


LIVE_TENANT = UUID("11111111-1111-4111-8111-111111111111")


@pytest.mark.docker
async def test_the_stored_rows_carry_the_run_and_nothing_where_there_was_none() -> None:
    """Asserted against PostgreSQL, because the claim is about what was written.

    The double agreeing with the domain model proves the two authorities build the same
    event; it cannot prove the column, the constraint or the writer's SQL are right. The
    second half is the one worth running: an implementation that defaults ``run_id`` to
    the ambient run would pass the first assertion and quietly fabricate a cause for a
    TTL expiry -- so the expiry is checked for NULL, not merely for "some value".

    The rows are not cleaned up, and cannot be: ``memory_events`` rejects UPDATE and
    DELETE at the database, so the ledger a test writes is the ledger the tenant keeps.
    Subject keys carry a per-run marker, which is how the rows stay identifiable. This
    matches ``scripts/verify_phase5_governance.py``, the established convention.
    """
    from sqlalchemy import select

    from servicemind.memory.contracts import MemoryScope
    from servicemind.memory.repository import PostgresMemoryRepository
    from servicemind.persistence.database import tenant_session
    from servicemind.persistence.models import MemoryEventRecord
    from servicemind.persistence.repository import ServiceMindRepository

    marker = uuid4().hex[:12]
    trace = f"thread-memory-event-{marker}"
    repository = PostgresMemoryRepository(LIVE_TENANT)
    runs = ServiceMindRepository(LIVE_TENANT)
    # Real ``agent_runs`` rows: ``memory_events.run_id`` is a foreign key, so a made-up
    # uuid would prove nothing about what the deployment can actually record. Two of them,
    # because a writer that reached for "the" run instead of the one it was given would
    # pass every single-run assertion here.
    run = await runs.create_run(
        user_id="correlation-probe",
        ticket_id=0,
        goal=f"prove the ledger records which run caused a transition ({marker})",
        request_write=False,
    )
    later_run = await runs.create_run(
        user_id="correlation-probe",
        ticket_id=0,
        goal=f"the run that withdrew this memory's evidence ({marker})",
        request_write=False,
    )

    created = await repository.persist(
        _candidate(
            tenant_id=LIVE_TENANT,
            scope=MemoryScope(),
            subject_key=f"correlation-created-{marker}",
            source_run_id=run.id,
            source_trace_id=trace,
        ),
        _decision(MemoryWriteAction.ACTIVATE, "SEMANTIC_LEARNED_FACT"),
    )
    assert created is not None

    # The clock ends this one's life: valid_to is already behind us, so the next read
    # sweeps it to EXPIRED without any run being involved.
    lapsed = await repository.persist(
        _candidate(
            tenant_id=LIVE_TENANT,
            scope=MemoryScope(),
            subject_key=f"correlation-lapsed-{marker}",
            source_run_id=run.id,
            source_trace_id=trace,
            valid_from=datetime.now(UTC) - timedelta(minutes=2),
            valid_to=datetime.now(UTC) - timedelta(minutes=1),
        ),
        _decision(MemoryWriteAction.ACTIVATE, "SEMANTIC_LEARNED_FACT"),
    )
    assert lapsed is not None
    await repository.candidates(_query(tenant_id=LIVE_TENANT))

    # A transition driven by a *second* run, so the row cannot be right by coincidence.
    revoked_trace = f"{trace}-later"
    await repository.transition(
        created.memory_id,
        MemoryStatus.REVOKED,
        actor_id="phase5-governance",
        reason="POISONED_EVIDENCE",
        run_id=later_run.id,
        trace_id=revoked_trace,
    )

    async with tenant_session(LIVE_TENANT) as session:

        async def events_for(memory_id: UUID) -> list[tuple[str, UUID | None, str | None]]:
            rows = (
                await session.execute(
                    select(MemoryEventRecord)
                    .where(MemoryEventRecord.memory_id == memory_id)
                    .order_by(MemoryEventRecord.created_at, MemoryEventRecord.event_type)
                )
            ).scalars()
            return [(row.event_type, row.run_id, row.trace_id) for row in rows]

        assert await events_for(created.memory_id) == [
            ("memory.active", run.id, trace),
            ("memory.revoked", later_run.id, revoked_trace),
        ]
        assert await events_for(lapsed.memory_id) == [
            ("memory.active", run.id, trace),
            ("memory.expired", None, None),
        ]

        # The index the question is asked through: one run's effect on a tenant's memory.
        # The revocation belongs to the later run and not to the authoring one, and the
        # expiry belongs to neither.
        async def correlated_event_types(run_id: UUID) -> list[str]:
            return sorted(
                (
                    await session.execute(
                        select(MemoryEventRecord.event_type).where(
                            MemoryEventRecord.run_id == run_id
                        )
                    )
                ).scalars()
            )

        assert await correlated_event_types(run.id) == ["memory.active", "memory.active"]
        assert await correlated_event_types(later_run.id) == ["memory.revoked"]
