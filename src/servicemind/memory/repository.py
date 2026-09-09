from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, text

from servicemind.memory.contracts import (
    MemoryCandidate,
    MemoryEvidenceRef,
    MemoryQuery,
    MemoryRecord,
    MemoryScope,
    MemoryScopeType,
    MemoryStatus,
    MemoryType,
    MemoryWriteAction,
    MemoryWriteDecision,
)
from servicemind.memory.policy import MemoryGovernancePolicy
from servicemind.persistence.database import tenant_session
from servicemind.persistence.models import MemoryEventRecord, MemoryRecordRow


class MemoryRepository(Protocol):
    async def persist(
        self, candidate: MemoryCandidate, decision: MemoryWriteDecision
    ) -> MemoryRecord | None: ...

    async def candidates(self, query: MemoryQuery, *, ceiling: int = 500) -> list[MemoryRecord]: ...

    async def revalidate(self, query: MemoryQuery, ids: list[UUID]) -> set[UUID]: ...

    async def transition(
        self,
        memory_id: UUID,
        status: MemoryStatus,
        *,
        actor_id: str,
        reason: str,
        human_review_ref: str | None = None,
    ) -> MemoryRecord: ...

    async def revoke_by_evidence(
        self, evidence_id: str, *, tenant_id: UUID, actor_id: str, reason: str
    ) -> int: ...


LEGAL_TRANSITIONS: dict[MemoryStatus, frozenset[MemoryStatus]] = {
    MemoryStatus.CANDIDATE: frozenset(
        {MemoryStatus.QUARANTINE, MemoryStatus.ACTIVE, MemoryStatus.REVOKED}
    ),
    MemoryStatus.QUARANTINE: frozenset(
        {MemoryStatus.ACTIVE, MemoryStatus.REVOKED, MemoryStatus.EXPIRED}
    ),
    MemoryStatus.ACTIVE: frozenset(
        {MemoryStatus.SUPERSEDED, MemoryStatus.REVOKED, MemoryStatus.EXPIRED}
    ),
    MemoryStatus.SUPERSEDED: frozenset(),
    MemoryStatus.REVOKED: frozenset(),
    MemoryStatus.EXPIRED: frozenset(),
}


def _validate_transition(
    current: MemoryStatus,
    target: MemoryStatus,
    *,
    procedural: bool,
    human_review_ref: str | None,
) -> None:
    if target not in LEGAL_TRANSITIONS[current]:
        raise ValueError(f"illegal memory transition: {current.value} -> {target.value}")
    if procedural and target is MemoryStatus.ACTIVE and not human_review_ref:
        raise PermissionError("procedural activation requires human review")


def _status_for(decision: MemoryWriteDecision) -> MemoryStatus | None:
    if decision.action is MemoryWriteAction.REJECT:
        return None
    if decision.action is MemoryWriteAction.ACTIVATE:
        return MemoryStatus.ACTIVE
    return MemoryStatus.QUARANTINE


#: Predecessor statuses that block a subject from being re-written with the
#: exact same content. Revocation and supersession are authoritative negative
#: signals (human review / evidence invalidation); silently returning the dead
#: record would both swallow the new learning and report it as stored.
_TERMINATED_BY_AUTHORITY = frozenset({MemoryStatus.REVOKED, MemoryStatus.SUPERSEDED})


def _subject_dedupe_outcome(
    predecessor: MemoryStatus | None, same_content: bool
) -> tuple[bool, MemoryStatus | None, tuple[str, ...]]:
    """Decide how a write to a known ``subject_key`` relates to its latest version.

    Returns ``(return_existing, override_status, extra_reason_codes)``.

    - No predecessor: fresh write at the policy's decision.
    - Live (``ACTIVE``) or pending-review (``QUARANTINE``) predecessor with
      identical content: idempotent duplicate -> return the existing record.
    - Live/pending predecessor with changed content: version conflict -> force
      quarantine for human review (never a second live version on the same
      subject without review).
    - Identical content after a REVOKED/SUPERSEDED predecessor must NOT
      resurrect the fact silently; it re-enters quarantine for human review.
    - Any predecessor that already lapsed (EXPIRED) or a changed content after
      an authority-terminated predecessor is fresh learning at the policy's
      decision: nothing live conflicts and the subject must not be deadlocked
      by its history.
    """
    if predecessor is None:
        return False, None, ()
    if predecessor in {MemoryStatus.ACTIVE, MemoryStatus.QUARANTINE}:
        if same_content:
            return True, None, ()
        return False, MemoryStatus.QUARANTINE, ("VERSION_CONFLICT",)
    if same_content and predecessor in _TERMINATED_BY_AUTHORITY:
        return False, MemoryStatus.QUARANTINE, (f"RESURRECTION_AFTER_{predecessor.value.upper()}",)
    return False, None, ()



