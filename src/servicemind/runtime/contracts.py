from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from servicemind.domain.integrity import stable_digest as stable_digest
from servicemind.domain.invocation import AgentInvocationContext as AgentInvocationContext


class AgentRunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    DEGRADED = "degraded"
    FAILED = "failed"


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
