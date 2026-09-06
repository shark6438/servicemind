from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from servicemind.domain.task import AgentName


class RouteType(StrEnum):
    SIMPLE_DATA_QUERY = "simple_data_query"
    SIMPLE_KNOWLEDGE_QUERY = "simple_knowledge_query"
    COMPLEX_WORKFLOW = "complex_workflow"
    UNSUPPORTED = "unsupported"


class RouteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    route: RouteType
    required_capabilities: list[AgentName] = Field(default_factory=list)
    reason_code: str = Field(min_length=1, max_length=100)
    confidence: float = Field(ge=0, le=1)