def _validate_activation(
    record: MemoryRecord, review: str | None, episodes: list[MemoryRecord]
) -> None:
    if not review or not review.strip():
        raise PermissionError("activation from quarantine requires human review")
    proposal = MemoryCandidate.model_validate(
        {
            key: value
            for key, value in record.model_dump().items()
            if key in MemoryCandidate.model_fields
        }
        | {"final_state_verified": record.memory_type is MemoryType.EPISODIC}
    )
    assessment = MemoryGovernancePolicy().assess(proposal)
    hard_reasons = set(assessment.reason_codes) - {
        "PROCEDURAL_REQUIRES_HUMAN_REVIEW",
        "CONFIDENCE_BELOW_AUTO_ACTIVATION",
        "CONFLICT_DETECTED",
        "DETERMINISTIC_POLICY_PASSED",
    }
    if hard_reasons or assessment.action is MemoryWriteAction.REJECT:
        raise PermissionError("memory activation failed safety/evidence revalidation")
    now = datetime.now(UTC)
    if (record.valid_to is not None and record.valid_to <= now) or (
        record.expires_at is not None and record.expires_at <= now
    ):
        raise PermissionError("expired memory cannot be activated")
    if record.memory_type is MemoryType.PROCEDURAL:
        valid = [
            episode
            for episode in episodes
            if episode.memory_id in record.supporting_episode_ids
            and episode.tenant_id == record.tenant_id
            and episode.memory_type is MemoryType.EPISODIC
            and episode.visible_at(now)
            and not episode.taint_labels
            and all(ref.verified for ref in episode.evidence_refs)
            and (
                episode.scope == record.scope or episode.scope.scope_type is MemoryScopeType.TENANT
            )
        ]
        if (
            len(valid) != len(set(record.supporting_episode_ids))
            or len({episode.source_run_id for episode in valid} - {None}) < 2
        ):
            raise PermissionError(
                "procedural memory requires distinct verified accessible episodes"
            )


def _revocation_closure(records: list[MemoryRecord], evidence_id: str) -> set[UUID]:
    affected = {
        record.memory_id
        for record in records
        if any(ref.evidence_id == evidence_id for ref in record.evidence_refs)
    }
    while True:
        enlarged = affected | {
            record.memory_id
            for record in records
            if affected.intersection(record.supporting_episode_ids)
        }
        if enlarged == affected:
            return affected
        affected = enlarged


def _semantic_overlap(left: str, right: str) -> float:
    pattern = r"[a-z0-9_-]+|[\u4e00-\u9fff]"
    first = set(re.findall(pattern, left.casefold()))
    second = set(re.findall(pattern, right.casefold()))
    return len(first & second) / len(first | second) if first and second else 0


def _record_from_candidate(
    candidate: MemoryCandidate,
    decision: MemoryWriteDecision,
    *,
    memory_id: UUID | None = None,
    lineage_id: UUID | None = None,
    version: int = 1,
) -> MemoryRecord | None:
    status = _status_for(decision)
    if status is None:
        return None
    now = datetime.now(UTC)
    return MemoryRecord(
        memory_id=memory_id or uuid4(),
        lineage_id=lineage_id or uuid4(),
        tenant_id=candidate.tenant_id,
        scope=candidate.scope,
        memory_type=candidate.memory_type,
        semantic_subtype=candidate.semantic_subtype,
        subject_key=candidate.subject_key,
        content=candidate.content,
        content_hash=candidate.content_hash,
        source_run_id=candidate.source_run_id,
        source_trace_id=candidate.source_trace_id,
        evidence_refs=candidate.evidence_refs,
        supporting_episode_ids=candidate.supporting_episode_ids,
        confidence=candidate.confidence,
        importance=candidate.importance,
        version=version,
        valid_from=candidate.valid_from,
        valid_to=candidate.valid_to,
        expires_at=candidate.expires_at,
        status=status,
        provenance=candidate.provenance,
        taint_labels=candidate.taint_labels,
        consent_ref=candidate.consent_ref,
        created_by=candidate.created_by,
        idempotency_key=candidate.idempotency_key,
        activation_reason=(
            ",".join(decision.reason_codes) if status is MemoryStatus.ACTIVE else None
        ),
        created_at=now,
        updated_at=now,
    )


