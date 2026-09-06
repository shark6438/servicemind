from dataclasses import dataclass

from servicemind.domain.task import AgentName, ErrorPolicy


@dataclass(frozen=True)
class AgentContract:
    name: AgentName
    description: str
    input_schema: str
    output_schema: str
    allowed_tools: tuple[str, ...]
    forbidden_tools: tuple[str, ...]
    read_write_scope: str
    timeout_seconds: float
    error_policy: ErrorPolicy
    task_types: tuple[str, ...]


@dataclass(frozen=True)
class SupervisorContract:
    name: str = "supervisor"
    description: str = "Control-plane agent that selects validated workflow transitions."
    input_schema: str = "SupervisorStateView"
    output_schema: str = "SupervisorDecision"
    allowed_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = (
        "glpi.write",
        "glpi.credentials",
        "direct_http",
        "direct_database",
    )
    read_write_scope: str = "control_only"
    timeout_seconds: float = 20


class AgentRegistry:
    def __init__(self, contracts: tuple[AgentContract, ...]) -> None:
        self._contracts = {contract.name: contract for contract in contracts}
        if len(self._contracts) != len(contracts):
            raise ValueError("Agent registry contains duplicate names")

    def get(self, name: AgentName | str) -> AgentContract:
        try:
            normalized = name if isinstance(name, AgentName) else AgentName(name)
            return self._contracts[normalized]
        except (KeyError, ValueError) as exc:
            raise KeyError(f"Unknown ServiceMind agent: {name}") from exc

    def contains(self, name: AgentName | str) -> bool:
        try:
            self.get(name)
        except KeyError:
            return False
        return True

    @property
    def names(self) -> frozenset[AgentName]:
        return frozenset(self._contracts)


COMMON_FORBIDDEN = (
    "glpi.write",
    "glpi.credentials",
    "direct_http",
    "direct_database",
)

agent_registry = AgentRegistry(
    (
        AgentContract(
            name=AgentName.KNOWLEDGE,
            description="Read runbooks and return provenance-preserving knowledge evidence.",
            input_schema="KnowledgeTaskInput",
            output_schema="list[Evidence]",
            allowed_tools=(
                "knowledge.search.hybrid",
                "knowledge.parent.expand",
            ),
            forbidden_tools=COMMON_FORBIDDEN,
            read_write_scope="read_only",
            timeout_seconds=10,
            error_policy=ErrorPolicy.RETRY,
            task_types=("retrieve_knowledge", "retrieve_more_knowledge"),
        ),
        AgentContract(
            name=AgentName.DATA,
            description="Read tenant-scoped GLPI facts and return data evidence.",
            input_schema="DataTaskInput",
            output_schema="list[Evidence]",
            allowed_tools=(
                "glpi.read.ticket",
                "glpi.read.groups",
                "glpi.read.ticket_followups",
            ),
            forbidden_tools=COMMON_FORBIDDEN,
            read_write_scope="read_only",
            timeout_seconds=20,
            error_policy=ErrorPolicy.RETRY,
            task_types=("get_ticket", "retrieve_more_data"),
        ),
        AgentContract(
            name=AgentName.ANALYSIS,
            description="Derive an ITSM analysis only from joined evidence.",
            input_schema="JoinedEvidence",
            output_schema="AnalysisResult",
            allowed_tools=(),
            forbidden_tools=COMMON_FORBIDDEN,
            read_write_scope="no_tools",
            timeout_seconds=30,
            error_policy=ErrorPolicy.REPLAN,
            task_types=("analyze_ticket",),
        ),
        AgentContract(
            name=AgentName.REVIEWER,
            description="Review evidence, claims, policy and action reachability.",
            input_schema="AnalysisReviewInput",
            output_schema="ReviewResult",
            allowed_tools=(),
            forbidden_tools=COMMON_FORBIDDEN,
            read_write_scope="no_tools",
            timeout_seconds=20,
            error_policy=ErrorPolicy.ESCALATE,
            task_types=("review_analysis",),
        ),
        AgentContract(
            name=AgentName.ACTION,
            description="Convert a passed handoff into a credential-free ActionIntent.",
            input_schema="HandoffEnvelope",
            output_schema="ActionIntent",
            allowed_tools=(),
            forbidden_tools=COMMON_FORBIDDEN,
            read_write_scope="intent_only",
            timeout_seconds=10,
            error_policy=ErrorPolicy.FAIL_FAST,
            task_types=("propose_followup",),
        ),
    )
)

supervisor_contract = SupervisorContract()
