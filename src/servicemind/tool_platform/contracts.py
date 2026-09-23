from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class ToolAccess(StrEnum):
    READ = "read"
    SUBMIT_INTENT = "submit_intent"
    WRITE = "write"


class ToolRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class DataClassification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class RetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_attempts: int = Field(default=1, ge=1, le=5)
    base_delay_ms: int = Field(default=100, ge=10, le=10_000)
    max_delay_ms: int = Field(default=2_000, ge=10, le=30_000)
    retryable_codes: frozenset[str] = Field(
        default_factory=lambda: frozenset({"timeout", "rate_limit", "provider_unavailable"})
    )


class ToolDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,159}$")
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    provider: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,79}$")
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    read_write_type: ToolAccess
    risk_level: ToolRisk
    allowed_roles: frozenset[str] = Field(min_length=1)
    allowed_entities: frozenset[int] = Field(default_factory=frozenset)
    requires_approval: bool = False
    timeout_seconds: float = Field(default=15, gt=0, le=120)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    idempotency_strategy: str = Field(min_length=1, max_length=100)
    verification_strategy: str = Field(min_length=1, max_length=100)
    data_classification: DataClassification
    discoverable: bool = True
    active: bool = True

    @model_validator(mode="after")
    def enforce_write_contract(self) -> ToolDefinition:
        if self.read_write_type is ToolAccess.WRITE:
            if not self.requires_approval:
                raise ValueError("side-effecting tools require approval")
            if self.retry_policy.max_attempts != 1:
                raise ValueError("side-effecting tools cannot retry outside the durable harness")
            if self.idempotency_strategy == "none" or self.verification_strategy == "none":
                raise ValueError("side-effecting tools require idempotency and verification")
        return self

    @property
    def checksum(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    run_id: UUID
    task_id: str = Field(min_length=1, max_length=100)
    user_id: str = Field(min_length=1, max_length=255)
    roles: frozenset[str]
    entity_ids: frozenset[int] = Field(default_factory=frozenset)
    #: The group scope the caller is actually holding. Absent until the graph tool needed
    #: it, which is the same shape as the defect this field closes: ``TenantContext``
    #: carried it, the tool boundary dropped it, and a group-restricted resource was
    #: then unreachable *or* reachable depending on which side of the boundary you read.
    #: Empty means the caller holds no group -- denial, not a wildcard.
    group_ids: frozenset[int] = Field(default_factory=frozenset)
    capabilities: frozenset[str]
    tool_name: str
    tool_version: str
    arguments: dict[str, Any]
    taint_labels: frozenset[str] = Field(default_factory=frozenset)
    approval_ref: str | None = Field(default=None, max_length=500)
    approval_binding: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    deadline: AwareDatetime

    @property
    def argument_hash(self) -> str:
        canonical = json.dumps(
            self.arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    @property
    def approval_digest(self) -> str:
        canonical = json.dumps(
            {
                "tenant_id": str(self.tenant_id),
                "run_id": str(self.run_id),
                "tool": self.tool_name,
                "version": self.tool_version,
                "argument_hash": self.argument_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def ensure_active(self) -> None:
        if datetime.now(UTC) >= self.deadline:
            raise TimeoutError("tool call deadline expired")


class ToolPolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: UUID = Field(default_factory=uuid4)
    allow: bool
    requires_approval: bool
    allowed_fields: frozenset[str] = Field(default_factory=frozenset)
    redactions: tuple[str, ...] = ()
    reason_codes: tuple[str, ...]
    policy_version: str = Field(min_length=1, max_length=200)
    external_decision_id: str | None = Field(default=None, max_length=255)


class ProviderResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    output: Any
    provider_request_id: str | None = Field(default=None, max_length=255)
    status_code: int | None = Field(default=None, ge=100, le=599)


class ToolExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    tool_name: str
    tool_version: str
    provider: str
    output: Any
    output_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    verified: bool
    duplicate_suppressed: bool = False
    attempts: int = Field(ge=1, le=5)
    policy_decision_id: UUID


class ToolProvider(Protocol):
    name: str

    async def execute(self, definition: ToolDefinition, call: ToolCall) -> ProviderResult: ...

    async def verify(
        self, definition: ToolDefinition, call: ToolCall, result: ProviderResult
    ) -> bool: ...


class ToolPolicyEngine(Protocol):
    async def decide(self, definition: ToolDefinition, call: ToolCall) -> ToolPolicyDecision: ...


def output_hash(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
