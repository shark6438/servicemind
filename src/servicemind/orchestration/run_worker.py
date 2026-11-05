"""Independent Agent run worker with PostgreSQL leases and tenant capacity.

`agent_runs` is the queue's durable source of truth. Redis is not required to find
work, so losing a notification cannot lose a run. Every requester is revalidated at
execution; the worker owns no bearer token and cannot invent grants for a user.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from datetime import UTC, datetime
from uuid import UUID, uuid4

from servicemind.domain.models import ApprovalDecision
from servicemind.foundation.errors import bounded_error_text
from servicemind.observability.metrics import (
    record_queue_wait,
    record_recovery,
    record_run_execution,
)
from servicemind.observability.tracing import phase_span
from servicemind.orchestration.runtime import (
    ResumeBlocked,
    continue_incomplete_run,
    decline_run,
    get_checkpoint_state,
    resolve_resume_scope,
    resume_run,
    run_config,
    start_run,
)
from servicemind.orchestration.supervisor_workflow import supervisor_graph
from servicemind.persistence.models import AgentRun, RunStatus
from servicemind.persistence.repository import RunCancelled, ServiceMindRepository, list_tenant_ids
from servicemind.persistence.run_queue import ACTIVE_STATUSES, RunQueue
from servicemind.security.auth import TenantContext
from servicemind.security.entitlements import revalidate

logger = logging.getLogger(__name__)

BLOCKED_RETRY_SECONDS = 60
BUSY_RETRY_SECONDS = 5


class RunWorker:
    def __init__(
        self,
        *,
        per_tenant_limit: int,
        concurrency: int,
        lease_seconds: int,
        poll_seconds: float,
        worker_id: str | None = None,
        shutdown_timeout: float = 30,
    ) -> None:
        if min(per_tenant_limit, concurrency, lease_seconds) < 1 or poll_seconds <= 0:
            raise ValueError("worker limits and poll interval must be positive")
        self.per_tenant_limit = per_tenant_limit
        self.concurrency = concurrency
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.worker_id = worker_id or f"run-worker-{uuid4()}"
        self.shutdown_timeout = shutdown_timeout
        self._tenant_cursor = 0

    async def claim_next(self) -> tuple[RunQueue, AgentRun, str] | None:
        tenants = await list_tenant_ids()
        if not tenants:
            return None
        # Rotating the first tenant prevents one noisy tenant from monopolizing
        # this worker's claim slots. PostgreSQL enforces the cross-process cap.
        start = self._tenant_cursor % len(tenants)
        for offset in range(len(tenants)):
            index = (start + offset) % len(tenants)
            queue = RunQueue(tenants[index])
            owner = f"{self.worker_id}:{uuid4()}"
            run = await queue.claim(
                owner,
                lease_seconds=self.lease_seconds,
                per_tenant_limit=self.per_tenant_limit,
            )
            if run is not None:
                self._tenant_cursor = index + 1
                return queue, run, owner
        self._tenant_cursor = start + 1
        return None

    async def _heartbeat(
        self, queue: RunQueue, run_id: UUID, owner: str, executing: asyncio.Task[None]
    ) -> None:
        try:
            while True:
                await asyncio.sleep(max(self.lease_seconds / 3, 1))
                if not await queue.renew(run_id, owner, lease_seconds=self.lease_seconds):
                    logger.error("Run %s lost its lease; cancelling graph invocation", run_id)
                    executing.cancel()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Run %s could not renew lease; cancelling graph", run_id)
            executing.cancel()

    async def _execute_graph(self, run: AgentRun) -> None:
        repository = ServiceMindRepository(run.tenant_id)
        webhook_origin = False
        if run.user_id == "glpi-webhook":
            first = await repository.list_events(run.id, limit=1)
            webhook_origin = bool(
                first and first[0].sequence == 1 and first[0].event_type == "webhook.accepted"
            )
        if webhook_origin:
            # The signed webhook was verified before the run was persisted. The
            # first event is written atomically by that admission path. A human's
            # OIDC subject matching the reserved label alone confers no authority.
            if run.request_write:
                raise PermissionError("Webhook runs cannot request writes")
            integration = await repository.get_glpi_integration()
            if integration is None:
                raise ResumeBlocked("integration_unavailable")
            scope = run.access_scope or {}
            if (
                scope.get("entity_ids") != [integration.entity_id]
                or run.thread_id != f"glpi-webhook-{run.id}"
            ):
                raise PermissionError("Webhook integration scope changed")
            context = TenantContext(
                tenant_id=run.tenant_id,
                user_id=run.user_id,
                username=run.user_id,
                roles={"viewer", "analyst"},
                allowed_glpi_entity_ids={integration.entity_id},
            )
            snapshot = await get_checkpoint_state(run, context)
            if snapshot:
                if (
                    snapshot.get("tenant_id") != str(run.tenant_id)
                    or snapshot.get("user_id") != run.user_id
                    or set(snapshot.get("allowed_glpi_entity_ids") or []) != {integration.entity_id}
                    or snapshot.get("request_write")
                ):
                    raise PermissionError("Webhook checkpoint scope changed")
                # Webhook runs are read-only and stay within the integration entity.
                # They have no human bearer token to revalidate on a restart.
                await supervisor_graph.ainvoke(None, config=run_config(run, context))
            else:
                await start_run(run, context)
            return

        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id, username=run.user_id)
        snapshot = await get_checkpoint_state(run, context)
        if not snapshot:
            if run.status == RunStatus.WAITING_APPROVAL.value:
                raise PermissionError("Approval recovery requires its checkpoint")
            result = await revalidate(run.tenant_id, run.user_id, fresh=True)
            if not result.established:
                raise ResumeBlocked(result.outcome.value, result.detail)
            if "analyst" not in result.roles:
                raise ResumeBlocked("insufficient_authority", "analyst role revoked")
            # A request may sit in the queue while the requester's grants change.
            # The new run can use only the intersection of the submitted scope and
            # freshly verified grants, never newly acquired entities or groups.
            scope = run.access_scope or {}
            entities = set(scope.get("entity_ids") or []) & set(result.entity_ids)
            groups = set(scope.get("group_ids") or []) & set(result.group_ids)
            await start_run(
                run,
                TenantContext(
                    tenant_id=run.tenant_id,
                    user_id=run.user_id,
                    username=run.user_id,
                    roles=set(result.roles),
                    allowed_glpi_entity_ids=entities,
                    allowed_glpi_group_ids=groups,
                ),
            )
            return

        approval = await repository.get_approval(run.id)
        if approval is not None and run.status in {
            RunStatus.RUNNING.value,
            RunStatus.WAITING_APPROVAL.value,
        }:
            # A synchronous approval response may have been lost after persisting
            # the decision. Replay the durable decision under fresh authority.
            decision = ApprovalDecision.model_validate(
                {
                    "decision": approval.decision,
                    "decided_by": approval.decided_by,
                    "comment": approval.comment,
                }
            )
            if approval.decision == "rejected":
                await decline_run(run, context, decision)
                return
            approval_scope = await resolve_resume_scope(run, context)
            if approval_scope.void:
                # This action was approved before the requester lost access to its
                # evidence. It cannot be re-derived in place: the historical approval
                # row still points at the old intent. Fail closed and require a fresh
                # run rather than executing a stale approved action.
                raise PermissionError("Approved action became invalid before recovery")
            await resume_run(
                run,
                context,
                decision,
                scope=approval_scope,
            )
        else:
            await continue_incomplete_run(run, context)

    async def execute_claimed(self, queue: RunQueue, run: AgentRun, owner: str) -> None:
        delay = 0
        release = True
        outcome = "succeeded"
        started = time.perf_counter()
        current = asyncio.current_task()
        assert current is not None
        recovered = run.status != RunStatus.PENDING.value
        async with queue.execution_guard(run.id, per_tenant_limit=self.per_tenant_limit) as locked:
            if not locked:
                # Another process still holds a session lock from the prior lease,
                # or all tenant execution slots are occupied. Never race its graph.
                await queue.release(run.id, owner, delay_seconds=BUSY_RETRY_SECONDS)
                return
            # A process can pause between claiming and acquiring its execution lock.
            # Recheck ownership and status before spending a graph or model call.
            if not await queue.renew(run.id, owner, lease_seconds=self.lease_seconds):
                return
            fresh_run = await ServiceMindRepository(run.tenant_id).get_run(run.id)
            approval_wait = (
                fresh_run is not None
                and fresh_run.status == RunStatus.WAITING_APPROVAL.value
                and await ServiceMindRepository(run.tenant_id).get_approval(run.id) is not None
            )
            if fresh_run is None or (fresh_run.status not in ACTIVE_STATUSES and not approval_wait):
                await queue.release(run.id, owner)
                return
            run = fresh_run
            heartbeat = asyncio.create_task(
                self._heartbeat(queue, run.id, owner, current),
                name=f"run-heartbeat-{run.id}",
            )
            try:
                if not recovered and run.created_at is not None:
                    waited = (datetime.now(UTC) - run.created_at).total_seconds()
                    record_queue_wait(max(waited, 0))
                with phase_span("run.execute", run_id=str(run.id), worker_id=self.worker_id):
                    await self._execute_graph(run)
                fresh = await ServiceMindRepository(run.tenant_id).get_run(run.id)
                if fresh is None or fresh.status in ACTIVE_STATUSES:
                    # The graph did not settle the row. Do not silently loop forever.
                    raise RuntimeError("Graph returned without a terminal or paused run status")
                if fresh.status in {
                    RunStatus.WAITING_APPROVAL.value,
                    RunStatus.WAITING_REVIEW.value,
                }:
                    outcome = "paused"
                elif fresh.status == RunStatus.FAILED.value:
                    outcome = "failed"
                elif fresh.status == RunStatus.CANCELLED.value:
                    outcome = "cancelled"
                if recovered:
                    record_recovery("recovered")
            except ResumeBlocked as exc:
                outcome = "paused"
                delay = BLOCKED_RETRY_SECONDS
                logger.warning("Run %s deferred: %s", run.id, exc.reason)
                if recovered:
                    record_recovery("blocked")
            except RunCancelled:
                outcome = "cancelled"
            except asyncio.CancelledError:
                outcome = "cancelled"
                release = False
                raise
            except Exception as exc:
                outcome = "failed"
                logger.exception("Run %s failed in independent worker", run.id)
                if recovered:
                    record_recovery("failed")
                repository = ServiceMindRepository(run.tenant_id)
                await repository.append_event(
                    run.id,
                    "run.failed",
                    {"reason": bounded_error_text(exc), "stage": "worker"},
                )
                await repository.update_run(run.id, RunStatus.FAILED, error=bounded_error_text(exc))
            finally:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat
                record_run_execution(time.perf_counter() - started, outcome)
                if release:
                    await queue.release(run.id, owner, delay_seconds=delay)

    async def once(self) -> bool:
        claimed = await self.claim_next()
        if claimed is None:
            return False
        await self.execute_claimed(*claimed)
        return True

    async def run(self, stop: asyncio.Event) -> None:
        active: set[asyncio.Task[None]] = set()

        def completed(task: asyncio.Task[None]) -> None:
            active.discard(task)
            if not task.cancelled() and (error := task.exception()) is not None:
                logger.error("Run worker task failed outside graph handling", exc_info=error)

        try:
            while not stop.is_set():
                while len(active) < self.concurrency and not stop.is_set():
                    claimed = await self.claim_next()
                    if claimed is None:
                        break
                    task = asyncio.create_task(self.execute_claimed(*claimed))
                    active.add(task)
                    task.add_done_callback(completed)
                if active:
                    await asyncio.wait(
                        active, timeout=self.poll_seconds, return_when=asyncio.FIRST_COMPLETED
                    )
                else:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
        finally:
            if active:
                done, pending = await asyncio.wait(active, timeout=self.shutdown_timeout)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    with suppress(asyncio.CancelledError):
                        task.result()
