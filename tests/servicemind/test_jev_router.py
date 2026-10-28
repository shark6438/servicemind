"""Behaviour matrix, validation and failure containment for the Jev advisor.

Every test uses ``httpx.MockTransport``; nothing reaches a provider and nothing is
billed. The recurring assertion is the same: whenever anything is wrong, the served
decision is exactly the rule decision.
"""

from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from servicemind.domain.routing import RouteDecision, RouteType
from servicemind.domain.task import AgentName
from servicemind.orchestration.jev_router import (
    PROMPT_HASH,
    ROUTE_CAPABILITIES,
    SOURCE_CONFIRMED,
    SOURCE_FALLBACK,
    SOURCE_OVERRIDE,
    SOURCE_RULE,
    SOURCE_SHADOW,
    JevRouter,
    JevRouterConfig,
    JevRoutingOutcome,
    _RouteGuards,
    route_completed_payload,
)

SERVED_MODEL = "typesafe/jev-1.14-20261014"
FROZEN_PROMPT_HASH = "c7178c51d12f1e79e251e263843eb2c8c97a95b5cbfd8b040113da403f46abb3"

RULE_WRITE = RouteDecision(
    route=RouteType.COMPLEX_WORKFLOW,
    required_capabilities=[
        AgentName.DATA,
        AgentName.KNOWLEDGE,
        AgentName.ANALYSIS,
        AgentName.REVIEWER,
        AgentName.ACTION,
    ],
    reason_code="analysis_or_action_required",
    confidence=0.98,
)
RULE_COMPLEX = RouteDecision(
    route=RouteType.COMPLEX_WORKFLOW,
    required_capabilities=[
        AgentName.DATA,
        AgentName.KNOWLEDGE,
        AgentName.ANALYSIS,
        AgentName.REVIEWER,
    ],
    reason_code="analysis_or_action_required",
    confidence=0.98,
)
RULE_REFUSAL = RouteDecision(
    route=RouteType.UNSUPPORTED,
    required_capabilities=[],
    reason_code="forbidden_operation",
    confidence=1,
)
RULE_KNOWLEDGE = RouteDecision(
    route=RouteType.SIMPLE_KNOWLEDGE_QUERY,
    required_capabilities=[AgentName.KNOWLEDGE],
    reason_code="knowledge_lookup_only",
    confidence=0.97,
)


def make_config(tmp_path: Path, **overrides) -> JevRouterConfig:
    values = dict(
        mode="active",
        endpoint="https://provider.invalid/decisions",
        request_model="typesafe/jev-1.14",
        served_model=SERVED_MODEL,
        confidence_floor=0.9,
        timeout_seconds=1.0,
        max_payload_bytes=12_000,
        context_ceiling=262_144,
        price_usd_per_million_input_tokens=0.042,
        daily_budget_usd=10.0,
        per_minute_limit=100,
        concurrency_limit=4,
        circuit_failures=5,
        circuit_reset_seconds=60.0,
        ledger_dir=tmp_path,
        ledger_timeout_seconds=5.0,
        api_key="test-key",
    )
    values.update(overrides)
    return JevRouterConfig(**values)


def decision_body(
    route: str = "simple_knowledge_query",
    *,
    confidence: float = 0.99,
    model: str = SERVED_MODEL,
    usage: dict | None = None,
) -> dict:
    probabilities = {item.value: 0.0 for item in RouteType}
    probabilities[route] = 1.0
    return {
        "model": model,
        "answers": {
            "route": {
                "type": "choice",
                "choice": route,
                "probabilities": probabilities,
                "confidence": confidence,
            }
        },
        "usage": usage
        if usage is not None
        else {"input_tokens": 100, "output_tokens": 5, "cost": 0.0000042},
    }


def router_for(
    tmp_path: Path,
    handler,
    *,
    guards: _RouteGuards | None = None,
    **overrides,
) -> JevRouter:
    return JevRouter(
        config=make_config(tmp_path, **overrides),
        transport=httpx.MockTransport(handler),
        guards=guards,
    )


