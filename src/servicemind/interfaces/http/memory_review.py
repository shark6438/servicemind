"""Human-review HTTP adapter for quarantined governed memories."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from servicemind.memory.contracts import (
    MemoryEvidenceRef,
    MemoryRecord,
    MemoryReviewQuery,
    MemoryStatus,
    MemoryType,
)
from servicemind.memory.repository import PostgresMemoryRepository
from servicemind.security.auth import TenantContext, TenantContextDependency

router = APIRouter(prefix="/memories", tags=["Governed Memory Review"])


class MemoryReviewItem(BaseModel):
    """Immutable snapshot an approver must inspect before deciding."""

    memory_id: UUID
    memory_type: MemoryType
    subject_key: str
    content: str
    content_hash: str
    version: int
    status: MemoryStatus
    confidence: float
    importance: float
    evidence_refs: tuple[MemoryEvidenceRef, ...]
    supporting_episode_ids: tuple[UUID, ...]
    provenance: dict[str, Any]
    created_at: datetime


class MemoryReviewQueuePage(BaseModel):
    items: list[MemoryReviewItem]
    next_after_created_at: datetime | None = None
    next_after_memory_id: UUID | None = None


class MemoryReviewRequest(BaseModel):
    decision: Literal["activate", "reject"]
    expected_version: int = Field(ge=1)
    expected_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_ref: str = Field(min_length=3, max_length=500)
    comment: str = Field(min_length=3, max_length=1000)


def _memory_review_item(record: MemoryRecord) -> MemoryReviewItem:
    return MemoryReviewItem(
        memory_id=record.memory_id,
        memory_type=record.memory_type,
        subject_key=record.subject_key,
        content=record.content,
        content_hash=record.content_hash,
        version=record.version,
        status=record.status,
        confidence=record.confidence,
        importance=record.importance,
        evidence_refs=record.evidence_refs,
        supporting_episode_ids=record.supporting_episode_ids,
        provenance=record.provenance,
        created_at=record.created_at,
    )


def _memory_review_query(
    context: TenantContext,
    *,
    memory_type: MemoryType | None = None,
    limit: int = 50,
    after_created_at: datetime | None = None,
    after_memory_id: UUID | None = None,
) -> MemoryReviewQuery:
    return MemoryReviewQuery(
        tenant_id=context.tenant_id,
        reviewer_id=context.user_id,
        entity_ids=frozenset(context.allowed_glpi_entity_ids),
        group_ids=frozenset(context.allowed_glpi_group_ids),
        memory_types=(frozenset({memory_type}) if memory_type else frozenset(MemoryType)),
        after_created_at=after_created_at,
        after_memory_id=after_memory_id,
        limit=limit,
    )


@router.get("/review-queue", response_model=MemoryReviewQueuePage)
async def list_memory_review_queue(
    context: TenantContextDependency,
    memory_type: MemoryType | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    after_created_at: datetime | None = Query(default=None),
    after_memory_id: UUID | None = Query(default=None),
) -> MemoryReviewQueuePage:
    """List pending memories visible to this tenant/entity/group approver."""
    context.require_role("approver")
    if (after_created_at is None) != (after_memory_id is None):
        raise HTTPException(
            status_code=422,
            detail="Review cursor requires both after_created_at and after_memory_id",
        )
    query = _memory_review_query(
        context,
        memory_type=memory_type,
        limit=limit,
        after_created_at=after_created_at,
        after_memory_id=after_memory_id,
    )
    records = await PostgresMemoryRepository(context.tenant_id).list_review_queue(query)
    items = [_memory_review_item(record) for record in records]
    cursor = records[-1] if len(records) == limit else None
    return MemoryReviewQueuePage(
        items=items,
        next_after_created_at=cursor.created_at if cursor else None,
        next_after_memory_id=cursor.memory_id if cursor else None,
    )


@router.post("/{memory_id}/review", response_model=MemoryReviewItem)
async def review_memory(
    memory_id: UUID,
    request: MemoryReviewRequest,
    context: TenantContextDependency,
) -> MemoryReviewItem:
    """Atomically bind a human decision to the reviewed version and content."""
    context.require_role("approver")
    repository = PostgresMemoryRepository(context.tenant_id)
    query = _memory_review_query(context)
    pending = await repository.get_for_review(query, memory_id)
    if pending is None:
        # Do not distinguish an unknown id from a record outside the reviewer's
        # tenant/entity/group scope.
        raise HTTPException(status_code=404, detail="Memory review item not found")
    target = MemoryStatus.ACTIVE if request.decision == "activate" else MemoryStatus.REVOKED
    try:
        updated = await repository.transition(
            memory_id,
            target,
            actor_id=context.user_id,
            reason=f"HUMAN_REVIEW_{request.decision.upper()}",
            human_review_ref=request.review_ref,
            review_comment=request.comment,
            expected_version=request.expected_version,
            expected_content_hash=request.expected_content_hash,
            expected_status=MemoryStatus.QUARANTINE,
        )
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _memory_review_item(updated)
