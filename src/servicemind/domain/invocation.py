"""Credential-free invocation identity shared across agent boundaries."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AgentInvocationContext(BaseModel):
    """Immutable context passed to agents, model calls, and governed tools."""

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
