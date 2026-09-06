import hashlib
import json
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from servicemind.domain.models import ApprovalDecision
from servicemind.harness.webhooks import (
    WebhookValidationError,
    parse_glpi_webhook,
    verify_glpi_signature,
    webhook_run_id,
)
from servicemind.integrations.glpi.client import GlpiAPIError, GlpiClient
from servicemind.integrations.glpi.resolver import resolve_glpi_config
from servicemind.orchestration.runtime import (
    has_pending_interrupt,
    resume_review_run,
    resume_run,
    start_run,
)
from servicemind.persistence.models import ActionIntentRecord, AgentRun, RunStatus
from servicemind.persistence.repository import ServiceMindRepository
from servicemind.security.auth import TenantContext, TenantContextDependency
from servicemind.security.crypto import CredentialCipher

phase2_router = APIRouter(prefix="/v1/servicemind", tags=["ServiceMind Phase 2"])


class GlpiHealth(BaseModel):
    status: str
    api_version: str
    authenticated: bool
    entity_id: int
    profile_id: int


@phase2_router.get("/glpi/health")
async def glpi_health(context: TenantContextDependency) -> GlpiHealth:
    context.require_role("viewer")
    try:
        config = await resolve_glpi_config(context)
        async with GlpiClient(config) as client:
            session: dict[str, Any] = await client.get_session()
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="GLPI entity is outside tenant scope") from exc
    except (GlpiAPIError, httpx.HTTPError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="GLPI integration is unavailable") from exc

    return GlpiHealth(
        status="ok",
        api_version=config.api_version,
        authenticated=bool(session),
        entity_id=config.entity_id,
        profile_id=config.profile_id,
    )


class CreateRunRequest(BaseModel):
    ticket_id: int = Field(ge=1)
    goal: str = Field(min_length=3, max_length=2000)
    request_write: bool = False


class ApprovalRequest(BaseModel):
    decision: Literal["approved", "rejected"]
    expected_action_hash: str = Field(min_length=64, max_length=64)
    comment: str | None = Field(default=None, max_length=1000)


class ReviewResolutionRequest(BaseModel):
    decision: Literal["continue", "stop"]
    comment: str | None = Field(default=None, max_length=1000)


class RunView(BaseModel):
    id: UUID
    tenant_id: UUID
    user_id: str
    ticket_id: int
    goal: str
    request_write: bool
    status: str
    result: dict[str, Any] | None
    error: str | None
    action_intent: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class WebhookAccepted(BaseModel):
    accepted: bool
    duplicate: bool
    run_id: UUID