async def decide(
    router: JevRouter,
    *,
    request_write: bool = False,
    fallback: RouteDecision = RULE_KNOWLEDGE,
    raw: str = "查询 VPN 操作手册",
) -> JevRoutingOutcome:
    return await router.decide(
        raw_request=raw,
        request_write=request_write,
        fallback=fallback,
        run_id="run-1",
    )


def test_prompt_hash_is_frozen() -> None:
    assert PROMPT_HASH == FROZEN_PROMPT_HASH


def test_capabilities_are_fixed_per_route_and_never_include_action() -> None:
    assert ROUTE_CAPABILITIES[RouteType.UNSUPPORTED] == ()
    assert ROUTE_CAPABILITIES[RouteType.SIMPLE_KNOWLEDGE_QUERY] == (AgentName.KNOWLEDGE,)
    assert ROUTE_CAPABILITIES[RouteType.SIMPLE_DATA_QUERY] == (AgentName.DATA,)
    assert ROUTE_CAPABILITIES[RouteType.COMPLEX_WORKFLOW] == (
        AgentName.DATA,
        AgentName.KNOWLEDGE,
        AgentName.ANALYSIS,
        AgentName.REVIEWER,
    )
    for capabilities in ROUTE_CAPABILITIES.values():
        assert AgentName.ACTION not in capabilities


@pytest.mark.asyncio
async def test_payload_is_whitelisted(tmp_path: Path) -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["document"] = json.loads(request.content)
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json=decision_body())

    await decide(router_for(tmp_path, handler), raw="查询 VPN 操作手册 SECRET-SENTINEL")

    document = captured["document"]
    assert set(document) == {"model", "state", "questions"}
    assert set(document["state"]) == {"request", "request_write"}
    assert set(document["questions"]) == {"route"}
    assert "run-1" not in json.dumps(document)
    assert captured["authorization"] == "Bearer test-key"


@pytest.mark.asyncio
async def test_off_mode_never_reads_config_or_touches_the_network() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("off mode must not call the provider")

    def forbidden_factory() -> JevRouterConfig:
        raise AssertionError("off mode must not read router config")

    router = JevRouter(
        transport=httpx.MockTransport(handler),
        config_factory=forbidden_factory,
        mode_provider=lambda: "off",
    )
    outcome = await decide(router)

    assert outcome.source == SOURCE_RULE
    assert outcome.decision == RULE_KNOWLEDGE
    assert JevRouterConfig.from_settings(_settings(mode="off")) is None


class _Secret:
    def get_secret_value(self) -> str:
        return "test-key"


def _settings(*, mode: str):
    from types import SimpleNamespace

    return SimpleNamespace(
        SERVICEMIND_JEV_ROUTER_MODE=mode,
        SERVICEMIND_JEV_ROUTER_ENDPOINT="https://provider.invalid/decisions",
        SERVICEMIND_JEV_ROUTER_REQUEST_MODEL="typesafe/jev-1.14",
        SERVICEMIND_JEV_ROUTER_SERVED_MODEL=SERVED_MODEL,
        SERVICEMIND_JEV_ROUTER_CONFIDENCE_FLOOR=0.9,
        SERVICEMIND_JEV_ROUTER_TIMEOUT_SECONDS=3.0,
        SERVICEMIND_JEV_ROUTER_MAX_PAYLOAD_BYTES=12_000,
        SERVICEMIND_JEV_ROUTER_CONTEXT_CEILING=262_144,
        SERVICEMIND_JEV_ROUTER_PRICE_USD_PER_MILLION_INPUT_TOKENS=0.042,
        SERVICEMIND_JEV_ROUTER_DAILY_BUDGET_USD=0.5,
        SERVICEMIND_JEV_ROUTER_PER_MINUTE_LIMIT=60,
        SERVICEMIND_JEV_ROUTER_CONCURRENCY_LIMIT=4,
        SERVICEMIND_JEV_ROUTER_CIRCUIT_FAILURES=5,
        SERVICEMIND_JEV_ROUTER_CIRCUIT_RESET_SECONDS=60.0,
        SERVICEMIND_JEV_ROUTER_LEDGER_DIR=".runtime/jev_router",
        SERVICEMIND_JEV_ROUTER_LEDGER_TIMEOUT_SECONDS=1.0,
        OPENROUTER_API_KEY=_Secret(),
    )


