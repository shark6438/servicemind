from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from servicemind.domain.task import MAX_PLAN_TASKS, AgentName


class ControlOwner(StrEnum):
    SUPERVISOR = "supervisor"
    ACTION = "action"
    HUMAN = "human"
    HARNESS = "harness"
    NONE = "none"


class SupervisorAction(StrEnum):
    PLAN = "plan"
    DISPATCH = "dispatch"
    JOIN_EVIDENCE = "join_evidence"
    ANALYZE = "analyze"
    REVIEW = "review"
    RETRIEVE_MORE = "retrieve_more"
    REPLAN = "replan"
    HANDOFF_ACTION = "handoff_action"
    ESCALATE = "escalate"
    FINALIZE = "finalize"


class SupervisorDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: SupervisorAction
    selected_task_ids: list[str] = Field(default_factory=list, max_length=16)
    rationale_summary: str = Field(min_length=3, max_length=1000)
    confidence: float = Field(ge=0, le=1)


class PlanTaskProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(pattern=r"^T[1-9][0-9]*$")
    agent: AgentName
    task_type: str = Field(min_length=1, max_length=100)
    objective: str = Field(min_length=3, max_length=1000)
    depends_on: list[str] = Field(default_factory=list)


class PlanProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rationale_summary: str = Field(min_length=3, max_length=1500)
    # ``revise_plan`` re-wraps a revision as a ``PlanProposal`` (completed tasks
    # first, then the new ones), so this ceiling must admit a cumulative revision
    # too -- it is the compiled ``TaskPlan`` that bounds the run, not this type.
    tasks: list[PlanTaskProposal] = Field(min_length=1, max_length=MAX_PLAN_TASKS)
    max_parallel: int = Field(default=2, ge=1, le=4)


class PlanRevisionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rationale_summary: str = Field(min_length=3, max_length=1500)
    preserved_task_ids: list[str] = Field(default_factory=list)
    # A revision is cumulative: it re-lists every completed task next to the new
    # ones, so its ceiling has to match the compiled plan's rather than a first
    # plan's. At 12, a second revision (10 completed + 4 new) could not be
    # expressed at all and every run died on the second RETRIEVE_MORE.
    tasks: list[PlanTaskProposal] = Field(min_length=1, max_length=MAX_PLAN_TASKS)
    max_parallel: int = Field(default=2, ge=1, le=4)