def _run_view(run: AgentRun, action: ActionIntentRecord | None = None) -> RunView:
    action_payload = None
    if action is not None:
        action_payload = {
            "id": str(action.id),
            "action_type": action.action_type,
            "target_id": action.target_id,
            "arguments": action.arguments,
            "risk_level": action.risk_level,
            "requires_approval": action.requires_approval,
            "action_hash": action.action_hash,
            "status": action.status,
            "intent_version": action.intent_version,
            "policy_version": action.policy_version,
            "review_digest": action.review_digest,
            "evidence_digest": action.evidence_digest,
            "evidence_refs": action.evidence_refs,
            "expires_at": action.expires_at.isoformat() if action.expires_at else None,
            "dry_run_preview": action.dry_run_preview,
        }
    return RunView(
        id=run.id,
        tenant_id=run.tenant_id,
        user_id=run.user_id,
        ticket_id=run.ticket_id,
        goal=run.goal,
        request_write=run.request_write,
        status=run.status,
        result=run.result,
        error=run.error,
        action_intent=action_payload,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


async def _process_webhook_run(run: AgentRun, context: TenantContext) -> None:
    repository = ServiceMindRepository(run.tenant_id)
    try:
        await start_run(run, context)
    except Exception as exc:
        await repository.update_run(run.id, RunStatus.FAILED, error=type(exc).__name__)


@phase2_router.post(
    "/webhooks/glpi", status_code=status.HTTP_202_ACCEPTED, response_model=WebhookAccepted
)
async def receive_glpi_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_glpi_signature: str = Header(alias="X-GLPI-signature"),
    x_glpi_timestamp: str = Header(alias="X-GLPI-timestamp"),
) -> WebhookAccepted:
    body = await request.body()
    try:
        webhook = parse_glpi_webhook(body)
    except WebhookValidationError as exc:
        raise HTTPException(status_code=422, detail="Invalid GLPI webhook payload") from exc

    context = TenantContext(
        tenant_id=webhook.tenant_id,
        user_id="glpi-webhook",
        username="glpi-webhook",
        roles={"viewer", "analyst"},
        allowed_glpi_entity_ids={webhook.entity_id},
    )
    repository = ServiceMindRepository(webhook.tenant_id)
    integration = await repository.get_glpi_integration()
    if integration is None or integration.entity_id != webhook.entity_id:
        raise HTTPException(status_code=401, detail="Unknown webhook tenant scope")
    secret = CredentialCipher().decrypt(integration.webhook_secret_encrypted)
    try:
        verify_glpi_signature(
            body=body,
            timestamp=x_glpi_timestamp,
            signature=x_glpi_signature,
            secret=secret,
        )
    except WebhookValidationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    body_hash = hashlib.sha256(body).hexdigest()
    idempotency_key = f"glpi-webhook:{x_glpi_signature}"
    deterministic_run_id = webhook_run_id(webhook.tenant_id, x_glpi_signature)
    run, duplicate = await repository.claim_webhook_run(
        idempotency_key=idempotency_key,
        action_hash=body_hash,
        run_id=deterministic_run_id,
        thread_id=f"glpi-webhook-{deterministic_run_id}",
        ticket_id=webhook.ticket_id,
        goal=f"Analyze GLPI webhook event {webhook.event}",
        event_payload={
            "event": webhook.event,
            "ticket_id": webhook.ticket_id,
            "entity_id": webhook.entity_id,
        },
    )
    if not duplicate:
        background_tasks.add_task(_process_webhook_run, run, context)
    return WebhookAccepted(accepted=True, duplicate=duplicate, run_id=run.id)


@phase2_router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
async def create_run(request: CreateRunRequest, context: TenantContextDependency) -> RunView:
    context.require_role("analyst")
    repository = ServiceMindRepository(context.tenant_id)
    run = await repository.create_run(
        user_id=context.user_id,
        ticket_id=request.ticket_id,
        goal=request.goal,
        request_write=request.request_write,
    )
    await repository.append_event(
        run.id,
        "run.created",
        {"ticket_id": request.ticket_id, "request_write": request.request_write},
    )
    try:
        await start_run(run, context)
    except PermissionError as exc:
        await repository.update_run(run.id, RunStatus.FAILED, error="Tenant scope denied")
        raise HTTPException(status_code=403, detail="Ticket is outside tenant scope") from exc
    except GlpiAPIError as exc:
        await repository.update_run(run.id, RunStatus.FAILED, error="GLPI resource unavailable")
        if exc.status_code in {403, 404}:
            raise HTTPException(status_code=404, detail="Ticket not found") from exc
        raise HTTPException(status_code=502, detail="GLPI API request failed") from exc
    except Exception as exc:
        await repository.update_run(run.id, RunStatus.FAILED, error=type(exc).__name__)
        raise HTTPException(status_code=502, detail="ServiceMind run failed") from exc

    stored = await repository.get_run(run.id)
    assert stored is not None
    action = await repository.get_action_intent(run.id)
    if stored.status in {
        RunStatus.WAITING_APPROVAL.value,
        RunStatus.WAITING_REVIEW.value,
    }:
        assert await has_pending_interrupt(stored, context)
    return _run_view(stored, action)


@phase2_router.get("/runs/{run_id}")
async def get_run(run_id: UUID, context: TenantContextDependency) -> RunView:
    repository = ServiceMindRepository(context.tenant_id)
    run = await repository.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    action = await repository.get_action_intent(run_id)
    return _run_view(run, action)