def test_from_settings_maps_the_environment() -> None:
    config = JevRouterConfig.from_settings(_settings(mode="active"))

    assert config is not None
    assert config.mode == "active"
    assert config.served_model == SERVED_MODEL
    assert config.api_key == "test-key"
    assert config.ledger_dir == Path(".runtime/jev_router")


@pytest.mark.asyncio
async def test_write_request_can_only_be_tightened(tmp_path: Path) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=decision_body("unsupported", confidence=0.99))

    tightened = await decide(router_for(tmp_path, handler), request_write=True, fallback=RULE_WRITE)

    assert tightened.decision.route is RouteType.UNSUPPORTED
    assert tightened.decision.required_capabilities == []
    assert tightened.decision.reason_code == "jev_model_refusal"
    assert tightened.source == SOURCE_OVERRIDE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route", ["simple_data_query", "simple_knowledge_query", "complex_workflow"]
)
async def test_write_request_is_never_downgraded_to_a_fast_path(tmp_path: Path, route: str) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=decision_body(route, confidence=0.99))

    outcome = await decide(router_for(tmp_path, handler), request_write=True, fallback=RULE_WRITE)

    assert outcome.decision == RULE_WRITE
    assert AgentName.ACTION in outcome.decision.required_capabilities
    assert outcome.source == SOURCE_RULE


@pytest.mark.asyncio
async def test_write_request_rule_refusal_is_never_relaxed(tmp_path: Path) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=decision_body("simple_knowledge_query"))

    outcome = await decide(router_for(tmp_path, handler), request_write=True, fallback=RULE_REFUSAL)

    assert outcome.decision == RULE_REFUSAL
    assert outcome.source == SOURCE_RULE


@pytest.mark.asyncio
async def test_non_write_refusal_is_sent_to_the_reviewed_pipeline(tmp_path: Path) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=decision_body("simple_knowledge_query", confidence=0.9))

    outcome = await decide(router_for(tmp_path, handler), fallback=RULE_REFUSAL)

    assert outcome.decision.route is RouteType.COMPLEX_WORKFLOW
    assert outcome.decision.required_capabilities == list(
        ROUTE_CAPABILITIES[RouteType.COMPLEX_WORKFLOW]
    )
    assert outcome.decision.reason_code == "jev_unsupported_override_reviewed"
    assert outcome.source == SOURCE_OVERRIDE


@pytest.mark.asyncio
async def test_non_write_confidence_below_floor_falls_back(tmp_path: Path) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=decision_body("simple_data_query", confidence=0.8999))

    outcome = await decide(router_for(tmp_path, handler))

    assert outcome.decision == RULE_KNOWLEDGE
    assert outcome.source == SOURCE_FALLBACK
    assert outcome.fallback_reason == "confidence_below_floor"


@pytest.mark.asyncio
async def test_confidence_floor_is_inclusive(tmp_path: Path) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=decision_body("simple_data_query", confidence=0.9))

    outcome = await decide(router_for(tmp_path, handler))

    assert outcome.decision.route is RouteType.SIMPLE_DATA_QUERY
    assert outcome.decision.required_capabilities == [AgentName.DATA]
    assert outcome.source == SOURCE_OVERRIDE
    assert outcome.decision.reason_code == "jev_model_route"


@pytest.mark.asyncio
async def test_same_route_is_confirmed_not_rebuilt(tmp_path: Path) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=decision_body("simple_knowledge_query"))

    outcome = await decide(router_for(tmp_path, handler))

    assert outcome.decision == RULE_KNOWLEDGE
    assert outcome.source == SOURCE_CONFIRMED


