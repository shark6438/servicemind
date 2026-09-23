import re

from servicemind.domain.routing import RouteDecision, RouteType
from servicemind.domain.task import AgentName

FORBIDDEN_PATTERNS = (
    r"\b(delete|erase|purge|destroy)\b",
    r"删除|销毁|清空",
    r"\b(close|resolve)\b.*\b(ticket|incident)\b",
    r"关闭.*(?:工单|事件)",
    r"\b(password|credential|secret|token)\b.*\b(show|reveal|dump|give)\b",
    r"\b(show|reveal|dump|give)\b.*\b(password|credential|secret|token)\b",
    r"(?:显示|泄露|导出).*(?:密码|密钥|令牌)",
)
COMPLEX_PATTERNS = (
    r"\b(analy[sz]e|classif(?:y|ication)?|triage|recommend|root cause|recurring|risk|should)\b",
    r"\b(create|link)\b.*\b(problem|change)\b",
    r"分析|分类|推荐|根因|重复(?:事件|故障)|风险|判断|应该|是否",
    r"(?:创建|关联).*(?:问题单|变更)",
    r"\b(assign|route|take action)\b",
    r"分派|处理建议|执行|修改",
)
#: A question that asks *for* a documented procedure, rather than one that merely
#: mentions one. The artifact noun alone is not enough: "官方手册给出了明确处置。请给出
#: 结论。" names a manual and asks for a conclusion, and sending it down the knowledge
#: fast path would answer the wrong question. What makes a knowledge lookup is the
#: artifact being the thing asked about -- the same shape as the English ``runbook``
#: row above, where ``runbook`` is what ``what`` points at.
KNOWLEDGE_PATTERNS = (
    r"\b(runbook|sop|knowledge|guide|procedure|policy|how to|troubleshoot)\b",
    r"知识库|运行手册|操作手册|标准流程|排障|如何处理|怎么处理|文档|矩阵|规范",
    r"(?:手册|流程|规程|指引|预案|方案|步骤|做法|清单)\s*(?:是|为|有)?\s*(?:什么|哪些)",
)
#: A *data* query is selected by the field it asks about, never by the interrogative.
#: "是什么" and "who" appear in questions of every kind -- including knowledge questions
#: that happen to end in them -- so matching on them made this bucket swallow requests
#: it has no way to answer: the run returned the ticket's raw fields and reported
#: SUCCEEDED. Every row below now names a subject the data agent actually serves.
DATA_PATTERNS = (
    r"\b(status|assignee|owner|priority|urgency|impact|created|updated|fields?)\b",
    r"状态|负责人|指派给谁|当前优先级|紧急度|影响度|创建时间|更新时间|字段|标题|内容|描述",
)


class FastPathRouter:
    """Deterministic structured router; natural language never selects graph edges directly."""

    def route(self, request: str, *, request_write: bool) -> RouteDecision:
        normalized = " ".join(request.casefold().split())
        if any(re.search(pattern, normalized) for pattern in FORBIDDEN_PATTERNS):
            return RouteDecision(
                route=RouteType.UNSUPPORTED,
                required_capabilities=[],
                reason_code="forbidden_operation",
                confidence=1,
            )
        if request_write or any(re.search(pattern, normalized) for pattern in COMPLEX_PATTERNS):
            return RouteDecision(
                route=RouteType.COMPLEX_WORKFLOW,
                required_capabilities=[
                    AgentName.DATA,
                    AgentName.KNOWLEDGE,
                    AgentName.ANALYSIS,
                    AgentName.REVIEWER,
                    *([AgentName.ACTION] if request_write else []),
                ],
                reason_code="analysis_or_action_required",
                confidence=0.98,
            )
        if any(re.search(pattern, normalized) for pattern in KNOWLEDGE_PATTERNS):
            return RouteDecision(
                route=RouteType.SIMPLE_KNOWLEDGE_QUERY,
                required_capabilities=[AgentName.KNOWLEDGE],
                reason_code="knowledge_lookup_only",
                confidence=0.97,
            )
        if any(re.search(pattern, normalized) for pattern in DATA_PATTERNS):
            return RouteDecision(
                route=RouteType.SIMPLE_DATA_QUERY,
                required_capabilities=[AgentName.DATA],
                reason_code="fact_lookup_only",
                confidence=0.97,
            )
        return RouteDecision(
            route=RouteType.COMPLEX_WORKFLOW,
            required_capabilities=[
                AgentName.DATA,
                AgentName.KNOWLEDGE,
                AgentName.ANALYSIS,
                AgentName.REVIEWER,
            ],
            reason_code="ambiguous_request_requires_review",
            confidence=0.7,
        )


fast_path_router = FastPathRouter()
