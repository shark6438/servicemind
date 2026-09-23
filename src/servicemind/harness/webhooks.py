import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid5

from servicemind.domain.task import GOAL_MAX_LENGTH, TICKET_ID_MAX

#: The composed run goal for a delivery. The event name is interpolated into it, so this
#: template and the label bound below are two views of one thing.
_GOAL_TEMPLATE = "Analyze GLPI webhook event {event}"

#: The GLPI event name is a label -- "new", "update", "delete" -- not free text. Deriving
#: its ceiling from the goal contract rather than restating a number means the composed
#: goal cannot exceed ``TaskPlan.goal`` by construction; a copied constant could, and the
#: failure mode is a ``ValidationError`` raised deep inside plan compilation, long after
#: the offending byte was accepted here.
_MAX_EVENT_LENGTH = GOAL_MAX_LENGTH - len(_GOAL_TEMPLATE.format(event=""))


class WebhookValidationError(ValueError):
    pass


@dataclass(frozen=True)
class GlpiWebhookEvent:
    tenant_id: UUID
    ticket_id: int
    entity_id: int
    event: str


def glpi_webhook_goal(event: str) -> str:
    """Compose the run goal for one delivery, inside the shared goal contract."""
    return _GOAL_TEMPLATE.format(event=event)


def webhook_run_id(tenant_id: UUID, signature: str) -> UUID:
    """Map one signed delivery to one stable durable run across retries/restarts."""
    return uuid5(NAMESPACE_URL, f"servicemind:{tenant_id}:glpi-webhook:{signature}")


def parse_glpi_webhook(body: bytes) -> GlpiWebhookEvent:
    try:
        payload = json.loads(body)
        parsed = GlpiWebhookEvent(
            tenant_id=UUID(str(payload["tenant_id"])),
            ticket_id=int(payload["item"]["id"]),
            entity_id=int(payload["item"]["entity"]["id"]),
            event=str(payload.get("event", "updated")),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise WebhookValidationError("Invalid GLPI webhook payload") from exc
    # Checked after the parse guard, not inside it: a well-formed body carrying a value
    # outside the contract is a different failure from a body that could not be read, and
    # it is the one an operator has to act on. The guard above would have flattened this
    # to "Invalid GLPI webhook payload" along with every malformed-body case.
    if not parsed.event or len(parsed.event) > _MAX_EVENT_LENGTH:
        raise WebhookValidationError(
            f"GLPI webhook event must be 1..{_MAX_EVENT_LENGTH} characters"
        )
    # The same contract the ``POST /runs`` body is held to, for the same reason: this id
    # is written into a 32-bit column, and a body carrying one that does not fit is a
    # rejected delivery, not a database error four layers down.
    if not 1 <= parsed.ticket_id <= TICKET_ID_MAX:
        raise WebhookValidationError(f"GLPI webhook ticket id must be 1..{TICKET_ID_MAX}")
    return parsed


#: An HMAC-SHA256 digest is 32 bytes, so its hex form is exactly 64 characters. This is
#: the same shape ``ApprovalRequest.expected_action_hash`` already requires.
_SIGNATURE_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def verify_glpi_signature(
    *, body: bytes, timestamp: str, signature: str, secret: str, now: int | None = None
) -> None:
    # Both header values are checked for shape before they reach a comparison or an
    # encode, because neither operation fails softly on a str: ``hmac.compare_digest``
    # raises TypeError on non-ASCII and ``str.encode("ascii")`` raises UnicodeEncodeError.
    # Those escaped the endpoint's ``except WebhookValidationError``, so an unauthenticated
    # caller could turn any of them into a 500 -- and ``str.isdigit()`` alone would not
    # have caught the timestamp, since it accepts Arabic-Indic digits that ``int()`` reads
    # happily and that ASCII encoding then rejects.
    if not timestamp.isascii() or not timestamp.isdigit():
        raise WebhookValidationError("Invalid webhook timestamp")
    if _SIGNATURE_PATTERN.fullmatch(signature) is None:
        raise WebhookValidationError("Invalid webhook signature")
    timestamp_value = int(timestamp)
    if abs((now if now is not None else int(time.time())) - timestamp_value) > 300:
        raise WebhookValidationError("Expired webhook request")
    expected = hmac.new(
        secret.encode("utf-8"), body + timestamp.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise WebhookValidationError("Invalid webhook signature")
