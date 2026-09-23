import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from servicemind.domain.task import (
    GOAL_MAX_LENGTH,
    TICKET_ID_MAX,
    AgentName,
    Budget,
    ErrorPolicy,
    Task,
    TaskPlan,
)
from servicemind.harness import webhooks
from servicemind.harness.webhooks import (
    WebhookValidationError,
    glpi_webhook_goal,
    parse_glpi_webhook,
    verify_glpi_signature,
    webhook_run_id,
)

BODY = (
    b'{"tenant_id":"11111111-1111-4111-8111-111111111111",'
    b'"event":"new","item":{"id":5,"entity":{"id":1}}}'
)


def plan_with_goal(goal: str) -> TaskPlan:
    """A minimal legal plan, so a goal can be tested against the real field contract."""
    due = datetime.now(UTC) + timedelta(minutes=5)
    budget = Budget(deadline=due)
    return TaskPlan(
        goal=goal,
        tasks=[
            Task(
                task_id="T1",
                agent=AgentName.DATA,
                task_type="get_ticket",
                input={"objective": "Read ticket", "ticket_id": 5},
                error_policy=ErrorPolicy.RETRY,
                deadline=due,
            )
        ],
        max_parallel=2,
        max_steps=budget.max_steps,
        max_replans=budget.max_replans,
        deadline=due,
        budget=budget,
    )


def signature(secret: str, timestamp: str) -> str:
    return hmac.new(secret.encode(), BODY + timestamp.encode("ascii"), hashlib.sha256).hexdigest()


def test_parse_glpi_webhook() -> None:
    event = parse_glpi_webhook(BODY)
    assert event.ticket_id == 5
    assert event.entity_id == 1
    assert event.event == "new"


def test_signature_is_body_timestamp_and_secret_bound() -> None:
    timestamp = "1000"
    verify_glpi_signature(
        body=BODY,
        timestamp=timestamp,
        signature=signature("secret", timestamp),
        secret="secret",
        now=1000,
    )
    with pytest.raises(WebhookValidationError, match="signature"):
        verify_glpi_signature(
            body=BODY,
            timestamp=timestamp,
            signature="0" * 64,
            secret="secret",
            now=1000,
        )


@pytest.mark.parametrize(
    ("timestamp", "signature", "reason"),
    [
        ("١٢", "0" * 64, "Arabic-Indic digits pass int() and isdigit() but not ASCII encode"),
        ("1000", "é" * 64, "non-ASCII makes hmac.compare_digest raise TypeError"),
        ("1000", "a" * 63, "a short digest can never be the expected one"),
        ("1000", "A" * 64, "hexdigest() is lowercase, so uppercase cannot match either"),
        ("1000", "", "an absent signature is not a signature"),
    ],
)
def test_a_malformed_signature_header_is_a_clean_rejection_not_a_500(
    timestamp: str, signature: str, reason: str
) -> None:
    """Nothing here may escape as anything but ``WebhookValidationError``.

    The endpoint catches ``WebhookValidationError`` alone, and these headers are
    caller-controlled and read before authentication, so anything else is an
    unauthenticated 500. ``hmac.compare_digest`` raises ``TypeError`` on non-ASCII str and
    ``str.encode("ascii")`` raises ``UnicodeEncodeError`` -- both reachable from the wire.
    """
    with pytest.raises(WebhookValidationError, match="timestamp|signature"):
        verify_glpi_signature(
            body=BODY,
            timestamp=timestamp,
            signature=signature,
            secret="secret",
            now=int(timestamp) if timestamp.isascii() else 12,
        )


def test_expired_webhook_is_rejected() -> None:
    with pytest.raises(WebhookValidationError, match="Expired"):
        verify_glpi_signature(
            body=BODY,
            timestamp="1000",
            signature=signature("secret", "1000"),
            secret="secret",
            now=1301,
        )


def test_an_over_long_event_is_rejected_before_it_reaches_the_run_goal() -> None:
    """The event name is a GLPI label, and the goal contract applies on this path too.

    ``CreateRunRequest.goal`` caps a goal at 2000 characters, but the webhook path never
    passed through it: the event was interpolated into ``Analyze GLPI webhook event
    {event}`` unmodified. A 5000-character event therefore produced a 5027-character goal
    that was accepted, persisted, and then rejected by ``TaskPlan`` during plan
    compilation -- a 502 on a delivery GLPI had signed, with the run already claimed.
    """
    body = json.dumps(
        {
            "tenant_id": "11111111-1111-4111-8111-111111111111",
            "event": "x" * (GOAL_MAX_LENGTH + 1),
            "item": {"id": 5, "entity": {"id": 1}},
        }
    ).encode()

    with pytest.raises(WebhookValidationError, match="event must be"):
        parse_glpi_webhook(body)


def test_a_ticket_id_the_run_table_cannot_hold_is_rejected_at_the_delivery() -> None:
    """The ticket id on this path is an integer read out of an externally POSTed body.

    ``agent_runs.ticket_id`` is a 32-bit integer, so a delivery carrying a larger one
    was accepted, signed, and claimed -- and only then failed in the driver, four layers
    below the boundary that could have named it.
    """
    body = json.dumps(
        {
            "tenant_id": "11111111-1111-4111-8111-111111111111",
            "event": "new",
            "item": {"id": TICKET_ID_MAX + 1, "entity": {"id": 1}},
        }
    ).encode()

    with pytest.raises(WebhookValidationError, match="ticket id must be"):
        parse_glpi_webhook(body)


def test_the_largest_ticket_id_the_run_table_holds_is_still_deliverable() -> None:
    """The bound rejects only what it has to; the ceiling itself stays legal."""
    body = json.dumps(
        {
            "tenant_id": "11111111-1111-4111-8111-111111111111",
            "event": "new",
            "item": {"id": TICKET_ID_MAX, "entity": {"id": 1}},
        }
    ).encode()

    assert parse_glpi_webhook(body).ticket_id == TICKET_ID_MAX


def test_every_admissible_event_composes_into_a_goal_the_plan_contract_accepts() -> None:
    """The label bound is derived from the goal contract, not restated beside it.

    ``_MAX_EVENT_LENGTH`` is defined as exactly the room the template leaves inside
    ``GOAL_MAX_LENGTH``, so the longest event the parser admits still composes into a goal
    ``TaskPlan`` accepts. Asserting the arithmetic is what stops a later edit from
    changing one of the two numbers and silently reopening the gap -- a copy of a bound
    is not a bound.
    """
    longest = "y" * webhooks._MAX_EVENT_LENGTH
    parsed = parse_glpi_webhook(
        json.dumps(
            {
                "tenant_id": "11111111-1111-4111-8111-111111111111",
                "event": longest,
                "item": {"id": 5, "entity": {"id": 1}},
            }
        ).encode()
    )

    goal = glpi_webhook_goal(parsed.event)

    assert len(goal) == GOAL_MAX_LENGTH, "the label bound must use the contract exactly"
    assert plan_with_goal(goal).goal == goal
    with pytest.raises(ValidationError, match="goal"):
        plan_with_goal(goal + "y")


def test_webhook_run_id_is_deterministic_and_tenant_scoped() -> None:
    acme = parse_glpi_webhook(BODY).tenant_id
    globex = type(acme)("22222222-2222-4222-8222-222222222222")
    assert webhook_run_id(acme, "abc") == webhook_run_id(acme, "abc")
    assert webhook_run_id(acme, "abc") != webhook_run_id(acme, "def")
    assert webhook_run_id(acme, "abc") != webhook_run_id(globex, "abc")
