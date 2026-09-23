from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from servicemind.persistence.models import RunStatus


def _fixture(monkeypatch, *, checkpoint: dict, resume_raises=None):
    """Wire recovery to a single recoverable run, with or without a checkpoint.

    ``checkpoint`` is what ``get_checkpoint_state`` reports. Empty means the run reached
    the database but never reached the graph -- the case where there is no recorded
    principal to rebuild, and where the old code invented one.
    """
    from servicemind.orchestration import recovery
    from servicemind.orchestration.runtime import ResumeBlocked

    tenant_id = UUID("11111111-1111-4111-8111-111111111111")
    run = SimpleNamespace(
        id=uuid4(),
        thread_id=str(uuid4()),
        tenant_id=tenant_id,
        user_id="original-user",
        status=RunStatus.RUNNING.value,
    )
    audits = []
    failed = []

    class FakeRepository:
        def __init__(self, value: UUID) -> None:
            assert value == tenant_id

        async def get_glpi_integration(self):
            return SimpleNamespace(entity_id=1)

        async def list_recoverable_runs(self):
            return [run]

        async def get_approval(self, run_id):
            assert run_id == run.id
            return None

        async def audit(self, **kwargs):
            audits.append(kwargs)

        async def update_run(self, run_id, status, error=None):
            failed.append((run_id, status, error))

    observed = []

    async def fake_tenants():
        return [tenant_id]

    async def fake_continue(value, context):
        observed.append((value, context))
        if resume_raises is not None:
            raise resume_raises

    async def fake_checkpoint(value, context):
        return checkpoint

    monkeypatch.setattr(recovery, "list_tenant_ids", fake_tenants)
    monkeypatch.setattr(recovery, "ServiceMindRepository", FakeRepository)
    monkeypatch.setattr(recovery, "continue_incomplete_run", fake_continue)
    monkeypatch.setattr(recovery, "get_checkpoint_state", fake_checkpoint)
    return SimpleNamespace(
        recovery=recovery,
        ResumeBlocked=ResumeBlocked,
        run=run,
        observed=observed,
        audits=audits,
        failed=failed,
        tenant_id=tenant_id,
    )


@pytest.mark.asyncio
async def test_recovery_hands_over_identity_and_nothing_else(monkeypatch) -> None:
    """Recovery holds no token, so it cannot know the requester's scope.

    What it must not do is pretend otherwise. The context it builds names the subject
    and the tenant; every grant on it is resolved by the runtime against the identity
    provider, which is the only party that can answer the question.
    """
    fx = _fixture(monkeypatch, checkpoint={})

    assert await fx.recovery.recover_incomplete_runs() == 1
    assert fx.observed[0][0] is fx.run
    context = fx.observed[0][1]
    assert context.tenant_id == fx.tenant_id
    assert context.user_id == "original-user"
    assert context.roles == set()
    assert context.allowed_glpi_group_ids == set()
    assert fx.audits[0]["payload"]["principal_source"] == "verified_current_grants"


@pytest.mark.asyncio
async def test_recovery_defers_to_the_checkpoint_when_one_exists(monkeypatch) -> None:
    """A run that has a checkpoint resumes under its own recorded principal. The audit
    row must not claim the requester's current grants were the source when what ran was
    the checkpoint's principal intersected with them."""
    checkpoint: dict = {}
    fx = _fixture(monkeypatch, checkpoint=checkpoint)
    checkpoint.update({"tenant_id": str(fx.tenant_id), "user_id": fx.run.user_id})

    assert await fx.recovery.recover_incomplete_runs() == 1
    assert fx.audits[0]["payload"]["principal_source"] == "recorded_checkpoint"


@pytest.mark.asyncio
async def test_a_paused_run_is_neither_recovered_nor_failed(monkeypatch) -> None:
    """An unverifiable subject is a paused run, not a crashed one.

    Marking it FAILED would destroy recoverable work because an identity provider was
    briefly unreachable, and it would erase the distinction between "the run was
    refused" and "the run broke" -- which is the distinction an operator triages on.
    """
    from servicemind.orchestration.runtime import ResumeBlocked

    fx = _fixture(
        monkeypatch,
        checkpoint={},
        resume_raises=ResumeBlocked("unavailable", "connect error"),
    )

    assert await fx.recovery.recover_incomplete_runs() == 0
    assert fx.failed == []
    assert fx.audits == [], "the runtime writes the block's own audit row, not recovery"
