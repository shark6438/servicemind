from datetime import UTC, datetime, timedelta
from uuid import uuid4

from servicemind.domain.evidence import self_authored_marker
from servicemind.domain.models import ActionIntent, ExecutionResult
from servicemind.persistence.models import ActionStatus
from servicemind.persistence.repository import ServiceMindRepository
from servicemind.security.auth import TenantContext
from servicemind.tool_platform.catalog import (
    APPEND_FOLLOWUP_TOOL,
    APPEND_FOLLOWUP_TOOL_VERSION,
)
from servicemind.tool_platform.contracts import ToolCall
from servicemind.tool_platform.runtime import build_tool_gateway

#: How long the gateway call may run. A call that inherits the intent's own expiry could
#: sit on a deadline months out; the tool definition's ``timeout_seconds`` is the real
#: ceiling, and this is only the contract's required bound.
_CALL_CEILING = timedelta(seconds=60)


class ControlledActionExecutor:
    """The only Phase 2 component allowed to perform GLPI writes.

    It writes through the tool gateway, which it did not before: the harness reached
    GlpiClient directly, so the one side effect in the platform was also the one call
    with no policy decision and no invocation row behind it. The durable harness --
    persisted-intent equality, approval status, idempotency claim, read-after-write --
    is still here, because those are properties of the *run*, not of one HTTP call. What
    moved is the GLPI I/O: the gateway now decides whether this call may happen at all,
    records that decision, and the provider re-derives from the persisted intent what
    the approval actually authorises.
    """

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
            try:
                execution = await build_tool_gateway().execute(self._tool_call(context, intent))
            except Exception:
                # Every way this can fail -- the policy refusing, the provider failing, the
                # read-back disagreeing -- leaves the action attempted and unproven, which
                # is what FAILED means. Recording it here rather than at each raise keeps
                # the status in step with the exception however the gateway grows.
                await repository.update_action_status(intent.id, ActionStatus.FAILED)
                raise
            followup_id = int(execution.output["followup_id"])
            duplicate_suppressed = bool(execution.output["duplicate_suppressed"])
            result = ExecutionResult(
                tool_name=APPEND_FOLLOWUP_TOOL,
                followup_id=followup_id,
                ticket_id=intent.target_id,
                verified=True,
                duplicate_suppressed=duplicate_suppressed,
            )
            await repository.complete_idempotency(record.id, result.model_dump())
            await repository.update_action_status(intent.id, ActionStatus.SUCCEEDED)
            if duplicate_suppressed:
                # A reconciled duplicate is a write that already happened; auditing it as
                # a new followup would put two creation events in the ledger for one row.
                return result
            await repository.audit(
                actor_id=context.user_id,
                event_type="glpi.followup.created",
                resource_type="Ticket",
                resource_id=str(intent.target_id),
                run_id=intent.run_id,
                payload={
                    "action_hash": intent.action_hash,
                    "followup_id": followup_id,
                    "verified": True,
                    "tool_call_id": str(execution.request_id),
                    "policy_decision_id": str(execution.policy_decision_id),
                },
            )
            return result

    def _tool_call(self, context: TenantContext, intent: ActionIntent) -> ToolCall:
        """The call the gateway sees, built so it can be refused on its own merits.

        ``capabilities`` names this tool because the harness is not an agent and the agent
        registry therefore has no contract to quote it from: the write is not a capability
        a model was granted, it is the one side effect the platform performs on a human's
        instruction. The fields that carry authority are the ones read from elsewhere --
        roles from the verified principal, the entity and group scope it holds, the
        approval reference, and the intent the provider resolves underneath it.

        ``approval_binding`` is the digest over this call's own arguments, tenant and run,
        so the approval is bound to the exact bytes and the exact target rather than to
        the fact that an approval exists somewhere.
        """
        deadline = datetime.now(UTC) + _CALL_CEILING
        if intent.expires_at is not None:
            deadline = min(deadline, intent.expires_at)
        call = ToolCall(
            request_id=uuid4(),
            tenant_id=context.tenant_id,
            run_id=intent.run_id,
            task_id="phase2-harness",
            user_id=context.user_id,
            roles=frozenset(context.roles),
            entity_ids=frozenset(context.allowed_glpi_entity_ids),
            group_ids=frozenset(context.allowed_glpi_group_ids),
            capabilities=frozenset({APPEND_FOLLOWUP_TOOL}),
            tool_name=APPEND_FOLLOWUP_TOOL,
            tool_version=APPEND_FOLLOWUP_TOOL_VERSION,
            arguments={
                "ticket_id": intent.target_id,
                "content": intent.arguments["content"],
                "idempotency_marker": self_authored_marker(intent.run_id, intent.action_hash),
                "is_private": bool(intent.arguments.get("is_private", True)),
            },
            approval_ref=f"action-intent://{intent.id}",
            deadline=deadline,
        )
        return call.model_copy(update={"approval_binding": call.approval_digest})


controlled_executor = ControlledActionExecutor()
