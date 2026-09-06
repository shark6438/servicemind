import logging

from servicemind.domain.models import ApprovalDecision
from servicemind.orchestration.runtime import continue_incomplete_run, resume_run
from servicemind.persistence.models import RunStatus
from servicemind.persistence.repository import ServiceMindRepository, list_tenant_ids
from servicemind.security.auth import TenantContext

logger = logging.getLogger(__name__)


async def recover_incomplete_runs() -> int:
    """Resume durable work left pending/running by a terminated API process.

    Recovery uses the tenant's configured GLPI entity and a least-privilege service
    principal. A write-requesting run can only advance to WAITING_APPROVAL; execution
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
            context = TenantContext(
                tenant_id=tenant_id,
                user_id=run.user_id,
                username=run.user_id,
                roles={"viewer", "analyst"},
                allowed_glpi_entity_ids={integration.entity_id},
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
                    payload={"previous_status": run.status},
                )
            except Exception as exc:
                logger.exception("Failed to recover ServiceMind run %s", run.id)
                await repository.update_run(
                    run.id, RunStatus.FAILED, error=f"Recovery:{type(exc).__name__}"
                )
    return recovered