@pytest.mark.asyncio
async def test_non_write_model_refusal_is_honoured(tmp_path: Path) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=decision_body("unsupported", confidence=0.99))

    outcome = await decide(router_for(tmp_path, handler), fallback=RULE_COMPLEX)

    assert outcome.decision.route is RouteType.UNSUPPORTED
    assert outcome.decision.required_capabilities == []
    assert outcome.decision.reason_code == "jev_model_refusal"
    assert outcome.source == SOURCE_OVERRIDE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate, reason",
    [
        (lambda doc: doc.update(model="typesafe/jev-1.14"), "unexpected_served_model"),
        (
            lambda doc: doc["answers"]["route"].update(choice="drop_database"),
            "invalid_provider_response",
        ),
        (
            lambda doc: doc["answers"]["route"]["probabilities"].pop("unsupported"),
            "invalid_provider_response",
        ),
        (
            lambda doc: doc["answers"]["route"]["probabilities"].update(simple_data_query=0.5),
            "invalid_provider_response",
        ),
        (
            lambda doc: doc["answers"]["route"].update(confidence=1.5),
            "invalid_provider_response",
        ),
        (lambda doc: doc.pop("usage"), "usage_missing"),
    ],
)
async def test_malformed_responses_fall_back(tmp_path: Path, mutate, reason: str) -> None:
    document = decision_body()
    mutate(document)

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=document)

    outcome = await decide(router_for(tmp_path, handler))

    assert outcome.decision == RULE_KNOWLEDGE
    assert outcome.source == SOURCE_FALLBACK
    assert outcome.fallback_reason == reason


@pytest.mark.asyncio
async def test_billed_but_unusable_response_is_still_settled(tmp_path: Path) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=decision_body(model="typesafe/jev-1.15-20270101"))

    router = router_for(tmp_path, handler)
    await decide(router)

    snapshot = router._ledger_for(router._config).snapshot()
    assert snapshot.pending_usd == 0
    assert snapshot.settled_usd == Decimal("0.0000042")


@pytest.mark.asyncio
async def test_non_json_response_is_unknown_spend(tmp_path: Path) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json", headers={"content-type": "text/plain"})

    router = router_for(tmp_path, handler)
    outcome = await decide(router)

    assert outcome.source == SOURCE_FALLBACK
    assert outcome.fallback_reason == "invalid_provider_response"
    snapshot = router._ledger_for(router._config).snapshot()
    assert snapshot.pending_usd == 0
    assert snapshot.unknown_usd > 0


@pytest.mark.asyncio
async def test_http_error_is_unknown_spend_and_falls_back(tmp_path: Path) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    router = router_for(tmp_path, handler)
    outcome = await decide(router)

    assert outcome.decision == RULE_KNOWLEDGE
    assert outcome.fallback_reason == "http_503"
    assert outcome.http_status == 503
    snapshot = router._ledger_for(router._config).snapshot()
    assert snapshot.unknown_usd > 0
    assert snapshot.pending_usd == 0


@pytest.mark.asyncio
async def test_timeout_falls_back_and_keeps_the_reservation(tmp_path: Path) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.5)
        return httpx.Response(200, json=decision_body())

    router = router_for(tmp_path, handler, timeout_seconds=0.02)
    outcome = await decide(router)

    assert outcome.decision == RULE_KNOWLEDGE
    assert outcome.fallback_reason == "timeout"
    snapshot = router._ledger_for(router._config).snapshot()
    assert snapshot.unknown_usd > 0


@pytest.mark.asyncio
async def test_missing_api_key_never_calls_the_provider(tmp_path: Path) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be made without a key")

    outcome = await decide(router_for(tmp_path, handler, api_key=None))

    assert outcome.fallback_reason == "missing_api_key"
    assert outcome.source == SOURCE_FALLBACK


@pytest.mark.asyncio
async def test_budget_exhaustion_never_calls_the_provider(tmp_path: Path) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be made once the budget is spent")

    outcome = await decide(router_for(tmp_path, handler, daily_budget_usd=1e-9))

    assert outcome.fallback_reason == "budget_exhausted"
    assert outcome.source == SOURCE_FALLBACK


