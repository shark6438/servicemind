"""Data Agent evidence-boundary guarantees.

Audit-driven regression tests (2026-09-08) for the ``DataAgent`` normalization
path. A single GLPI task must never crash the run for a legitimate but oversized
payload: ``Evidence.content`` is capped at ``EVIDENCE_CONTENT_MAX`` and the per-row
count is capped so one task cannot exhaust the joined-evidence budget. The degrade
path must also be budget-respecting instead of raising out of the sub-agent.
"""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

import servicemind.agents.data as data_module
from servicemind.agents.data import DataAcquisitionPlan, DataAgent, DataToolCall
from servicemind.domain.evidence import (
    EVIDENCE_CONTENT_MAX,
    EvidenceSourceType,
    join_evidence,
)
from servicemind.runtime.contracts import AgentInvocationContext, AgentRunStatus
from servicemind.runtime.tool_gateway import DataToolName
from servicemind.security.auth import TenantContext

TENANT = UUID("11111111-1111-4111-8111-111111111111")


class FakeRunnable:
    def __init__(self, *results) -> None:
        self.results = list(results)

    async def ainvoke(self, messages):
        return self.results.pop(0)


class FailingRunnable:
    async def ainvoke(self, messages):
        raise RuntimeError("model transport failure")


class FakeReadGateway:
    """Server-side read gateway double; echoes configured GLPI payloads."""

    def __init__(
        self,
        *,
        ticket: dict | None = None,
        groups: list[dict] | None = None,
        followups: list[dict] | None = None,
    ) -> None:
        self.ticket = (
            ticket if ticket is not None else {"id": 2, "name": "VPN outage", "priority": 3}
        )
        self.groups = groups if groups is not None else [{"id": 5, "name": "Network Team"}]
        self.followups = followups if followups is not None else []
        self.calls: list[DataToolName] = []

    async def execute(self, *, invocation, tenant_context, tool_name, ticket_id):
        assert invocation.tenant_id == tenant_context.tenant_id == TENANT
        self.calls.append(tool_name)
        if tool_name is DataToolName.GET_TICKET:
            return self.ticket
        if tool_name is DataToolName.LIST_GROUPS:
            return self.groups
        if tool_name is DataToolName.LIST_FOLLOWUPS:
            return self.followups
        raise AssertionError(tool_name)


def invocation(
    *,
    max_model_calls: int = 2,
    max_tool_calls: int = 6,
    capabilities: frozenset[str] | None = None,
) -> AgentInvocationContext:
    return AgentInvocationContext(
        run_id=uuid4(),
        tenant_id=TENANT,
        user_id="analyst-1",
        task_id="T1",
        trace_id="trace-1",
        deadline=datetime.now(UTC) + timedelta(minutes=5),
        allowed_capabilities=(
            capabilities
            if capabilities is not None
            else frozenset(item.value for item in DataToolName)
        ),
        max_model_calls=max_model_calls,
        max_tool_calls=max_tool_calls,
        policy_version="test-policy-v2",
    )


def tenant_context() -> TenantContext:
    return TenantContext(
        tenant_id=TENANT,
        user_id="analyst-1",
        username="analyst",
        roles={"analyst"},
        allowed_glpi_entity_ids={1},
    )


def full_plan(ticket_id: int = 2) -> DataAcquisitionPlan:
    return DataAcquisitionPlan(
        calls=[
            DataToolCall(
                tool_name=DataToolName.GET_TICKET,
                ticket_id=ticket_id,
                purpose="Read ticket facts.",
            ),
            DataToolCall(
                tool_name=DataToolName.LIST_GROUPS,
                ticket_id=ticket_id,
                purpose="Validate groups.",
            ),
            DataToolCall(
                tool_name=DataToolName.LIST_FOLLOWUPS,
                ticket_id=ticket_id,
                purpose="Read followup history.",
            ),
        ],
        rationale_summary="Full bounded evidence plan.",
    )


def big_ticket() -> dict:
    return {
        "id": 2,
        "name": "VPN outage",
        "priority": 3,
        "content": "A" * (EVIDENCE_CONTENT_MAX * 3),
    }


