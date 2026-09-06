from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from servicemind.persistence.models import RunStatus


@pytest.mark.asyncio
async def test_recovery_uses_least_privilege_tenant_context(monkeypatch) -> None:
    from servicemind.harness import recovery

    tenant_id = UUID("11111111-1111-4111-8111-111111111111")
    run = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        user_id="original-user",
        status=RunStatus.RUNNING.value,
    )
    audits = []

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

    observed = []

    async def fake_tenants():
        return [tenant_id]

    async def fake_start(value, context):
        observed.append((value, context))

    monkeypatch.setattr(recovery, "list_tenant_ids", fake_tenants)
    monkeypatch.setattr(recovery, "ServiceMindRepository", FakeRepository)
    monkeypatch.setattr(recovery, "continue_incomplete_run", fake_start)

    assert await recovery.recover_incomplete_runs() == 1
    assert observed[0][0] is run
    context = observed[0][1]
    assert context.tenant_id == tenant_id
    assert context.roles == {"viewer", "analyst"}
    assert context.allowed_glpi_entity_ids == {1}
    assert audits[0]["event_type"] == "run.recovered"