class InMemoryMemoryRepository:
    """Concurrency-safe authority substitute used by deterministic evaluations."""

    def __init__(self) -> None:
        self._records: dict[UUID, MemoryRecord] = {}
        self._idempotency: dict[tuple[UUID, str], UUID] = {}
        self._lock = asyncio.Lock()

    async def persist(
        self, candidate: MemoryCandidate, decision: MemoryWriteDecision
    ) -> MemoryRecord | None:
        if decision.action is MemoryWriteAction.REJECT:
            return None
        async with self._lock:
            # See PostgresMemoryRepository.persist: expire lapsed rows first so
            # same-content reaffirmations see an EXPIRED (not zombie ACTIVE)
            # predecessor and are written as a fresh version, not deduped away.
            self._expire_lapsed_locked()
            key = (candidate.tenant_id, candidate.idempotency_key)
            if key in self._idempotency:
                return self._records[self._idempotency[key]]
            prior = sorted(
                (
                    record
                    for record in self._records.values()
                    if record.tenant_id == candidate.tenant_id
                    and record.scope == candidate.scope
                    and record.memory_type is candidate.memory_type
                    and record.subject_key == candidate.subject_key
                ),
                key=lambda record: record.version,
            )
            previous = prior[-1] if prior else None
            return_existing, override_status, extra_codes = _subject_dedupe_outcome(
                previous.status if previous else None,
                previous is not None and previous.content_hash == candidate.content_hash,
            )
            if return_existing and previous is not None:
                self._idempotency[key] = previous.memory_id
                return previous
            version = previous.version + 1 if previous else 1
            lineage = previous.lineage_id if previous else uuid4()
            effective_decision = decision
            if override_status is not None:
                reason_codes = [*decision.reason_codes, *extra_codes]
                if (
                    override_status is MemoryStatus.QUARANTINE
                    and previous is not None
                    and previous.content_hash != candidate.content_hash
                    and _semantic_overlap(previous.content, candidate.content) >= 0.85
                ):
                    reason_codes.append("SEMANTIC_DUPLICATE_SUSPECTED")
                effective_decision = MemoryWriteDecision(
                    action=MemoryWriteAction.QUARANTINE,
                    reason_codes=tuple(dict.fromkeys(reason_codes)),
                )
            record = _record_from_candidate(
                candidate,
                effective_decision,
                lineage_id=lineage,
                version=version,
            )
            assert record is not None
            self._records[record.memory_id] = record
            self._idempotency[key] = record.memory_id
            return record

    def _expire_lapsed_locked(self) -> None:
        """Lazy TTL: flip ACTIVE rows whose validity window or TTL has lapsed.

        Runs on the read path so an ACTIVE row never keeps a lapsed status, and
        so the terminal EXPIRED state is reached without a dedicated scheduler.
        Read selection already excludes lapsed rows via ``visible_at``; this
        transition keeps the stored status honest and mirrors the audit trail
        the Postgres repository writes (``memory.expired`` events).
        """
        now = datetime.now(UTC)
        for memory_id, current in tuple(self._records.items()):
            if current.status is not MemoryStatus.ACTIVE:
                continue
            lapsed = (current.expires_at is not None and current.expires_at <= now) or (
                current.valid_to is not None and current.valid_to <= now
            )
            if lapsed:
                self._records[memory_id] = current.model_copy(
                    update={"status": MemoryStatus.EXPIRED, "updated_at": now}
                )

    async def candidates(self, query: MemoryQuery, *, ceiling: int = 500) -> list[MemoryRecord]:
        async with self._lock:
            self._expire_lapsed_locked()
            eligible = [record for record in self._records.values() if query.allows_record(record)]
            eligible.sort(
                key=lambda record: (
                    -_semantic_overlap(query.text, record.content),
                    -record.updated_at.timestamp(),
                    str(record.memory_id),
                )
            )
            return eligible[:ceiling]

    async def revalidate(self, query: MemoryQuery, ids: list[UUID]) -> set[UUID]:
        async with self._lock:
            self._expire_lapsed_locked()
            current_query = query.model_copy(update={"at": max(query.at, datetime.now(UTC))})
            return {
                memory_id
                for memory_id in ids
                if memory_id in self._records
                and current_query.allows_record(self._records[memory_id])
            }

    async def transition(
        self,
        memory_id: UUID,
        status: MemoryStatus,
        *,
        actor_id: str,
        reason: str,
        human_review_ref: str | None = None,
    ) -> MemoryRecord:
        del actor_id
        async with self._lock:
            current = self._records[memory_id]
            _validate_transition(
                current.status,
                status,
                procedural=current.memory_type.value == "procedural",
                human_review_ref=human_review_ref,
            )
            if status is MemoryStatus.ACTIVE:
                _validate_activation(current, human_review_ref, list(self._records.values()))
                for other_id, other in tuple(self._records.items()):
                    if (
                        other_id != memory_id
                        and other.lineage_id == current.lineage_id
                        and other.status is MemoryStatus.ACTIVE
                    ):
                        self._records[other_id] = other.model_copy(
                            update={
                                "status": MemoryStatus.SUPERSEDED,
                                "updated_at": datetime.now(UTC),
                            }
                        )
            updated = current.model_copy(
                update={
                    "status": status,
                    "activation_reason": reason if status is MemoryStatus.ACTIVE else None,
                    "updated_at": datetime.now(UTC),
                    "provenance": {
                        **current.provenance,
                        **({"human_review_ref": human_review_ref} if human_review_ref else {}),
                    },
                }
            )
            self._records[memory_id] = updated
            return updated

    async def revoke_by_evidence(
        self, evidence_id: str, *, tenant_id: UUID, actor_id: str, reason: str
    ) -> int:
        del actor_id
        async with self._lock:
            count = 0
            affected = _revocation_closure(
                [record for record in self._records.values() if record.tenant_id == tenant_id],
                evidence_id,
            )
            for memory_id, current in tuple(self._records.items()):
                if current.tenant_id != tenant_id:
                    continue
                if current.status not in {MemoryStatus.ACTIVE, MemoryStatus.QUARANTINE}:
                    continue
                if memory_id not in affected:
                    continue
                self._records[memory_id] = current.model_copy(
                    update={
                        "status": MemoryStatus.REVOKED,
                        "updated_at": datetime.now(UTC),
                        "provenance": {**current.provenance, "revocation_reason": reason},
                    }
                )
                count += 1
            return count

    @property
    def records(self) -> tuple[MemoryRecord, ...]:
        return tuple(self._records.values())


