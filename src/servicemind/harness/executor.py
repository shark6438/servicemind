from datetime import UTC, datetime

from servicemind.domain.models import ActionIntent, ExecutionResult
from servicemind.integrations.glpi.client import GlpiClient
from servicemind.integrations.glpi.models import html_to_text
from servicemind.integrations.glpi.resolver import resolve_glpi_config
from servicemind.persistence.models import ActionStatus
from servicemind.persistence.repository import ServiceMindRepository
from servicemind.security.auth import TenantContext


class ControlledActionExecutor:
    """The only Phase 2 component allowed to perform GLPI writes."""

    async def execute(
        self,
        context: TenantContext,
        intent: ActionIntent,
    ) -> ExecutionResult:
        if intent.action_type != "append_ticket_followup":
            raise RuntimeError("Action type is not allowed by the Phase 2 policy")
        if not intent.id:
            raise RuntimeError("Persisted action intent ID is required")
        if intent.tenant_id is not None and intent.tenant_id != context.tenant_id:
            raise PermissionError("ActionIntent tenant does not match execution context")
        if intent.expires_at is not None and datetime.now(UTC) >= intent.expires_at:
            raise TimeoutError("ActionIntent has expired")
        intent.verify_integrity()

        repository = ServiceMindRepository(context.tenant_id)
        persisted = await repository.get_action_intent(intent.run_id)
        if persisted is None:
            raise PermissionError("ActionIntent is not persisted")
        if (
            persisted.id != intent.id
            or persisted.action_hash != intent.action_hash
            or persisted.action_type != intent.action_type
            or persisted.target_id != intent.target_id
            or persisted.arguments != intent.arguments
            or persisted.intent_version != intent.intent_version
            or persisted.policy_version != intent.policy_version
            or persisted.review_digest != intent.review_digest
            or persisted.evidence_digest != intent.evidence_digest
            or persisted.evidence_refs != intent.evidence_refs
            or persisted.idempotency_context != intent.idempotency_context
            or persisted.requested_by != intent.requested_by
            or persisted.expires_at != intent.expires_at
        ):
            raise PermissionError("Runtime ActionIntent differs from the persisted approved intent")
        if persisted.status != ActionStatus.APPROVED.value:
            raise PermissionError("ActionIntent is not approved")
        idempotency_key = f"glpi:{intent.action_type}:{intent.run_id}"
        await repository.claim_idempotency(
            idempotency_key=idempotency_key,
            action_hash=intent.action_hash,
        )
        async with repository.action_lock(idempotency_key):
            record = await repository.get_idempotency(idempotency_key)
            if record.completed and record.result:
                return ExecutionResult.model_validate(
                    {**record.result, "duplicate_suppressed": True}
                )

            await repository.update_action_status(intent.id, ActionStatus.EXECUTING)
            config = await resolve_glpi_config(context)
            marker = f"[ServiceMind run={intent.run_id} action={intent.action_hash[:16]}]"
            content = f"{intent.arguments['content']}\n{marker}"

            async with GlpiClient(config) as client:
                # Always reconcile against GLPI before writing. This recovers a crash
                # after GLPI committed but before our idempotency record completed.
                existing = await client.list_ticket_followups(intent.target_id)
                duplicate = next(
                    (followup for followup in existing if marker in html_to_text(followup.content)),
                    None,
                )
                if duplicate:
                    result = ExecutionResult(
                        tool_name="glpi_append_ticket_followup",
                        followup_id=duplicate.id,
                        ticket_id=intent.target_id,
                        verified=True,
                        duplicate_suppressed=True,
                    )
                    await repository.complete_idempotency(record.id, result.model_dump())
                    await repository.update_action_status(intent.id, ActionStatus.SUCCEEDED)
                    return result

                followup = await client.append_ticket_followup(
                    intent.target_id,
                    content,
                    is_private=bool(intent.arguments.get("is_private", True)),
                )

            verified = marker in html_to_text(followup.content)
            if not verified:
                await repository.update_action_status(intent.id, ActionStatus.FAILED)
                raise RuntimeError("GLPI read-after-write verification failed")

            result = ExecutionResult(
                tool_name="glpi_append_ticket_followup",
                followup_id=followup.id,
                ticket_id=intent.target_id,
                verified=True,
            )
            await repository.complete_idempotency(record.id, result.model_dump())
            await repository.update_action_status(intent.id, ActionStatus.SUCCEEDED)
            await repository.audit(
                actor_id=context.user_id,
                event_type="glpi.followup.created",
                resource_type="Ticket",
                resource_id=str(intent.target_id),
                run_id=intent.run_id,
                payload={
                    "action_hash": intent.action_hash,
                    "followup_id": followup.id,
                    "verified": True,
                },
            )
            return result


controlled_executor = ControlledActionExecutor()