# --- F1: oversized single row is bounded, never crashes ----------------------


def test_bounded_json_content_truncates_oversized_payload() -> None:
    payload = {"content": "B" * (EVIDENCE_CONTENT_MAX * 2), "id": 1}
    text, truncated = data_module._bounded_json_content(payload)
    assert truncated is True
    assert len(text) == EVIDENCE_CONTENT_MAX
    assert data_module._EVIDENCE_TRUNCATION_SUFFIX in text


def test_bounded_json_content_passthrough_when_small() -> None:
    payload = {"content": "short", "id": 1}
    text, truncated = data_module._bounded_json_content(payload)
    assert truncated is False
    assert json.loads(text) == payload


@pytest.mark.asyncio
async def test_oversized_ticket_payload_never_crashes_evidence(monkeypatch) -> None:
    plan = full_plan()
    monkeypatch.setattr(data_module, "structured_output", lambda model, schema: FakeRunnable(plan))
    gateway = FakeReadGateway(ticket=big_ticket())
    agent = DataAgent(gateway=gateway, model_factory=lambda: object())
    result = await agent.run(
        invocation=invocation(),
        tenant_context=tenant_context(),
        objective="Analyze this ticket",
        ticket_id=2,
    )
    assert result.status is AgentRunStatus.SUCCEEDED
    ticket_rows = [item for item in result.output if item.resource_type == "ticket"]
    assert len(ticket_rows) == 1
    row = ticket_rows[0]
    assert len(row.content) <= EVIDENCE_CONTENT_MAX
    assert row.metadata.get("content_truncated") is True
    # Full facts survive for the deterministic fallback path.
    assert row.metadata["ticket_facts"]["content"] == big_ticket()["content"]


@pytest.mark.asyncio
async def test_oversized_followups_each_bounded(monkeypatch) -> None:
    plan = full_plan()
    monkeypatch.setattr(data_module, "structured_output", lambda model, schema: FakeRunnable(plan))
    followups = [
        {"id": i, "content": "<div>" + "C" * (EVIDENCE_CONTENT_MAX * 2) + "</div>"}
        for i in range(1, 6)
    ]
    gateway = FakeReadGateway(followups=followups)
    agent = DataAgent(gateway=gateway, model_factory=lambda: object())
    result = await agent.run(
        invocation=invocation(),
        tenant_context=tenant_context(),
        objective="Read the followup history for this VPN ticket",
        ticket_id=2,
    )
    followup_rows = [item for item in result.output if item.resource_type == "ticket_followup"]
    assert len(followup_rows) == 5
    assert all(len(item.content) <= EVIDENCE_CONTENT_MAX for item in followup_rows)
    assert all(item.metadata["content_truncated"] for item in followup_rows)


# --- F2: row-count ceilings keep one task inside the join budget --------------


@pytest.mark.asyncio
async def test_group_and_followup_row_ceilings(monkeypatch) -> None:
    plan = full_plan()
    monkeypatch.setattr(data_module, "structured_output", lambda model, schema: FakeRunnable(plan))
    groups = [{"id": i, "name": f"Team {i:02d}"} for i in range(1, 61)]
    followups = [{"id": i, "content": f"note {i}"} for i in range(1, 41)]
    gateway = FakeReadGateway(groups=groups, followups=followups)
    agent = DataAgent(gateway=gateway, model_factory=lambda: object())
    result = await agent.run(
        invocation=invocation(),
        tenant_context=tenant_context(),
        objective="Summarize the followup history on this chatty ticket",
        ticket_id=2,
    )
    group_rows = [item for item in result.output if item.resource_type == "support_group"]
    followup_rows = [item for item in result.output if item.resource_type == "ticket_followup"]
    assert len(group_rows) == data_module._MAX_SUPPORT_GROUP_ROWS  # 50
    assert len(followup_rows) == data_module._MAX_FOLLOWUP_ROWS  # 15
    # Only the most recent followups are kept.
    assert sorted(int(item.resource_id) for item in followup_rows) == list(range(26, 41))
    # The bounded output must still fit the joined-evidence budget with room to spare.
    joined = join_evidence(TENANT, result.output)
    assert len(joined.items) <= 100
    assert joined.items[0].source_type is EvidenceSourceType.GLPI


