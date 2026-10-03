"""The read half of the outbox stream, against a real Redis.

The relay that *publishes* has a deployment unit and runs. ``RedisStreamConsumer`` -- the
class that reads what the relay published -- appeared exactly once in this repository, at
its own definition. A class nothing constructs is a class nothing checks, and an audit
reading the delivery path found the feed filling up with no reader on the other end.

It was right to look, because the class was wrong where it mattered. ``reclaim``, the
call that recovers messages a crashed consumer claimed and never acknowledged, took no
handler: it moved those messages into the live consumer's own pending list and returned
their count. Nothing handled them, nothing acknowledged them, and the caller received a
number that read like deliveries. At-least-once had quietly become at-most-once for
exactly the messages a crash had already put at risk.

These tests drive the consumer against the Redis the deployment uses. They pin the two
halves of the guarantee separately: a live message is handled and then acknowledged, and
a message stranded by a consumer that never came back is handed to a survivor.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from redis.asyncio import Redis

from core import settings
from servicemind.persistence.models import ToolOutboxRecord
from servicemind.reliability.outbox import RedisStreamConsumer, RedisStreamPublisher

pytestmark = [pytest.mark.asyncio, pytest.mark.docker, pytest.mark.redis]

TENANT = UUID("11111111-1111-4111-8111-111111111111")


async def _record(sink: list[dict[str, str]], fields: dict[str, str]) -> None:
    sink.append(fields)


async def _unreachable(_: dict[str, str]) -> None:  # pragma: no cover - never called
    raise AssertionError("recovery handed over a message that was not idle")


def _client() -> Redis:
    url = settings.SERVICEMIND_REDIS_URL
    if url is None:
        # Reached only when the run did not ask for docker tests; under ``--run-docker``
        # conftest turns this skip into a failure, which is the point -- the run claimed
        # a Redis it does not have.
        pytest.skip("SERVICEMIND_REDIS_URL is not configured; no stream to consume")
    return Redis.from_url(
        url.get_secret_value(), decode_responses=False, socket_connect_timeout=2, socket_timeout=2
    )


def _event(**overrides: object) -> ToolOutboxRecord:
    payload: dict[str, object] = {
        "id": uuid4(),
        "tenant_id": TENANT,
        "event_type": "action.approved",
        "aggregate_type": "ActionIntent",
        "aggregate_id": "intent-consumer-probe",
        "idempotency_key": "key-consumer-probe",
        "payload": {"run_id": "run-1", "action_hash": "hash-1", "action_type": "glpi.followup"},
    }
    payload.update(overrides)
    return ToolOutboxRecord(**payload)  # type: ignore[arg-type]


async def test_a_published_event_is_handled_and_then_acknowledged() -> None:
    """The ordinary path, end to end: what the relay writes is what a handler receives.

    The assertions are ordered the way the guarantee is: the handler saw the event, and
    only then is there nothing left pending. A consumer that acknowledged on receipt
    would satisfy the second assertion while the first told the real story, so both are
    checked, and the handler records the order it was called in.
    """
    client = _client()
    stream = f"servicemind:test-consume-{uuid4().hex[:12]}"
    try:
        event = _event()
        await RedisStreamPublisher(client, stream=stream).publish(event)

        consumer = RedisStreamConsumer(
            client, stream=stream, group="integration", consumer="worker-1"
        )
        seen: list[dict[str, str]] = []
        handled = await consumer.once(lambda fields: _record(seen, fields))
        assert handled == 1

        assert [entry["event_id"] for entry in seen] == [str(event.id)]
        assert seen[0]["event_type"] == "action.approved"
        assert seen[0]["aggregate_id"] == "intent-consumer-probe"
        pending = await client.xpending(stream, "integration")
        assert pending["pending"] == 0, "the message was handled but left pending"
    finally:
        await client.delete(stream)
        await client.aclose()


async def test_a_message_stranded_by_a_dead_consumer_is_reclaimed_and_handled() -> None:
    """At-least-once, measured on the case that makes the phrase mean anything.

    ``worker-1`` reads the event and dies before acknowledging -- reproduced here by
    reading as that consumer and simply not acking, which is exactly the state a crash
    leaves. ``worker-2`` then recovers it. Against the previous implementation this
    returned 1 without calling the handler: a delivery count for a message that was
    delivered to no one.
    """
    client = _client()
    stream = f"servicemind:test-reclaim-{uuid4().hex[:12]}"
    try:
        event = _event()
        await RedisStreamPublisher(client, stream=stream).publish(event)

        doomed = RedisStreamConsumer(
            client, stream=stream, group="integration", consumer="worker-1"
        )
        await doomed.ensure_group()
        read = await client.xreadgroup("integration", "worker-1", {stream: ">"}, count=10)
        assert len(read[0][1]) == 1, "the doomed consumer did not take the message"

        survivor = RedisStreamConsumer(
            client, stream=stream, group="integration", consumer="worker-2"
        )
        # The same call with the production idle threshold: the message is seconds old,
        # so it belongs to worker-1 until worker-1 is actually gone. Recovery that took
        # in-flight work would deliver every message twice under normal operation, which
        # is why "at-least-once" needs handlers that are idempotent rather than a
        # recovery that never waits.
        assert await survivor.reclaim(_unreachable, min_idle_ms=30_000) == 0, (
            "recovery took a message a live consumer had just read"
        )

        seen: list[dict[str, str]] = []
        reclaimed = await survivor.reclaim(
            lambda fields: _record(seen, fields), min_idle_ms=0, count=10
        )
        assert reclaimed == 1, "the stranded message was not handed to a handler"
        assert [entry["event_id"] for entry in seen] == [str(event.id)]

        pending = await client.xpending(stream, "integration")
        assert pending["pending"] == 0, "the recovered message was left pending"
    finally:
        await client.delete(stream)
        await client.aclose()


async def test_a_failing_handler_leaves_its_message_pending_for_retry() -> None:
    """The other half of at-least-once: a handler that raises has not consumed anything.

    Acknowledging in a ``finally`` would discard the event on the very path where it
    still needs to be delivered, and the outbox row it came from already reads
    ``published`` -- so the loss would be invisible from the database.
    """
    client = _client()
    stream = f"servicemind:test-retry-{uuid4().hex[:12]}"
    try:
        await RedisStreamPublisher(client, stream=stream).publish(_event())
        consumer = RedisStreamConsumer(
            client, stream=stream, group="integration", consumer="worker-1"
        )

        async def explode(_: dict[str, str]) -> None:
            raise RuntimeError("handler failed")

        with pytest.raises(RuntimeError):
            await consumer.once(explode)

        pending = await client.xpending(stream, "integration")
        assert pending["pending"] == 1, "a failed handler consumed the message anyway"
    finally:
        await client.delete(stream)
        await client.aclose()
