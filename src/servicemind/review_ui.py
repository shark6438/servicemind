"""Typed, token-safe client for the human memory review console."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field


class ReviewItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_id: UUID
    memory_type: str
    subject_key: str
    content: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    version: int = Field(ge=1)
    status: str
    confidence: float = Field(ge=0, le=1)
    importance: float = Field(ge=0, le=1)
    evidence_refs: tuple[dict[str, Any], ...] = ()
    supporting_episode_ids: tuple[UUID, ...] = ()
    provenance: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ReviewQueuePage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[ReviewItem, ...]
    next_after_created_at: datetime | None = None
    next_after_memory_id: UUID | None = None


class MemoryReviewClientError(RuntimeError):
    """A sanitized API failure that never includes credentials or response headers."""

    def __init__(self, status_code: int | None, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        prefix = f"HTTP {status_code}: " if status_code is not None else ""
        super().__init__(f"{prefix}{detail}")


class MemoryReviewClient:
    """Call review endpoints with an OIDC access token kept out of logs and state."""

    def __init__(
        self,
        *,
        base_url: str,
        access_token: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._access_token = access_token
        self._transport = transport

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            with httpx.Client(
                base_url=self._base_url,
                headers={"Authorization": f"Bearer {self._access_token}"},
                timeout=10,
                trust_env=False,
                transport=self._transport,
            ) as client:
                response = client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise MemoryReviewClientError(None, "Review service is unavailable") from exc
        if response.is_error:
            detail = "Review request failed"
            try:
                payload = response.json()
                if isinstance(payload, dict) and isinstance(payload.get("detail"), str):
                    # A broken upstream must not be able to reflect the bearer
                    # credential into Streamlit state, logs, or an exception.
                    detail = payload["detail"].replace(self._access_token, "[REDACTED]")
            except ValueError:
                pass
            raise MemoryReviewClientError(response.status_code, detail)
        return response

    def list_queue(
        self,
        *,
        memory_type: str | None = "procedural",
        limit: int = 50,
        after_created_at: datetime | None = None,
        after_memory_id: UUID | None = None,
    ) -> ReviewQueuePage:
        if (after_created_at is None) != (after_memory_id is None):
            raise ValueError("review cursor requires both values")
        params: dict[str, str | int] = {"limit": limit}
        if memory_type:
            params["memory_type"] = memory_type
        if after_created_at is not None and after_memory_id is not None:
            params["after_created_at"] = after_created_at.isoformat()
            params["after_memory_id"] = str(after_memory_id)
        response = self._request("GET", "/v1/servicemind/memories/review-queue", params=params)
        return ReviewQueuePage.model_validate(response.json())

    def decide(
        self,
        item: ReviewItem,
        *,
        decision: Literal["activate", "reject"],
        review_ref: str,
        comment: str,
    ) -> ReviewItem:
        response = self._request(
            "POST",
            f"/v1/servicemind/memories/{item.memory_id}/review",
            json={
                "decision": decision,
                "expected_version": item.version,
                "expected_content_hash": item.content_hash,
                "review_ref": review_ref,
                "comment": comment,
            },
        )
        return ReviewItem.model_validate(response.json())
