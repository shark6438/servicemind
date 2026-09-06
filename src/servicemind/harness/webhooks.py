import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid5


class WebhookValidationError(ValueError):
    pass


@dataclass(frozen=True)
class GlpiWebhookEvent:
    tenant_id: UUID
    ticket_id: int
    entity_id: int
    event: str


def webhook_run_id(tenant_id: UUID, signature: str) -> UUID:
    """Map one signed delivery to one stable durable run across retries/restarts."""
    return uuid5(NAMESPACE_URL, f"servicemind:{tenant_id}:glpi-webhook:{signature}")


def parse_glpi_webhook(body: bytes) -> GlpiWebhookEvent:
    try:
        payload = json.loads(body)
        return GlpiWebhookEvent(
            tenant_id=UUID(str(payload["tenant_id"])),
            ticket_id=int(payload["item"]["id"]),
            entity_id=int(payload["item"]["entity"]["id"]),
            event=str(payload.get("event", "updated")),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise WebhookValidationError("Invalid GLPI webhook payload") from exc


def verify_glpi_signature(
    *, body: bytes, timestamp: str, signature: str, secret: str, now: int | None = None
) -> None:
    try:
        timestamp_value = int(timestamp)
    except ValueError as exc:
        raise WebhookValidationError("Invalid webhook timestamp") from exc
    if abs((now if now is not None else int(time.time())) - timestamp_value) > 300:
        raise WebhookValidationError("Expired webhook request")
    expected = hmac.new(
        secret.encode("utf-8"), body + timestamp.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise WebhookValidationError("Invalid webhook signature")
