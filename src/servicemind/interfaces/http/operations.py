"""Read-only operational views for the ServiceMind control console."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from servicemind.persistence.models import AgentRun, AuditEvent, RunEvent, RunStatus
from servicemind.persistence.repository import ServiceMindRepository
from servicemind.security.auth import TenantContextDependency

router = APIRouter(tags=["ServiceMind Operations"])


class RunSummary(BaseModel):
    id: UUID
    ticket_id: int
    goal: str
    request_write: bool
    status: str
    created_at: datetime
    updated_at: datetime


class RunListPage(BaseModel):
    items: list[RunSummary]
    next_after_created_at: datetime | None = None
    next_after_run_id: UUID | None = None


class RunEventView(BaseModel):
    sequence: int
    type: str
    payload: dict[str, Any]
    created_at: datetime


class AuditEventView(BaseModel):
    id: UUID
    run_id: UUID | None
    actor_id: str
    event_type: str
    resource_type: str
    resource_id: str
    payload: dict[str, Any]
    created_at: datetime


class AuditEventPage(BaseModel):
    items: list[AuditEventView]
    next_after_created_at: datetime | None = None
    next_after_event_id: UUID | None = None


def _run_summary(run: AgentRun) -> RunSummary:
    return RunSummary(
        id=run.id,
        ticket_id=run.ticket_id,
        goal=run.goal,
        request_write=run.request_write,
        status=run.status,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _run_event(event: RunEvent) -> RunEventView:
    return RunEventView(
        sequence=event.sequence,
        type=event.event_type,
        payload=event.payload,
        created_at=event.created_at,
    )


def _audit_event(event: AuditEvent) -> AuditEventView:
    return AuditEventView(
        id=event.id,
        run_id=event.run_id,
        actor_id=event.actor_id,
        event_type=event.event_type,
        resource_type=event.resource_type,
        resource_id=event.resource_id,
        payload=event.payload,
        created_at=event.created_at,
    )


@router.get("/runs", response_model=RunListPage)
async def list_runs(
    context: TenantContextDependency,
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    after_created_at: datetime | None = Query(default=None),
    after_run_id: UUID | None = Query(default=None),
) -> RunListPage:
    """List tenant-scoped runs using a stable descending cursor."""
    context.require_role("viewer")
    if status is not None:
        try:
            RunStatus(status)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Unknown run status") from exc
    if (after_created_at is None) != (after_run_id is None):
        raise HTTPException(status_code=422, detail="Run cursor requires both values")
    repository = ServiceMindRepository(context.tenant_id)
    runs = await repository.list_runs(
        status=status,
        limit=limit,
        after_created_at=after_created_at,
        after_run_id=after_run_id,
    )
    cursor = runs[-1] if len(runs) == limit else None
    return RunListPage(
        items=[_run_summary(run) for run in runs],
        next_after_created_at=cursor.created_at if cursor else None,
        next_after_run_id=cursor.id if cursor else None,
    )


@router.get("/runs/{run_id}/timeline", response_model=list[RunEventView])
async def get_run_timeline(
    run_id: UUID,
    context: TenantContextDependency,
    after: int = Query(default=0, ge=0),
) -> list[RunEventView]:
    """Return the durable, tenant-scoped event timeline without opening an SSE stream."""
    context.require_role("viewer")
    repository = ServiceMindRepository(context.tenant_id)
    if await repository.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return [_run_event(event) for event in await repository.list_events(run_id, after)]


@router.get("/audit-events", response_model=AuditEventPage)
async def list_audit_events(
    context: TenantContextDependency,
    run_id: UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    after_created_at: datetime | None = Query(default=None),
    after_event_id: UUID | None = Query(default=None),
) -> AuditEventPage:
    """Expose the append-only governance ledger to authorized operational roles."""
    context.require_any_role("operator", "approver", "tenant_admin")
    if (after_created_at is None) != (after_event_id is None):
        raise HTTPException(status_code=422, detail="Audit cursor requires both values")
    repository = ServiceMindRepository(context.tenant_id)
    events = await repository.list_audit_events(
        run_id=run_id,
        limit=limit,
        after_created_at=after_created_at,
        after_event_id=after_event_id,
    )
    cursor = events[-1] if len(events) == limit else None
    return AuditEventPage(
        items=[_audit_event(event) for event in events],
        next_after_created_at=cursor.created_at if cursor else None,
        next_after_event_id=cursor.id if cursor else None,
    )
