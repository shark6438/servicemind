from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AgentRunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    DEGRADED = "degraded"
    FAILED = "failed"


class AgentInvocationContext(BaseModel):
    """Immutable, credential-free context shared by every ServiceMind sub-agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    tenant_id: UUID
    user_id: str = Field(min_length=1, max_length=255)
    task_id: str = Field(pattern=r"^T[1-9][0-9]*$|^FAST-[A-Z]+$")
    trace_id: str = Field(min_length=1, max_length=255)
    deadline: datetime
    allowed_capabilities: frozenset[str] = Field(default_factory=frozenset)
    max_model_calls: int = Field(default=2, ge=0, le=10)
    max_tool_calls: int = Field(default=6, ge=0, le=20)
    prompt_version: str = Field(default="2026-09-05", min_length=1, max_length=50)
    policy_version: str = Field(default="servicemind-agent-policy-v1", min_length=1)

    @model_validator(mode="after")
    def require_aware_future_deadline(self) -> AgentInvocationContext:
        if self.deadline.tzinfo is None:
            raise ValueError("Agent invocation deadline must be timezone-aware")
        return self

    def require_capability(self, capability: str) -> None:
        if capability not in self.allowed_capabilities:
            raise PermissionError(f"Agent capability is not allowed: {capability}")

    def ensure_active(self) -> None:
        if datetime.now(UTC) >= self.deadline:
            raise TimeoutError("Agent invocation deadline has expired")


class AgentRunMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    attempts: int = Field(default=1, ge=1)
    latency_ms: float = Field(default=0, ge=0)


class ToolInvocationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str = Field(min_length=1, max_length=160)
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: str = Field(min_length=1, max_length=40)
    attempts: int = Field(ge=1, le=10)
    result_ref: str | None = Field(default=None, max_length=500)


class AgentResultEnvelope[OutputT](BaseModel):
    """Typed result boundary used between the Supervisor and a sub-agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_name: str = Field(min_length=1, max_length=100)
    task_id: str = Field(min_length=1, max_length=100)
    status: AgentRunStatus
    output: OutputT
    evidence_refs: list[str] = Field(default_factory=list)
    metrics: AgentRunMetrics = Field(default_factory=AgentRunMetrics)
    model_name: str | None = Field(default=None, max_length=200)
    prompt_version: str = Field(min_length=1, max_length=50)
    policy_version: str = Field(min_length=1, max_length=100)
    failure_code: str | None = Field(default=None, max_length=100)
    failure_detail: str | None = Field(default=None, max_length=1000)
    tool_invocations: list[ToolInvocationRecord] = Field(default_factory=list)


def stable_digest(value: Any) -> str:
    """Return a deterministic digest for a Pydantic model or JSON-compatible value."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
