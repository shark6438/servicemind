from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator


class ContextAgent(StrEnum):
    DATA = "data"
    KNOWLEDGE = "knowledge"
    ANALYSIS = "analysis"
    REVIEWER = "reviewer"
    ACTION = "action"


class ContextSource(StrEnum):
    TASK = "task"
    STATE = "state"
    EVIDENCE = "evidence"
    MEMORY = "memory"
    SKILL = "skill"
    TOOL_SCHEMA = "tool_schema"
    POLICY = "policy"
    OUTPUT_SCHEMA = "output_schema"


class TrustLabel(StrEnum):
    TRUSTED_CONTROL = "trusted_control"
    VERIFIED = "verified"
    UNTRUSTED = "untrusted"


class ContextItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: str = Field(min_length=1, max_length=255)
    source: ContextSource
    content: str = Field(min_length=1, max_length=100_000)
    allowed_agents: frozenset[ContextAgent]
    trust: TrustLabel
    authority: float = Field(ge=0, le=1)
    relevance: float = Field(ge=0, le=1)
    required: bool = False
    provenance_ref: str = Field(min_length=1, max_length=1000)
    taint_labels: frozenset[str] = Field(default_factory=frozenset)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    external_ref: str | None = Field(default=None, max_length=1000)

    @computed_field
    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode()).hexdigest()


class ContextBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_input_tokens: int = Field(ge=256, le=200_000)
    system_reserve: int = Field(default=256, ge=0)
    output_reserve: int = Field(default=1024, ge=0)
    tokens_used: int = Field(default=0, ge=0)
    tokens_pruned: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def valid_reserves(self) -> ContextBudget:
        if self.system_reserve + self.output_reserve >= self.max_input_tokens:
            raise ValueError("context reserves leave no usable input budget")
        if self.tokens_used + self.system_reserve + self.output_reserve > self.max_input_tokens:
            raise ValueError("context envelope exceeds the declared token budget")
        return self

    @property
    def usable_tokens(self) -> int:
        return self.max_input_tokens - self.system_reserve - self.output_reserve


class ContextSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: str
    source: ContextSource
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    tokens: int = Field(ge=0)
    decision: str = Field(pattern=r"^(selected|pruned|rejected)$")
    reason: str = Field(min_length=1, max_length=500)


class ContextEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: UUID
    run_id: UUID
    task_id: str = Field(min_length=1, max_length=100)
    agent: ContextAgent
    contract_version: str = "context-v1"
    items: tuple[ContextItem, ...]
    budget: ContextBudget
    selection_manifest: tuple[ContextSelection, ...]
    redaction_count: int = Field(default=0, ge=0)
    built_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @computed_field
    @property
    def input_hash(self) -> str:
        body: dict[str, Any] = {
            "tenant_id": str(self.tenant_id),
            "run_id": str(self.run_id),
            "task_id": self.task_id,
            "agent": self.agent.value,
            "contract_version": self.contract_version,
            "items": [(item.item_id, item.content_hash) for item in self.items],
        }
        return hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def model_payload(self) -> list[dict[str, Any]]:
        """Return only selected, already-redacted items with their trust boundary."""
        return [
            {
                "item_id": item.item_id,
                "source": item.source.value,
                "content": item.content,
                "trust": item.trust.value,
                "taint_labels": sorted(item.taint_labels),
                "provenance_ref": item.provenance_ref,
            }
            for item in self.items
        ]
