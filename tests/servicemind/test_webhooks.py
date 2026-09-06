import hashlib
import hmac

import pytest

from servicemind.harness.webhooks import (
    WebhookValidationError,
    parse_glpi_webhook,
    verify_glpi_signature,
    webhook_run_id,
)

BODY = (
    b'{"tenant_id":"11111111-1111-4111-8111-111111111111",'
    b'"event":"new","item":{"id":5,"entity":{"id":1}}}'
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


def test_expired_webhook_is_rejected() -> None:
    with pytest.raises(WebhookValidationError, match="Expired"):
        verify_glpi_signature(
            body=BODY,
            timestamp="1000",
            signature=signature("secret", "1000"),
            secret="secret",
            now=1301,
        )


def test_webhook_run_id_is_deterministic_and_tenant_scoped() -> None:
    acme = parse_glpi_webhook(BODY).tenant_id
    globex = type(acme)("22222222-2222-4222-8222-222222222222")
    assert webhook_run_id(acme, "abc") == webhook_run_id(acme, "abc")
    assert webhook_run_id(acme, "abc") != webhook_run_id(acme, "def")
    assert webhook_run_id(acme, "abc") != webhook_run_id(globex, "abc")
