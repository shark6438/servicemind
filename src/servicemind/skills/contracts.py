from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from servicemind.context.contracts import ContextAgent


class SkillRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class SkillScope(StrEnum):
    GLOBAL = "global"
    TENANT = "tenant"


class SkillManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    skill_id: str = Field(pattern=r"^[a-z][a-z0-9-]{2,79}$")
    version: str = Field(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope: SkillScope
    tenant_id: str | None = None
    allowed_agents: frozenset[ContextAgent]
    required_tools: frozenset[str] = Field(default_factory=frozenset)
    risk_level: SkillRisk
    input_schema: dict[str, object]
    output_schema: dict[str, object]
    evidence_requirements: tuple[str, ...]
    context_requirements: tuple[str, ...]
    tests: tuple[str, ...]
    keywords: frozenset[str]
    created_at: datetime
    deprecated_at: datetime | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> SkillManifest:
        if (self.scope is SkillScope.TENANT) != (self.tenant_id is not None):
            raise ValueError("tenant-scoped skills require tenant_id; global skills forbid it")
        if not self.allowed_agents:
            raise ValueError("skill must allow at least one agent")
        if not self.evidence_requirements or not self.tests:
            raise ValueError("skill must declare evidence requirements and acceptance tests")
        return self


class PublishedSkill(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest: SkillManifest
    instructions: str = Field(min_length=1, max_length=50_000)
    source_path: str


class ResolvedSkill(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    skill: PublishedSkill
    effective_capabilities: frozenset[str]
    match_score: float = Field(ge=0, le=1)
    resolution_reason: str