@pytest.mark.asyncio
async def test_oversized_payload_never_calls_the_provider(tmp_path: Path) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("oversized payload must be rejected locally")

    outcome = await decide(router_for(tmp_path, handler, max_payload_bytes=100), raw="x" * 500)

    assert outcome.fallback_reason == "payload_too_large"


@pytest.mark.asyncio
async def test_circuit_opens_and_half_opens_after_reset(tmp_path: Path) -> None:
    now = [0.0]
    calls: list[int] = []
    failing = [True]

    async def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        if failing[0]:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json=decision_body())

    guards = _RouteGuards(
        concurrency_limit=4,
        circuit_failures=2,
        circuit_reset_seconds=60.0,
        monotonic=lambda: now[0],
    )
    router = router_for(tmp_path, handler, guards=guards)

    first = await decide(router)
    second = await decide(router)
    refused = await decide(router)

    assert first.fallback_reason == "http_500"
    assert second.fallback_reason == "http_500"
    assert refused.fallback_reason == "circuit_open"
    assert len(calls) == 2

    now[0] = 61.0
    failing[0] = False
    recovered = await decide(router)
    assert recovered.source in {SOURCE_CONFIRMED, SOURCE_OVERRIDE}
    assert len(calls) == 3

    again = await decide(router)
    assert again.source in {SOURCE_CONFIRMED, SOURCE_OVERRIDE}
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_concurrency_bulkhead_sheds_instead_of_queueing(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(_: httpx.Request) -> httpx.Response:
        entered.set()
        await release.wait()
        return httpx.Response(200, json=decision_body())

    guards = _RouteGuards(
        concurrency_limit=1,
        circuit_failures=5,
        circuit_reset_seconds=60.0,
    )
    router = router_for(tmp_path, handler, guards=guards)

    first = asyncio.create_task(decide(router))
    await asyncio.wait_for(entered.wait(), timeout=1)
    shed = await decide(router)
    release.set()
    completed = await first

    assert shed.fallback_reason == "concurrency_limited"
    assert shed.source == SOURCE_FALLBACK
    assert completed.source in {SOURCE_CONFIRMED, SOURCE_OVERRIDE}


@pytest.mark.asyncio
async def test_shadow_mode_never_changes_the_route(tmp_path: Path) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=decision_body("simple_data_query"))

    outcome = await decide(router_for(tmp_path, handler, mode="shadow"))

    assert outcome.decision == RULE_KNOWLEDGE
    assert outcome.source == SOURCE_SHADOW
    assert outcome.jev_route is RouteType.SIMPLE_DATA_QUERY
    assert outcome.jev_confidence == 0.99


def test_event_payload_is_a_whitelist() -> None:
    outcome = JevRoutingOutcome(
        decision=RULE_KNOWLEDGE,
        source=SOURCE_CONFIRMED,
        jev_route=RouteType.SIMPLE_KNOWLEDGE_QUERY,
        jev_confidence=0.99,
        served_model=SERVED_MODEL,
        latency_ms=12.5,
        cost_usd=Decimal("0.0000042"),
        cost_source="provider",
        input_tokens=100,
        output_tokens=5,
        http_status=200,
        attempt_id="abc",
    )
    payload = route_completed_payload(
        served=RULE_KNOWLEDGE, rule=RULE_KNOWLEDGE, outcome=outcome, mode="active"
    )

    allowed = {
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
    assert set(payload) <= allowed
    assert payload["cost_usd"] == "0.0000042"


@pytest.mark.asyncio
async def test_logs_never_contain_the_request_or_the_key(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    caplog.set_level(logging.WARNING)
    await decide(router_for(tmp_path, handler), raw="DELETE-SENTINEL-REQUEST")

    assert "DELETE-SENTINEL-REQUEST" not in caplog.text
    assert "test-key" not in caplog.text
