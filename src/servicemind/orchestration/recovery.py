import logging

from servicemind.domain.models import ApprovalDecision
from servicemind.orchestration.runtime import (
    ResumeBlocked,
    continue_incomplete_run,
    get_checkpoint_state,
    resume_run,
)
from servicemind.persistence.models import RunStatus
from servicemind.persistence.repository import ServiceMindRepository, list_tenant_ids
from servicemind.security.auth import TenantContext

logger = logging.getLogger(__name__)


def _caller_context(tenant_id, run, integration) -> TenantContext:
    """What recovery hands to the runtime: identifiers only, no authority.

    Recovery holds no token. Everything a principal needs *except* the subject is
    therefore unknowable here, and this context is built to say so rather than to
    guess: the roles and scope on it are never used to run anything. The subject is
    ``run.user_id``, which is the one thing the run row does record, and the runtime
    resolves what that subject currently holds before it will resume anything.

    ``integration`` is passed only so the tenant's own GLPI entity is available for the
    branch that cannot reach the graph at all; it is not a substitute for verification.
    """
    return TenantContext(
        tenant_id=tenant_id,
        user_id=run.user_id,
        username=run.user_id,
        allowed_glpi_entity_ids={integration.entity_id},
    )


async def recover_incomplete_runs() -> int:
    """Resume durable work left pending/running by a terminated API process.

    Every branch runs as the run's own requester, re-verified now, never as the caller:

    * With a checkpoint, ``continue_incomplete_run``/``resume_run`` rebuild the run's
      recorded principal, re-verify it against the identity provider, and enforce the
      intersection. The context built here is used for nothing but identifying the
      subject and the tenant.
    * Without a checkpoint there is nothing to rebuild, so the run is started under
      what the requester is verified to hold at this moment -- not under a scope this
      process invented for them.
    * If the authority cannot be established, the run is left exactly where it is.
      Recovery holds no token, so it is not entitled to decide that an unverifiable
      run should proceed anyway.

    Either way a write-requesting run can only advance to WAITING_APPROVAL; execution
    still requires a fresh approver token and the persisted action hash.
    """
    recovered = 0
    for tenant_id in await list_tenant_ids():
        repository = ServiceMindRepository(tenant_id)
        integration = await repository.get_glpi_integration()
        if integration is None:
            continue
        for run in await repository.list_recoverable_runs():
            approval = await repository.get_approval(run.id)
            if run.status == RunStatus.WAITING_APPROVAL.value and approval is None:
                continue
            context = _caller_context(tenant_id, run, integration)
            # Read once, up front, purely so the audit row can say which principal
            # actually executed. Guessing would put a scope the run never had into the
            # very record used to reconstruct its history.
            principal_source = (
                "recorded_checkpoint"
                if await get_checkpoint_state(run, context)
                else "verified_current_grants"
            )
            try:
                if approval is None:
                    await continue_incomplete_run(run, context)
                else:
                    await resume_run(
                        run,
                        context,
                        ApprovalDecision.model_validate(
                            {
                                "decision": approval.decision,
                                "decided_by": approval.decided_by,
                                "comment": approval.comment,
                            }
                        ),
                    )
                recovered += 1
                await repository.audit(
                    actor_id="servicemind-recovery",
                    event_type="run.recovered",
                    resource_type="AgentRun",
                    resource_id=str(run.id),
                    run_id=run.id,
                    payload={
                        "previous_status": run.status,
                        "principal_source": principal_source,
                    },
                )
            except ResumeBlocked:
                # Not a failure. The run is paused where it stands, its own audit row
                # already written by the runtime, and the next pass will pick it up if
                # the authority can be established then. Marking it FAILED here would
                # destroy recoverable work because an identity provider was briefly
                # unreachable -- and would make "the run was refused" indistinguishable
                # from "the run crashed".
                logger.warning(
                    "ServiceMind run %s left paused: the requester's authority could "
                    "not be established",
                    run.id,
                )
            except Exception as exc:
                logger.exception("Failed to recover ServiceMind run %s", run.id)
                await repository.update_run(
                    run.id, RunStatus.FAILED, error=f"Recovery:{type(exc).__name__}"
                )
    return recovered
