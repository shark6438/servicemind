from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from servicemind.runtime.contracts import AgentInvocationContext


class ModelPurpose(StrEnum):
    CONTROL = "control"
    PLANNING = "planning"
    DATA_PLANNING = "data_planning"
    RETRIEVAL_REWRITE = "retrieval_rewrite"
    ANALYSIS = "analysis"
    REVIEW = "review"
    MEMORY_EXTRACTION = "memory_extraction"
    CONTEXT_COMPRESSION = "context_compression"


class ModelRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ModelCallContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: UUID
    run_id: UUID | None = None
    task_id: str | None = Field(default=None, max_length=100)
    agent_role: str = Field(min_length=1, max_length=80)
    purpose: ModelPurpose
    risk: ModelRisk = ModelRisk.MEDIUM
    required_capabilities: frozenset[str] = Field(default_factory=frozenset)
    policy_version: str = Field(min_length=1, max_length=100)
    prompt_version: str = Field(min_length=1, max_length=100)
    timeout_seconds: float = Field(default=30, gt=0, le=180)
    max_cost_usd: float | None = Field(default=None, gt=0)
    cache_allowed: bool = False
    personal_or_volatile: bool = False
    memory_generation: str = "none"
    rag_index_generation: str = "none"
    tool_schema_version: str = "none"
    context_builder_version: str = "context-v1"

    @classmethod
    def from_invocation(
        cls,
        invocation: AgentInvocationContext,
        *,
        agent_role: str,
        purpose: ModelPurpose,
        risk: ModelRisk = ModelRisk.MEDIUM,
        cache_allowed: bool = False,
    ) -> ModelCallContext:
        from core import settings

        remaining = (invocation.deadline - datetime.now(UTC)).total_seconds()
        return cls(
            tenant_id=invocation.tenant_id,
            run_id=invocation.run_id,
            task_id=invocation.task_id,
            agent_role=agent_role,
            purpose=purpose,
            risk=risk,
            required_capabilities=invocation.allowed_capabilities,
            policy_version=invocation.policy_version,
            prompt_version=invocation.prompt_version,
            timeout_seconds=max(
                0.1,
                min(settings.SERVICEMIND_MODEL_TIMEOUT_SECONDS, remaining),
            ),
            max_cost_usd=settings.SERVICEMIND_MODEL_MAX_COST_USD_PER_CALL,
            cache_allowed=cache_allowed,
        )


class ModelRouteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    model: str
    model_revision: str
    reason: str


class ModelInvocationAudit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID = Field(default_factory=uuid4)
    context: ModelCallContext
    route: ModelRouteDecision
    prompt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    latency_ms: float = Field(ge=0)
    cost_usd: float = Field(ge=0)
    token_accounting_source: str = Field(pattern=r"^(provider|estimated|cache)$")
    pricing_version: str = Field(min_length=1, max_length=100)
    cost_estimate: bool
    attempts: int = Field(ge=1)
    retries: int = Field(ge=0)
    fallback_from: str | None = None
    status: str = Field(pattern=r"^(succeeded|failed|cache_hit)$")
    error_code: str | None = None


def stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
    ).hexdigest()
