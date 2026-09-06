from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AgentName(StrEnum):
    KNOWLEDGE = "knowledge"
    DATA = "data"
    ANALYSIS = "analysis"
    REVIEWER = "reviewer"
    ACTION = "action"


class TaskStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class ErrorPolicy(StrEnum):
    FAIL_FAST = "fail_fast"
    RETRY = "retry"
    SKIP = "skip"
    REPLAN = "replan"
    ESCALATE = "escalate"


class Budget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_steps: int = Field(default=24, ge=1, le=100)
    max_replans: int = Field(default=2, ge=0, le=5)
    max_model_calls: int = Field(default=12, ge=0, le=50)
    max_tool_calls: int = Field(default=16, ge=0, le=100)
    token_budget: int | None = Field(default=None, ge=1)
    cost_budget: float | None = Field(default=None, ge=0)
    deadline: datetime

    @model_validator(mode="after")
    def validate_deadline(self) -> Self:
        if self.deadline.tzinfo is None:
            raise ValueError("Budget deadline must be timezone-aware")
        return self


class BudgetSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    remaining_steps: int = Field(ge=0)
    remaining_replans: int = Field(ge=0)
    remaining_model_calls: int = Field(ge=0)
    remaining_tool_calls: int = Field(ge=0)
    deadline: datetime


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    task_id: str = Field(pattern=r"^T[1-9][0-9]*$")
    agent: AgentName
    task_type: str = Field(min_length=1, max_length=100)
    task_input: dict[str, Any] = Field(default_factory=dict, alias="input")
    depends_on: list[str] = Field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    output_ref: str | None = Field(default=None, max_length=500)
    attempts: int = Field(default=0, ge=0, le=10)
    error_policy: ErrorPolicy = ErrorPolicy.FAIL_FAST
    deadline: datetime

    @model_validator(mode="after")
    def validate_task(self) -> Self:
        if self.deadline.tzinfo is None:
            raise ValueError("Task deadline must be timezone-aware")
        if self.task_id in self.depends_on:
            raise ValueError("Task cannot depend on itself")
        return self


class TaskPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: UUID = Field(default_factory=uuid4)
    goal: str = Field(min_length=3, max_length=2000)
    tasks: list[Task] = Field(min_length=1, max_length=50)
    max_parallel: int = Field(default=4, ge=1, le=16)
    max_steps: int = Field(default=24, ge=1, le=100)
    max_replans: int = Field(default=2, ge=0, le=5)
    deadline: datetime
    budget: Budget
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_times_and_limits(self) -> Self:
        if self.deadline.tzinfo is None or self.created_at.tzinfo is None:
            raise ValueError("Plan timestamps must be timezone-aware")
        if self.budget.deadline != self.deadline:
            raise ValueError("Plan and budget deadlines must match")
        if self.max_steps != self.budget.max_steps:
            raise ValueError("Plan and budget max_steps must match")
        if self.max_replans != self.budget.max_replans:
            raise ValueError("Plan and budget max_replans must match")
        return self


class RuntimeControl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_stage: str = "created"
    replan_count: int = Field(default=0, ge=0)
    retrieval_round: int = Field(default=0, ge=0)
    total_steps: int = Field(default=0, ge=0)
    model_call_count: int = Field(default=0, ge=0)
    tool_call_count: int = Field(default=0, ge=0)
    consecutive_failures: int = Field(default=0, ge=0)
    errors: list[dict[str, Any]] = Field(default_factory=list)
