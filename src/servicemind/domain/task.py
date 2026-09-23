from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Hard ceiling on how many tasks a compiled plan may hold. A plan *revision*
#: re-lists every already-completed task alongside the new ones, so its proposal
#: has to be allowed to reach the same ceiling -- capping it lower made the second
#: revision unsatisfiable once enough tasks had completed.
MAX_PLAN_TASKS = 50

#: The one length contract for a run goal. Every producer of a goal quotes these rather
#: than restating the numbers: the API request model, this plan, and the webhook that
#: composes a goal out of a GLPI event. Two copies of a bound are two bounds, and the
#: path that is not enforced is the one that fails -- the webhook interpolated an
#: unbounded event name straight into the goal while the API path rejected anything over
#: the limit, so a single over-long delivery reached ``TaskPlan`` and died there as a 502
#: with no run record. A derived bound cannot drift; a copied one will.
GOAL_MIN_LENGTH = 3
GOAL_MAX_LENGTH = 2000

#: The largest value a 32-bit integer column holds, named once because more than one
#: client-supplied field is written into one. A value past it does not fail where it was
#: read -- it travels down to the driver and comes back as a database error, an HTTP 500
#: on a request the platform had already authenticated and accepted.
INT32_MAX = 2_147_483_647

#: The largest ticket id the platform can own, quoted from ``agent_runs.ticket_id``. Both
#: producers of a run quote this one number: the ``POST /runs`` body and the GLPI webhook,
#: whose ticket id is an arbitrary integer read out of an externally POSTed body.
TICKET_ID_MAX = INT32_MAX

#: The event cursor ``Last-Event-ID`` is allowed to carry, quoted from
#: ``run_events.sequence``. The header is client-supplied text: the SSE spec has a browser
#: echo back the ``id:`` this platform itself sent, but a hand-written client may send
#: anything, and the header is CORS-exposed so a browser can send it too. It was read as
#: ``int(last_event_id or 0)`` in the request handler, which meant three separate ways for
#: a header to decide the response: text that is not a number raised ``ValueError`` past
#: every handler (a 500), a negative cursor silently replayed the log from the beginning,
#: and a value past the column reached the driver as a parameter it cannot bind. A cursor
#: is a position in *this* run's log, so the ends of that column are the ends of it.
EVENT_SEQUENCE_MAX = INT32_MAX
EVENT_SEQUENCE_MIN = 0

#: Ceiling on a completed task's ``output_ref``. It is a reference for the run ledger --
#: the decision node writes ``"analysis_result"``, the review node ``"review_result"`` --
#: so the evidence tasks are the only producers that build a *list* into it, and they must
#: fit it rather than be assumed to. Shared so the producer clips to the same number the
#: field enforces; a restated copy is how the join came to write 519 characters into a
#: 500-character field and only fail on the next node's re-validation of the plan.
TASK_OUTPUT_REF_MAX_LENGTH = 500


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
    # Was capped at 50 while ``max_steps`` and ``max_tool_calls`` were capped at 100.
    # The odd one out was also the binding one: ``dispatch_node`` grants every task up
    # to two model calls, so a plan at ``MAX_PLAN_TASKS`` needs more than 50 for its
    # tasks alone and could never have completed even one pass under this ceiling.
    max_model_calls: int = Field(default=12, ge=0, le=100)
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
    output_ref: str | None = Field(default=None, max_length=TASK_OUTPUT_REF_MAX_LENGTH)
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
    goal: str = Field(min_length=GOAL_MIN_LENGTH, max_length=GOAL_MAX_LENGTH)
    tasks: list[Task] = Field(min_length=1, max_length=MAX_PLAN_TASKS)
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