def _to_domain(row: MemoryRecordRow) -> MemoryRecord:
    return MemoryRecord(
        memory_id=row.id,
        lineage_id=row.lineage_id,
        tenant_id=row.tenant_id,
        scope=MemoryScope(scope_type=row.scope_type, scope_id=row.scope_id),
        memory_type=row.memory_type,
        semantic_subtype=row.semantic_subtype,
        subject_key=row.subject_key,
        content=row.content,
        content_hash=row.content_hash,
        source_run_id=row.source_run_id,
        source_trace_id=row.source_trace_id,
        evidence_refs=tuple(MemoryEvidenceRef.model_validate(value) for value in row.evidence_refs),
        supporting_episode_ids=tuple(UUID(value) for value in row.supporting_episode_ids),
        confidence=row.confidence,
        importance=row.importance,
        version=row.version,
        valid_from=row.valid_from,
        valid_to=row.valid_to,
        expires_at=row.expires_at,
        status=row.status,
        provenance=row.provenance,
        taint_labels=frozenset(row.taint_labels),
        consent_ref=row.consent_ref,
        created_by=row.created_by,
        idempotency_key=row.idempotency_key,
        activation_reason=row.activation_reason,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class PostgresMemoryRepository:
    """RLS-scoped canonical memory repository with append-only transition audit."""

    def __init__(self, tenant_id: UUID) -> None:
        self.tenant_id = tenant_id

    async def _write_lock(self, session) -> None:
        # Activation and revocation span lineages (procedural dependencies). Use
        # one transaction lock per tenant so they cannot race with new writes.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"memory-governance:{self.tenant_id}"},
        )

    def _read_filters(self, query: MemoryQuery) -> list:
        if query.tenant_id != self.tenant_id:
            raise PermissionError("query tenant does not match repository tenant")
        scope_filters = [MemoryRecordRow.scope_type == "tenant"]
        for scope_type, ids in (
            ("user", [query.user_id]),
            ("entity", [str(i) for i in query.entity_ids]),
            ("group", [str(i) for i in query.group_ids]),
            ("service", list(query.service_ids)),
        ):
            if ids:
                scope_filters.append(
                    and_(
                        MemoryRecordRow.scope_type == scope_type, MemoryRecordRow.scope_id.in_(ids)
                    )
                )
        return [
            MemoryRecordRow.status == MemoryStatus.ACTIVE.value,
            MemoryRecordRow.memory_type.in_([kind.value for kind in query.memory_types]),
            MemoryRecordRow.valid_from <= query.at,
            or_(MemoryRecordRow.valid_to.is_(None), MemoryRecordRow.valid_to > query.at),
            or_(MemoryRecordRow.expires_at.is_(None), MemoryRecordRow.expires_at > query.at),
            MemoryRecordRow.taint_labels == [],
            or_(*scope_filters),
            or_(
                MemoryRecordRow.created_by != "post-run-memory-middleware",
                MemoryRecordRow.scope_type == "user",
            ),
            or_(
                MemoryRecordRow.provenance["required_entity_ids"].is_(None),
                ~MemoryRecordRow.provenance.has_key("required_entity_ids"),
                MemoryRecordRow.provenance["required_entity_ids"].contained_by(
                    sorted(query.entity_ids)
                ),
            ),
            or_(
                MemoryRecordRow.provenance["required_group_ids"].is_(None),
                ~MemoryRecordRow.provenance.has_key("required_group_ids"),
                MemoryRecordRow.provenance["required_group_ids"].contained_by(
                    sorted(query.group_ids)
                ),
            ),
        ]

    async def persist(
        self, candidate: MemoryCandidate, decision: MemoryWriteDecision
    ) -> MemoryRecord | None:
        status = _status_for(decision)
        if status is None:
            return None
        if candidate.tenant_id != self.tenant_id:
            raise PermissionError("candidate tenant does not match repository tenant")
        async with tenant_session(self.tenant_id) as session:
            # Expire first so a same-subject candidate sees the true predecessor
            # status: an ACTIVE row whose TTL/validity has lapsed must read as
            # EXPIRED, or an identical reaffirmation would return the lapsed row
            # (idempotent dedupe) and report a stored memory that is not visible.
            await self._expire_lapsed(session)
            await self._write_lock(session)
            lock_key = "|".join(
                (
                    str(self.tenant_id),
                    candidate.scope.scope_type.value,
                    candidate.scope.scope_id or "",
                    candidate.memory_type.value,
                    candidate.subject_key,
                )
            )
            # Serialise all versions of the same subject. The transaction-scoped
            # advisory lock closes the version race without weakening RLS.
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": lock_key},
            )
            existing = (
                await session.execute(
                    select(MemoryRecordRow).where(
                        MemoryRecordRow.idempotency_key == candidate.idempotency_key
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return _to_domain(existing)
            previous = (
                await session.execute(
                    select(MemoryRecordRow)
                    .where(
                        MemoryRecordRow.scope_type == candidate.scope.scope_type.value,
                        MemoryRecordRow.scope_id == candidate.scope.scope_id,
                        MemoryRecordRow.memory_type == candidate.memory_type.value,
                        MemoryRecordRow.subject_key == candidate.subject_key,
                    )
                    .order_by(MemoryRecordRow.version.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            return_existing, override_status, extra_codes = _subject_dedupe_outcome(
                MemoryStatus(previous.status) if previous else None,
                previous is not None and previous.content_hash == candidate.content_hash,
            )
            if return_existing and previous is not None:
                return _to_domain(previous)
            version = previous.version + 1 if previous else 1
            lineage_id = previous.lineage_id if previous else uuid4()
            reason_codes = list(decision.reason_codes)
            if override_status is not None:
                status = override_status
                reason_codes = [*reason_codes, *extra_codes]
                if (
                    override_status is MemoryStatus.QUARANTINE
                    and previous is not None
                    and previous.content_hash != candidate.content_hash
                    and _semantic_overlap(previous.content, candidate.content) >= 0.85
                ):
                    reason_codes.append("SEMANTIC_DUPLICATE_SUSPECTED")
            row = MemoryRecordRow(
                tenant_id=self.tenant_id,
                lineage_id=lineage_id,
                scope_type=candidate.scope.scope_type.value,
                scope_id=candidate.scope.scope_id,
                memory_type=candidate.memory_type.value,
                semantic_subtype=(
                    candidate.semantic_subtype.value if candidate.semantic_subtype else None
                ),
                subject_key=candidate.subject_key,
                content=candidate.content,
                content_hash=candidate.content_hash,
                source_run_id=candidate.source_run_id,
                source_trace_id=candidate.source_trace_id,
                evidence_refs=[item.model_dump(mode="json") for item in candidate.evidence_refs],
                supporting_episode_ids=[str(item) for item in candidate.supporting_episode_ids],
                confidence=candidate.confidence,
                importance=candidate.importance,
                version=version,
                valid_from=candidate.valid_from,
                valid_to=candidate.valid_to,
                expires_at=candidate.expires_at,
                status=status.value,
                provenance=candidate.provenance,
                taint_labels=sorted(candidate.taint_labels),
                consent_ref=candidate.consent_ref,
                created_by=candidate.created_by,
                idempotency_key=candidate.idempotency_key,
                activation_reason=(
                    ",".join(reason_codes) if status is MemoryStatus.ACTIVE else None
                ),
            )
            session.add(row)
            await session.flush()
            session.add(
                MemoryEventRecord(
                    tenant_id=self.tenant_id,
                    memory_id=row.id,
                    actor_id=candidate.created_by,
                    event_type=f"memory.{status.value}",
                    reason_codes=reason_codes,
                    payload={"version": version, "content_hash": candidate.content_hash},
                )
            )
            await session.flush()
            return _to_domain(row)

    async def _expire_lapsed(self, session) -> int:
        """Lazy TTL transition: ACTIVE -> EXPIRED once valid_to/expires_at lapse.

        Invoked at the top of the read path so a stored row never keeps an ACTIVE
        status past its validity window (no scheduler dependency), and so the
        transition is always audited with an append-only ``memory.expired`` event.
        Read selection already excludes lapsed rows via ``visible_at``; this makes
        the terminal state real in the database.
        """
        now = datetime.now(UTC)
        rows = (
            await session.execute(
                select(MemoryRecordRow)
                .where(
                    MemoryRecordRow.status == MemoryStatus.ACTIVE.value,
                    or_(
                        and_(
                            MemoryRecordRow.expires_at.is_not(None),
                            MemoryRecordRow.expires_at <= now,
                        ),
                        and_(
                            MemoryRecordRow.valid_to.is_not(None),
                            MemoryRecordRow.valid_to <= now,
                        ),
                    ),
                )
                .with_for_update(skip_locked=True)
            )
        ).scalars()
        count = 0
        for row in rows:
            row.status = MemoryStatus.EXPIRED.value
            row.updated_at = now
            session.add(
                MemoryEventRecord(
                    tenant_id=self.tenant_id,
                    memory_id=row.id,
                    actor_id="ttl-maintenance",
                    event_type="memory.expired",
                    reason_codes=["TTL_ELAPSED"],
                    payload={"expired_at": now.isoformat()},
                )
            )
            count += 1
        return count

    async def candidates(self, query: MemoryQuery, *, ceiling: int = 500) -> list[MemoryRecord]:
        if query.tenant_id != self.tenant_id:
            raise PermissionError("query tenant does not match repository tenant")
        async with tenant_session(self.tenant_id) as session:
            await self._expire_lapsed(session)
            rows = (
                await session.execute(
                    select(MemoryRecordRow)
                    .where(*self._read_filters(query))
                    .order_by(
                        func.ts_rank_cd(
                            func.to_tsvector("simple", MemoryRecordRow.content),
                            func.plainto_tsquery("simple", query.text),
                        ).desc(),
                        MemoryRecordRow.updated_at.desc(),
                        MemoryRecordRow.confidence.desc(),
                        MemoryRecordRow.importance.desc(),
                        MemoryRecordRow.id,
                    )
                    .limit(ceiling)
                )
            ).scalars()
            return [record for row in rows if query.allows_record(record := _to_domain(row))]

    async def revalidate(self, query: MemoryQuery, ids: list[UUID]) -> set[UUID]:
        current_query = query.model_copy(update={"at": max(query.at, datetime.now(UTC))})
        async with tenant_session(self.tenant_id) as session:
            await self._expire_lapsed(session)
            rows = (
                await session.execute(
                    select(MemoryRecordRow).where(
                        *self._read_filters(current_query), MemoryRecordRow.id.in_(ids)
                    )
                )
            ).scalars()
            return {row.id for row in rows if current_query.allows_record(_to_domain(row))}

    async def transition(
        self,
        memory_id: UUID,
        status: MemoryStatus,
        *,
        actor_id: str,
        reason: str,
        human_review_ref: str | None = None,
    ) -> MemoryRecord:
        async with tenant_session(self.tenant_id) as session:
            await self._write_lock(session)
            row = (
                await session.execute(
                    select(MemoryRecordRow).where(MemoryRecordRow.id == memory_id).with_for_update()
                )
            ).scalar_one()
            _validate_transition(
                MemoryStatus(row.status),
                status,
                procedural=row.memory_type == "procedural",
                human_review_ref=human_review_ref,
            )
            if status is MemoryStatus.ACTIVE:
                episodes = (
                    await session.execute(
                        select(MemoryRecordRow).where(
                            MemoryRecordRow.id.in_(
                                [UUID(value) for value in row.supporting_episode_ids]
                            )
                        )
                    )
                ).scalars()
                _validate_activation(
                    _to_domain(row), human_review_ref, [_to_domain(episode) for episode in episodes]
                )
                prior_active = list(
                    (
                        await session.execute(
                            select(MemoryRecordRow)
                            .where(
                                MemoryRecordRow.lineage_id == row.lineage_id,
                                MemoryRecordRow.id != memory_id,
                                MemoryRecordRow.status == MemoryStatus.ACTIVE.value,
                            )
                            .with_for_update()
                        )
                    ).scalars()
                )
                for prior in prior_active:
                    prior.status = MemoryStatus.SUPERSEDED.value
                    prior.updated_at = datetime.now(UTC)
                    session.add(
                        MemoryEventRecord(
                            tenant_id=self.tenant_id,
                            memory_id=prior.id,
                            actor_id=actor_id,
                            event_type="memory.superseded",
                            reason_codes=["REPLACED_BY_REVIEWED_VERSION"],
                            payload={"replacement_memory_id": str(memory_id)},
                        )
                    )
            row.status = status.value
            row.activation_reason = reason if status is MemoryStatus.ACTIVE else None
            row.updated_at = datetime.now(UTC)
            if human_review_ref:
                row.provenance = {**row.provenance, "human_review_ref": human_review_ref}
            session.add(
                MemoryEventRecord(
                    tenant_id=self.tenant_id,
                    memory_id=memory_id,
                    actor_id=actor_id,
                    event_type=f"memory.{status.value}",
                    reason_codes=[reason],
                    payload={"human_review_ref": human_review_ref},
                )
            )
            await session.flush()
            return _to_domain(row)

    async def revoke_by_evidence(
        self, evidence_id: str, *, tenant_id: UUID, actor_id: str, reason: str
    ) -> int:
        if tenant_id != self.tenant_id:
            raise PermissionError("revocation tenant does not match repository tenant")
        async with tenant_session(self.tenant_id) as session:
            await self._write_lock(session)
            rows = list(
                (
                    await session.execute(
                        select(MemoryRecordRow)
                        .where(
                            MemoryRecordRow.status.in_(
                                [MemoryStatus.ACTIVE.value, MemoryStatus.QUARANTINE.value]
                            )
                        )
                        .with_for_update()
                    )
                ).scalars()
            )
            affected_ids = _revocation_closure([_to_domain(row) for row in rows], evidence_id)
            affected = [row for row in rows if row.id in affected_ids]
            for row in affected:
                row.status = MemoryStatus.REVOKED.value
                row.updated_at = datetime.now(UTC)
                row.provenance = {**row.provenance, "revocation_reason": reason}
                session.add(
                    MemoryEventRecord(
                        tenant_id=self.tenant_id,
                        memory_id=row.id,
                        actor_id=actor_id,
                        event_type="memory.revoked",
                        reason_codes=[reason],
                        payload={"evidence_id": evidence_id},
                    )
                )
            await session.flush()
            return len(affected)
