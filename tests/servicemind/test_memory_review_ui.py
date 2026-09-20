from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest

from servicemind.review_ui import MemoryReviewClient, MemoryReviewClientError, ReviewItem

MEMORY_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
TOKEN = "private-access-token"


def _item(**updates):
    payload = {
        "memory_id": str(MEMORY_ID),
        "memory_type": "procedural",
        "subject_key": "procedure:vpn",
        "content": "Verify two incidents before applying the VPN recovery.",
        "content_hash": "a" * 64,
        "version": 3,
        "status": "quarantine",
        "confidence": 0.95,
        "importance": 0.9,
        "evidence_refs": [],
        "supporting_episode_ids": [],
        "provenance": {},
        "created_at": datetime(2026, 9, 16, tzinfo=UTC).isoformat(),
    }
    payload.update(updates)
    return payload


def test_queue_uses_bearer_token_and_complete_cursor() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert request.url.params["memory_type"] == "procedural"
        assert request.url.params["after_memory_id"] == str(MEMORY_ID)
        assert request.url.params["after_created_at"].startswith("2026-09-16T00:00:00")
        return httpx.Response(200, json={"items": [_item()]})

    client = MemoryReviewClient(
        base_url="http://review.test",
        access_token=TOKEN,
        transport=httpx.MockTransport(handler),
    )
    page = client.list_queue(
        after_created_at=datetime(2026, 9, 16, tzinfo=UTC), after_memory_id=MEMORY_ID
    )
    assert page.items[0].memory_id == MEMORY_ID


def test_decision_binds_the_loaded_snapshot() -> None:
    item = ReviewItem.model_validate(_item())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(f"/{MEMORY_ID}/review")
        assert json.loads(request.read()) == {
            "decision": "activate",
            "expected_version": 3,
            "expected_content_hash": "a" * 64,
            "review_ref": "CAB-42",
            "comment": "Evidence verified",
        }
        return httpx.Response(200, json=_item(status="active"))

    client = MemoryReviewClient(
        base_url="http://review.test",
        access_token=TOKEN,
        transport=httpx.MockTransport(handler),
    )
    updated = client.decide(
        item, decision="activate", review_ref="CAB-42", comment="Evidence verified"
    )
    assert updated.status == "active"


def test_errors_never_expose_the_access_token() -> None:
    client = MemoryReviewClient(
        base_url="http://review.test",
        access_token=TOKEN,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                409, json={"detail": f"snapshot changed; reflected={TOKEN}"}
            )
        ),
    )
    with pytest.raises(MemoryReviewClientError) as caught:
        client.list_queue()
    assert str(caught.value) == "HTTP 409: snapshot changed; reflected=[REDACTED]"
    assert TOKEN not in str(caught.value)


def test_partial_cursor_is_rejected_before_any_request() -> None:
    client = MemoryReviewClient(base_url="http://review.test", access_token=TOKEN)
    with pytest.raises(ValueError, match="requires both"):
        client.list_queue(after_created_at=datetime.now(UTC))
