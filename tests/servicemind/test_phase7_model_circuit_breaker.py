"""What the governed model gateway's provider breaker does to runs that did not cause it.

The breaker exists so a failing provider is not hammered: after five consecutive failures
on one provider the process stops calling it for thirty seconds. Measured on the
2026-09-24 load batch, the tenth tier rested forty-one of sixty runs at ``failed``, in
whole repeats of twenty at a time, each within about 1.1 seconds -- a shape no retry
schedule produces, because a retry schedule spends its budget over seconds. A shared
counter that flips every concurrent run from "working" to "refused" at once produces
exactly that shape. These tests hold the mechanism still so the claim can be checked
rather than argued:

* a burst of failures on one tenant's runs must not refuse a *different* tenant's call
  once the provider is answering again;
* an answer of the wrong shape is the provider working, so it must not be counted as the
  provider being down;
* a caller that cannot afford the remaining open window must still be refused rather than
  spending its whole budget waiting, which is what keeps the breaker protecting anything.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from pydantic import BaseModel

from servicemind.model_gateway.contracts import ModelCallContext, ModelPurpose, ModelRisk
from servicemind.model_gateway.gateway import ModelGateway
from servicemind.model_gateway.policy import ModelRoutePolicy
from servicemind.model_gateway.repository import InMemoryModelAuditSink

TENANT_A = UUID("11111111-1111-4111-8111-111111111111")
TENANT_B = UUID("22222222-2222-4222-8222-222222222222")


class GatewayResult(BaseModel):
    value: int


class ProviderOutage(RuntimeError):
    """A provider-side failure: named without any retry marker, so it is not retried."""


def _context(tenant_id: UUID = TENANT_A, *, timeout_seconds: float = 30.0) -> ModelCallContext:
    return ModelCallContext(
        tenant_id=tenant_id,
        agent_role="analysis",
        purpose=ModelPurpose.ANALYSIS,
        risk=ModelRisk.LOW,
        policy_version="policy-v1",
        prompt_version="prompt-v1",
        cache_allowed=False,
        timeout_seconds=timeout_seconds,
    )


class _Runnable:
    def __init__(self, behaviour) -> None:
        self.behaviour = behaviour

    async def ainvoke(self, messages: object, config: object = None, **kwargs: object) -> object:
        del messages, config, kwargs
        return self.behaviour()


class _Model:
    """A stub chat model, in the shape the gateway's ``_structured`` wrapper expects."""

    model_name = "stub-v1"
    model_revision = "sha256:test"

    def __init__(self, behaviour) -> None:
        self.behaviour = behaviour

    def with_structured_output(self, schema: type[BaseModel], **kwargs: object) -> _Runnable:
        del schema, kwargs
        return _Runnable(self.behaviour)


def _gateway(**kwargs: object) -> ModelGateway:
    return ModelGateway(
        policy=ModelRoutePolicy(allowed_providers=("unknown",)),
        audit_sink=InMemoryModelAuditSink(),
        max_retries=0,
        **kwargs,
    )


def _outage() -> object:
    raise ProviderOutage("provider is down")


def _answer(value: int):
    return lambda: {"value": value}


async def _open_the_breaker(gateway: ModelGateway, *, times: int = 5) -> None:
    for _ in range(times):
        with pytest.raises(ProviderOutage):
            await gateway.invoke(_Model(_outage), GatewayResult, "hi", context=_context())


@pytest.mark.asyncio
async def test_a_provider_burst_does_not_refuse_a_concurrent_run_on_another_tenant() -> None:
    gateway = _gateway(circuit_open_seconds=0.05)
    await _open_the_breaker(gateway)
    result = await gateway.invoke(
        _Model(_answer(7)), GatewayResult, "hi", context=_context(TENANT_B)
    )
    assert result.value == 7


@pytest.mark.asyncio
async def test_an_answer_of_the_wrong_shape_is_not_read_as_the_provider_being_down() -> None:
    """The provider answered; only the shape was wrong. That is not a provider outage."""
    gateway = _gateway(circuit_open_seconds=0.05)
    for _ in range(5):
        with pytest.raises(Exception):
            await gateway.invoke(
                _Model(lambda: "not-a-dict"), GatewayResult, "hi", context=_context()
            )
    result = await gateway.invoke(
        _Model(_answer(3)), GatewayResult, "hi", context=_context(TENANT_B)
    )
    assert result.value == 3


@pytest.mark.asyncio
async def test_a_caller_that_cannot_afford_the_open_window_is_still_refused() -> None:
    gateway = _gateway(circuit_open_seconds=5.0)
    await _open_the_breaker(gateway)
    with pytest.raises(Exception) as caught:
        await gateway.invoke(
            _Model(_answer(1)),
            GatewayResult,
            "hi",
            context=_context(timeout_seconds=0.05),
        )
    assert not isinstance(caught.value, AssertionError)


@pytest.mark.asyncio
async def test_the_wait_for_the_window_does_not_outlast_the_callers_budget() -> None:
    """A wait bounded only by the window would let one call spend more than it was given."""
    gateway = _gateway(circuit_open_seconds=5.0)
    await _open_the_breaker(gateway)
    started = asyncio.get_running_loop().time()
    with pytest.raises(Exception):
        await gateway.invoke(
            _Model(_answer(1)), GatewayResult, "hi", context=_context(timeout_seconds=0.1)
        )
    assert asyncio.get_running_loop().time() - started < 1.0
