from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert

from servicemind.persistence.database import tenant_session
from servicemind.persistence.models import (
    ActionIntentRecord,
    ActionStatus,
    AgentRun,
    Approval,
    AuditEvent,
    GlpiIntegration,
    IdempotencyRecord,
    RunEvent,
    RunStatus,
    Tenant,
    ToolInvocation,
)


class ServiceMindRepository:
    def __init__(self, tenant_id: UUID) -> None:
        self.tenant_id = tenant_id

    async def create_run(
        self,
        *,
        user_id: str,
        ticket_id: int,
        goal: str,
        request_write: bool,
    ) -> AgentRun:
        run = AgentRun(
            id=uuid4(),
            tenant_id=self.tenant_id,
            user_id=user_id,
            thread_id=str(uuid4()),
            ticket_id=ticket_id,
            goal=goal,
            request_write=request_write,
            status=RunStatus.PENDING.value,
        )
        async with tenant_session(self.tenant_id) as session:
            session.add(run)
            await session.flush()
        return run

    async def claim_webhook_run(
        self,
        *,
        idempotency_key: str,
        action_hash: str,
        run_id: UUID,
        thread_id: str,
        ticket_id: int,
        goal: str,
        event_payload: dict[str, Any],
    ) -> tuple[AgentRun, bool]:
        """Atomically persist a webhook receipt and its deterministic agent run.

        Returning the same run for concurrent/retried deliveries closes the crash window
        between recording idempotency and creating the durable unit of work.
        """
        async with tenant_session(self.tenant_id) as session:
            receipt_created = (
                await session.execute(
                    insert(IdempotencyRecord)
                    .values(
                        tenant_id=self.tenant_id,
                        idempotency_key=idempotency_key,
                        action_hash=action_hash,
                        completed=False,
                    )
                    .on_conflict_do_nothing(
                        index_elements=["tenant_id", "idempotency_key"]
                    )
                    .returning(IdempotencyRecord.id)
                )
            ).scalar_one_or_none()
            receipt = (
                await session.execute(
                    select(IdempotencyRecord)
                    .where(IdempotencyRecord.idempotency_key == idempotency_key)
                    .with_for_update()
                )
            ).scalar_one()
            if receipt.action_hash != action_hash:
                raise RuntimeError("Idempotency key was reused with different action content")

            created_run_id = (
                await session.execute(
                    insert(AgentRun)
                    .values(
                        id=run_id,
                        tenant_id=self.tenant_id,
                        user_id="glpi-webhook",
                        thread_id=thread_id,
                        ticket_id=ticket_id,
                        goal=goal,
                        request_write=False,
                        status=RunStatus.PENDING.value,
                    )
                    .on_conflict_do_nothing(index_elements=["id"])
                    .returning(AgentRun.id)
                )
            ).scalar_one_or_none()
            run = (
                await session.execute(select(AgentRun).where(AgentRun.id == run_id))
            ).scalar_one()
            if created_run_id is not None:
                session.add(
                    RunEvent(
                        tenant_id=self.tenant_id,
                        run_id=run_id,
                        sequence=1,
                        event_type="webhook.accepted",
                        payload=event_payload,
                    )
                )
            receipt.result = {"run_id": str(run.id)}
            receipt.completed = True
            await session.flush()
            return run, receipt_created is None

    async def get_run(self, run_id: UUID, *, for_update: bool = False) -> AgentRun | None:
        statement = select(AgentRun).where(AgentRun.id == run_id)
        if for_update:
            statement = statement.with_for_update()
        async with tenant_session(self.tenant_id) as session:
            return (await session.execute(statement)).scalar_one_or_none()

    async def update_run(
        self,
        run_id: UUID,
        status: RunStatus,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> AgentRun:
        async with tenant_session(self.tenant_id) as session:
            run = (
                await session.execute(
                    select(AgentRun).where(AgentRun.id == run_id).with_for_update()
                )
            ).scalar_one()
            run.status = status.value
            if result is not None:
                run.result = result
            run.error = error
            await session.flush()
            return run

    async def append_event(
        self, run_id: UUID, event_type: str, payload: dict[str, Any]
    ) -> RunEvent:
        async with tenant_session(self.tenant_id) as session:
            await session.execute(
                select(AgentRun.id).where(AgentRun.id == run_id).with_for_update()
            )
            last_sequence = (
                await session.execute(
                    select(func.coalesce(func.max(RunEvent.sequence), 0)).where(
                        RunEvent.run_id == run_id
                    )
                )
            ).scalar_one()
            event = RunEvent(
                tenant_id=self.tenant_id,
                run_id=run_id,
                sequence=int(last_sequence) + 1,
                event_type=event_type,
                payload=payload,
            )
            session.add(event)
            await session.flush()
            return event

    async def list_events(self, run_id: UUID, after: int = 0) -> list[RunEvent]:
        async with tenant_session(self.tenant_id) as session:
            result = await session.execute(
                select(RunEvent)
                .where(RunEvent.run_id == run_id, RunEvent.sequence > after)
                .order_by(RunEvent.sequence)
            )
            return list(result.scalars())

    async def record_tool_invocation(
        self,
        *,
        run_id: UUID,
        tool_name: str,
        input_hash: str,
        status: str,
        result_ref: str | None,
    ) -> ToolInvocation:
        async with tenant_session(self.tenant_id) as session:
            invocation = ToolInvocation(
                tenant_id=self.tenant_id,
                run_id=run_id,
                tool_name=tool_name,
                input_hash=input_hash,
                status=status,
                result_ref=result_ref,
            )
            session.add(invocation)
            await session.flush()
            return invocation

    async def list_recoverable_runs(self) -> list[AgentRun]:
        async with tenant_session(self.tenant_id) as session:
            result = await session.execute(
                select(AgentRun)
                .where(
                    AgentRun.status.in_(
                        [
                            RunStatus.PENDING.value,
                            RunStatus.RUNNING.value,
                            RunStatus.WAITING_APPROVAL.value,
                        ]
                    )
                )
                .order_by(AgentRun.created_at)
            )
            return list(result.scalars())

    async def get_glpi_integration(self) -> GlpiIntegration | None:
        async with tenant_session(self.tenant_id) as session:
            return (
                await session.execute(
                    select(GlpiIntegration).where(GlpiIntegration.enabled.is_(True))
                )
            ).scalar_one_or_none()

    async def save_action_intent(
        self,
        *,
        run_id: UUID,
        action_type: str,
        target_id: int,
        arguments: dict[str, Any],
        risk_level: str,
        action_hash: str,
        intent_version: str = "v1",
        policy_version: str | None = None,
        review_digest: str | None = None,
        evidence_digest: str | None = None,
        evidence_refs: list[str] | None = None,
        idempotency_context: dict[str, str] | None = None,
        requested_by: str | None = None,
        expires_at: Any | None = None,
        dry_run_preview: str | None = None,
    ) -> ActionIntentRecord:
        async with tenant_session(self.tenant_id) as session:
            existing = (
                await session.execute(
                    select(ActionIntentRecord).where(ActionIntentRecord.run_id == run_id)
                )
            ).scalar_one_or_none()
            if existing:
                return existing
            intent = ActionIntentRecord(
                tenant_id=self.tenant_id,
                run_id=run_id,
                action_type=action_type,
                target_id=target_id,
                arguments=arguments,
                risk_level=risk_level,
                requires_approval=True,
                action_hash=action_hash,
                intent_version=intent_version,
                policy_version=policy_version,
                review_digest=review_digest,
                evidence_digest=evidence_digest,
                evidence_refs=evidence_refs or [],
                idempotency_context=idempotency_context or {},
                requested_by=requested_by,
                expires_at=expires_at,
                dry_run_preview=dry_run_preview,
                status=ActionStatus.PROPOSED.value,
            )
            session.add(intent)
            await session.flush()
            return intent

    async def get_action_intent(self, run_id: UUID) -> ActionIntentRecord | None:
        async with tenant_session(self.tenant_id) as session:
            return (
                await session.execute(
                    select(ActionIntentRecord).where(ActionIntentRecord.run_id == run_id)
                )
            ).scalar_one_or_none()

    async def record_approval(
        self,
        *,
        run_id: UUID,
        action_intent_id: UUID,
        decision: str,
        decided_by: str,
        comment: str | None,
    ) -> tuple[Approval, bool]:
        async with tenant_session(self.tenant_id) as session:
            run = (
                await session.execute(
                    select(AgentRun).where(AgentRun.id == run_id).with_for_update()
                )
            ).scalar_one()
            existing = (
                await session.execute(
                    select(Approval).where(Approval.action_intent_id == action_intent_id)
                )
            ).scalar_one_or_none()
            if existing:
                if existing.decision != decision:
                    raise ValueError("Approval decision conflicts with the recorded decision")
                return existing, False
            if run.status != RunStatus.WAITING_APPROVAL.value:
                raise ValueError("Run is no longer waiting for approval")
            approval = Approval(
                tenant_id=self.tenant_id,
                run_id=run_id,
                action_intent_id=action_intent_id,
                decision=decision,
                decided_by=decided_by,
                comment=comment,
            )
            session.add(approval)
            action = (
                await session.execute(
                    select(ActionIntentRecord)
                    .where(ActionIntentRecord.id == action_intent_id)
                    .with_for_update()
                )
            ).scalar_one()
            action.status = (
                ActionStatus.APPROVED.value
                if decision == "approved"
                else ActionStatus.REJECTED.value
            )
            # This compare-and-set closes concurrent approval and crash-before-resume windows.
            run.status = RunStatus.RUNNING.value
            await session.flush()
            return approval, True

    async def get_approval(self, run_id: UUID) -> Approval | None:
        async with tenant_session(self.tenant_id) as session:
            return (
                await session.execute(select(Approval).where(Approval.run_id == run_id))
            ).scalar_one_or_none()

    async def update_action_status(
        self, action_intent_id: UUID, status: ActionStatus
    ) -> ActionIntentRecord:
        async with tenant_session(self.tenant_id) as session:
            action = (
                await session.execute(
                    select(ActionIntentRecord)
                    .where(ActionIntentRecord.id == action_intent_id)
                    .with_for_update()
                )
            ).scalar_one()
            action.status = status.value
            await session.flush()
            return action

    async def claim_idempotency(
        self, *, idempotency_key: str, action_hash: str
    ) -> tuple[IdempotencyRecord, bool]:
        async with tenant_session(self.tenant_id) as session:
            statement = (
                insert(IdempotencyRecord)
                .values(
                    tenant_id=self.tenant_id,
                    idempotency_key=idempotency_key,
                    action_hash=action_hash,
                    completed=False,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "idempotency_key"])
                .returning(IdempotencyRecord.id)
            )
            created_id = (await session.execute(statement)).scalar_one_or_none()
            record = (
                await session.execute(
                    select(IdempotencyRecord).where(
                        IdempotencyRecord.idempotency_key == idempotency_key
                    )
                )
            ).scalar_one()
            if record.action_hash != action_hash:
                raise RuntimeError("Idempotency key was reused with different action content")
            return record, created_id is not None

    async def get_idempotency(self, idempotency_key: str) -> IdempotencyRecord:
        async with tenant_session(self.tenant_id) as session:
            return (
                await session.execute(
                    select(IdempotencyRecord).where(
                        IdempotencyRecord.idempotency_key == idempotency_key
                    )
                )
            ).scalar_one()

    @asynccontextmanager
    async def action_lock(self, idempotency_key: str) -> AsyncIterator[None]:
        """Serialize a side effect across processes/replicas with a PostgreSQL lock."""
        async with tenant_session(self.tenant_id) as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"{self.tenant_id}:{idempotency_key}"},
            )
            yield

    async def complete_idempotency(
        self, record_id: UUID, result: dict[str, Any]
    ) -> IdempotencyRecord:
        async with tenant_session(self.tenant_id) as session:
            record = (
                await session.execute(
                    select(IdempotencyRecord)
                    .where(IdempotencyRecord.id == record_id)
                    .with_for_update()
                )
            ).scalar_one()
            record.result = result
            record.completed = True
            await session.flush()
            return record

    async def audit(
        self,
        *,
        actor_id: str,
        event_type: str,
        resource_type: str,
        resource_id: str,
        run_id: UUID | None,
        payload: dict[str, Any],
    ) -> AuditEvent:
        event = AuditEvent(
            tenant_id=self.tenant_id,
            actor_id=actor_id,
            event_type=event_type,
            resource_type=resource_type,
            resource_id=resource_id,
            run_id=run_id,
            payload=payload,
        )
        async with tenant_session(self.tenant_id) as session:
            session.add(event)
            await session.flush()
        return event


async def list_tenant_ids() -> list[UUID]:
    """List tenant partitions for the internal startup recovery worker."""
    from servicemind.persistence.database import global_session

    async with global_session() as session:
        return list((await session.execute(select(Tenant.id).order_by(Tenant.id))).scalars())
