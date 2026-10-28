"""End-to-end routing through ``route_node`` with a stub advisor.

The graph is driven to its terminal ``unsupported`` node, which needs no model calls:
that is enough to prove the advisor is wired into the real route step, that the event
payload is additive and text-free, and that an advisor failure is indistinguishable
from the rule-only route.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from core import settings
from servicemind.domain.routing import RouteDecision, RouteType
from servicemind.orchestration.jev_router import (
    SOURCE_FALLBACK,
    SOURCE_OVERRIDE,
    JevRoutingOutcome,
    rule_only_outcome,
)
from servicemind.orchestration.supervisor_workflow import (
    SupervisorRuntimeServices,
    build_supervisor_graph,
    jev_router,
)

TENANT = UUID("11111111-1111-4111-8111-111111111111")
EVENT_KEYS = {
    "route",
    "reason_code",
    "mode",
    "source",
    "fallback_reason",
    "rule_route",
    "rule_reason_code",
    "jev_route",
    "jev_confidence",
    "served_model",
    "latency_ms",
    "cost_usd",
    "cost_source",
    "input_tokens",
    "output_tokens",
    "http_status",
    "attempt_id",
}


class FakeRepository:
    events: list[tuple[str, dict]] = []

    def __init__(self, tenant_id: UUID) -> None:
        assert tenant_id == TENANT

    @classmethod
    def reset(cls) -> None:
        cls.events = []

    async def update_run(self, run_id, status, *, result=None, error=None):
        return SimpleNamespace(id=run_id, status=status.value, result=result, error=error)

    async def append_event(self, run_id, event_type, payload):
        self.events.append((event_type, payload))
        return SimpleNamespace()


class FixedAdvisor:
    def __init__(self, factory) -> None:
        self.factory = factory
        self.calls: list[tuple[str, bool]] = []

    async def decide(self, *, raw_request, request_write, fallback, run_id=None):
        self.calls.append((raw_request, request_write))
        return self.factory(fallback)


class RaisingAdvisor:
    def __init__(self) -> None:
        self.calls = 0

    async def decide(self, **_):
        self.calls += 1
        raise RuntimeError("advisor exploded")


def initial(goal: str, *, write: bool = False) -> dict:
    return {
        "run_id": str(uuid4()),
        "tenant_id": str(TENANT),
        "user_id": "user-1",
        "username": "analyst",
        "roles": ["viewer", "analyst"],
        "allowed_glpi_entity_ids": [1],
        "thread_id": str(uuid4()),
        "ticket_id": 2,
        "raw_request": goal,
        "goal": goal,
        "request_write": write,
        "data_evidence": [],
        "knowledge_evidence": [],
        "branch_timings": [],
        "branch_errors": [],
        "task_completions": [],
        "trajectory": [],
        "control": {},
        "control_owner": "supervisor",
        "active_agent": "router",
        "evidence_dirty": False,
        "plan_revision": 0,
    }


def services(advisor) -> SupervisorRuntimeServices:
    return SupervisorRuntimeServices(repository_factory=FakeRepository, jev_router=advisor)


def route_event() -> dict:
    matches = [payload for name, payload in FakeRepository.events if name == "route.completed"]
    assert len(matches) == 1
    return matches[0]


@pytest.fixture(autouse=True)
def reset_repository():
    FakeRepository.reset()


def test_pytest_environment_forces_the_advisor_off() -> None:
    assert settings.SERVICEMIND_JEV_ROUTER_MODE == "off"


def test_frozen_router_files_are_unchanged() -> None:
    root = __import__("pathlib").Path(__file__).resolve().parents[2]
    expected = {
        "src/servicemind/orchestration/router.py": (
            "9c845ab198c6d9d212e708bbe32d8c70df35315e4c3704dcd7ae6a95c12fa59f"
        ),
        "src/servicemind/domain/routing.py": (
            "c4a22493f53b30c8a35542d1aa0178f6a182198948a2c43bcdb1df377aed2a85"
        ),
    }
    for relative, digest in expected.items():
        actual = hashlib.sha256((root / relative).read_bytes()).hexdigest()
        assert actual == digest, relative


def test_default_services_wire_the_off_singleton() -> None:
    assert SupervisorRuntimeServices().jev_router is jev_router
    assert str(settings.SERVICEMIND_JEV_ROUTER_MODE) == "off"


@pytest.mark.asyncio
async def test_rule_refusal_still_ends_at_rejected_with_a_text_free_event() -> None:
    advisor = FixedAdvisor(rule_only_outcome)
    result = await build_supervisor_graph(services(advisor)).ainvoke(
        initial("delete ticket 4110 now", write=True)
    )

    assert result["final_result"]["status"] == "rejected"
    assert result["final_result"]["route"]["route"] == "unsupported"
    assert advisor.calls and advisor.calls[0][1] is True

    payload = route_event()
    assert set(payload) <= EVENT_KEYS
    assert "delete ticket 4110 now" not in json.dumps(payload, ensure_ascii=False)
    assert payload["source"] == "rule"
    assert payload["rule_route"] == "unsupported"


@pytest.mark.asyncio
async def test_advisor_can_tighten_a_write_and_the_graph_executes_nothing() -> None:
    def tighten(fallback: RouteDecision) -> JevRoutingOutcome:
        assert fallback.route is RouteType.COMPLEX_WORKFLOW
        return JevRoutingOutcome(
            decision=RouteDecision(
                route=RouteType.UNSUPPORTED,
                required_capabilities=[],
                reason_code="jev_model_refusal",
                confidence=0.99,
            ),
            source=SOURCE_OVERRIDE,
            jev_route=RouteType.UNSUPPORTED,
            jev_confidence=0.99,
        )

    result = await build_supervisor_graph(services(FixedAdvisor(tighten))).ainvoke(
        initial("分析工单 4110 并给出处理建议", write=True)
    )

    assert result["final_result"]["status"] == "rejected"
    assert result["route"]["required_capabilities"] == []
    payload = route_event()
    assert payload["source"] == SOURCE_OVERRIDE
    assert payload["route"] == "unsupported"
    assert not [name for name, _ in FakeRepository.events if name.startswith("action.")]


@pytest.mark.asyncio
async def test_advisor_failure_is_identical_to_rule_only() -> None:
    request = "delete ticket 4110 now"

    baseline = await build_supervisor_graph(services(FixedAdvisor(rule_only_outcome))).ainvoke(
        initial(request, write=True)
    )
    baseline_result = baseline["final_result"]
    baseline_events = list(FakeRepository.events)
    FakeRepository.reset()

    advisor = RaisingAdvisor()
    result = await build_supervisor_graph(services(advisor)).ainvoke(initial(request, write=True))

    assert advisor.calls == 1
    assert result["final_result"] == baseline_result
    payload = route_event()
    assert payload["source"] == SOURCE_FALLBACK
    assert payload["fallback_reason"] == "internal_error"
    assert payload["route"] == "unsupported"
    # Same route and rejection, but the fallback event carries its audit fields.
    assert [name for name, _ in FakeRepository.events] == [name for name, _ in baseline_events]