@phase2_router.post("/runs/{run_id}/approval")
async def approve_run(
    run_id: UUID, request: ApprovalRequest, context: TenantContextDependency
) -> RunView:
    context.require_role("approver")
    repository = ServiceMindRepository(context.tenant_id)
    run = await repository.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    action = await repository.get_action_intent(run_id)
    if action is None:
        raise HTTPException(status_code=409, detail="Run has no pending action")
    if action.action_hash != request.expected_action_hash:
        raise HTTPException(status_code=409, detail="Action changed after it was reviewed")
    if run.status != RunStatus.WAITING_APPROVAL.value:
        return _run_view(run, action)

    try:
        _, approval_created = await repository.record_approval(
            run_id=run_id,
            action_intent_id=action.id,
            decision=request.decision,
            decided_by=context.user_id,
            comment=request.comment,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not approval_created:
        stored = await repository.get_run(run_id)
        assert stored is not None
        return _run_view(stored, await repository.get_action_intent(run_id))
    await repository.audit(
        actor_id=context.user_id,
        event_type=f"approval.{request.decision}",
        resource_type="ActionIntent",
        resource_id=str(action.id),
        run_id=run_id,
        payload={"action_hash": action.action_hash, "comment": request.comment},
    )
    decision = ApprovalDecision(
        decision=request.decision,
        decided_by=context.user_id,
        comment=request.comment,
    )
    try:
        await resume_run(run, context, decision)
    except Exception as exc:
        await repository.update_run(run.id, RunStatus.FAILED, error=type(exc).__name__)
        raise HTTPException(status_code=502, detail="ServiceMind resume failed") from exc

    stored = await repository.get_run(run_id)
    assert stored is not None
    action = await repository.get_action_intent(run_id)
    return _run_view(stored, action)


@phase2_router.post("/runs/{run_id}:cancel")
async def cancel_run(run_id: UUID, context: TenantContextDependency) -> RunView:
    repository = ServiceMindRepository(context.tenant_id)
    run = await repository.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status not in {
        RunStatus.PENDING.value,
        RunStatus.WAITING_APPROVAL.value,
        RunStatus.WAITING_REVIEW.value,
    }:
        raise HTTPException(status_code=409, detail="Run cannot be cancelled")
    run = await repository.update_run(run_id, RunStatus.CANCELLED)
    await repository.audit(
        actor_id=context.user_id,
        event_type="run.cancelled",
        resource_type="AgentRun",
        resource_id=str(run_id),
        run_id=run_id,
        payload={},
    )
    return _run_view(run, await repository.get_action_intent(run_id))


@phase2_router.post("/runs/{run_id}/review-resolution")
async def resolve_review_escalation(
    run_id: UUID,
    request: ReviewResolutionRequest,
    context: TenantContextDependency,
) -> RunView:
    context.require_role("approver")
    repository = ServiceMindRepository(context.tenant_id)
    run = await repository.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != RunStatus.WAITING_REVIEW.value:
        raise HTTPException(status_code=409, detail="Run is not waiting for human review")
    await repository.audit(
        actor_id=context.user_id,
        event_type=f"review_escalation.{request.decision}",
        resource_type="AgentRun",
        resource_id=str(run_id),
        run_id=run_id,
        payload={"comment": request.comment},
    )
    await repository.update_run(run.id, RunStatus.RUNNING)
    try:
        await resume_review_run(
            run,
            context,
            {
                "decision": request.decision,
                "comment": request.comment,
                "decided_by": context.user_id,
            },
        )
    except Exception as exc:
        await repository.update_run(run.id, RunStatus.FAILED, error=type(exc).__name__)
        raise HTTPException(status_code=502, detail="ServiceMind review resume failed") from exc
    stored = await repository.get_run(run_id)
    assert stored is not None
    return _run_view(stored, await repository.get_action_intent(run_id))


@phase2_router.get("/runs/{run_id}/events", response_class=StreamingResponse)
async def stream_run_events(
    run_id: UUID,
    context: TenantContextDependency,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    repository = ServiceMindRepository(context.tenant_id)
    if await repository.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found")
    after = int(last_event_id or 0)

    async def generate() -> AsyncGenerator[str, None]:
        for event in await repository.list_events(run_id, after):
            payload = {
                "sequence": event.sequence,
                "type": event.event_type,
                "payload": event.payload,
                "created_at": event.created_at.isoformat(),
            }
            yield f"id: {event.sequence}\nevent: {event.event_type}\ndata: {json.dumps(payload)}\n\n"
        yield "event: end\ndata: {}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