# --- F3: the degrade path is budget-respecting, never raising ------------------


@pytest.mark.asyncio
async def test_plan_degrade_drops_followups_when_tool_budget_is_two(monkeypatch) -> None:
    monkeypatch.setattr(data_module, "structured_output", lambda model, schema: FailingRunnable())
    gateway = FakeReadGateway()
    agent = DataAgent(gateway=gateway, model_factory=lambda: object())
    result = await agent.run(
        invocation=invocation(max_tool_calls=2),
        tenant_context=tenant_context(),
        objective="Read the followup history for this ticket",
        ticket_id=2,
    )
    # The history-flagged minimum needs 3 calls; under a 2-tool budget the degrade
    # drops followups instead of raising out of the sub-agent.
    assert result.status is AgentRunStatus.DEGRADED
    assert result.failure_code == "DATA_PLAN_DEGRADED"
    assert set(gateway.calls) == {
        DataToolName.GET_TICKET,
        DataToolName.LIST_GROUPS,
    }
    assert result.metrics.tool_calls == 2


@pytest.mark.asyncio
async def test_plan_degrade_keeps_single_ticket_read_at_budget_one(monkeypatch) -> None:
    monkeypatch.setattr(data_module, "structured_output", lambda model, schema: FailingRunnable())
    gateway = FakeReadGateway()
    agent = DataAgent(gateway=gateway, model_factory=lambda: object())
    result = await agent.run(
        invocation=invocation(max_tool_calls=1),
        tenant_context=tenant_context(),
        objective="Analyze this ticket",
        ticket_id=2,
    )
    assert result.status is AgentRunStatus.DEGRADED
    assert gateway.calls == [DataToolName.GET_TICKET]
    assert len(result.output) == 1  # ticket row survives as bounded evidence


# --- F4: zero model budget is reported DEGRADED, never silently SUCCEEDED ------


@pytest.mark.asyncio
async def test_zero_model_budget_is_an_explicit_degraded(monkeypatch) -> None:
    gateway = FakeReadGateway()
    agent = DataAgent(gateway=gateway, model_factory=lambda: object())
    result = await agent.run(
        invocation=invocation(max_model_calls=0, max_tool_calls=6),
        tenant_context=tenant_context(),
        objective="Analyze this ticket",
        ticket_id=2,
    )
    assert result.status is AgentRunStatus.DEGRADED
    assert result.failure_code == "DATA_MODEL_BUDGET_EXHAUSTED"
    assert set(gateway.calls) == {DataToolName.GET_TICKET, DataToolName.LIST_GROUPS}
    assert len(result.output) == 2  # deterministic minimum still delivers evidence


@pytest.mark.asyncio
async def test_zero_tool_budget_performs_no_convenience_read(monkeypatch) -> None:
    gateway = FakeReadGateway()
    agent = DataAgent(gateway=gateway, model_factory=lambda: object())
    result = await agent.run(
        invocation=invocation(max_model_calls=0, max_tool_calls=0),
        tenant_context=tenant_context(),
        objective="Analyze this ticket",
        ticket_id=2,
    )
    assert result.status is AgentRunStatus.DEGRADED
    assert gateway.calls == []
    assert result.output == []
    assert result.metrics.tool_calls == 0


@pytest.mark.asyncio
async def test_degraded_plan_never_invents_a_missing_capability(monkeypatch) -> None:
    monkeypatch.setattr(data_module, "structured_output", lambda model, schema: FailingRunnable())
    gateway = FakeReadGateway()
    result = await DataAgent(gateway=gateway, model_factory=lambda: object()).run(
        invocation=invocation(max_tool_calls=6, capabilities=frozenset()),
        tenant_context=tenant_context(),
        objective="Analyze this ticket",
        ticket_id=2,
    )
    assert result.status is AgentRunStatus.DEGRADED
    assert gateway.calls == []
    assert result.output == []
