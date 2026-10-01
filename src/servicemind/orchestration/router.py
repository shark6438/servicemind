import re

from servicemind.domain.routing import RouteDecision, RouteType
from servicemind.domain.task import AgentName

#: The agents a question needs once it has to be *answered* rather than looked up:
#: retrieve facts and knowledge, reason over them, and have the reasoning checked. Three
#: routes below return this same set, so it is named once -- the routes differ in why
#: they were chosen, not in what they need.
_REVIEWED_PIPELINE = (
    AgentName.DATA,
    AgentName.KNOWLEDGE,
    AgentName.ANALYSIS,
    AgentName.REVIEWER,
)

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
#: A report of a concrete failure that asks to be handled is a diagnosis request, not a
#: lookup -- and the troubleshooting rows below used to claim otherwise. They matched on
#: the interrogative alone ("如何处理", "how to"), which is the exact anti-pattern the data
#: rows warn about two comments down: the *question shape* was taken for the *question
#: subject*. "How to troubleshoot VPN MFA?" asks for the documented procedure and is a
#: lookup; "How do I fix a VPN MFA failure after changing phones?" supplies the symptom
#: and the change that produced it, so the thing being asked for is a decision about a
#: situation, and only the reviewed pipeline can give one.
#:
#: Measured on the live stack (2026-09-30), the second shape was answered by the
#: retrieval-only fast path with ``answer`` set to twelve raw parent chunks, no model
#: call and no reviewer -- and the twelve included the symptom-similar decoy, so the
#: caller received a competing root cause with nothing marking it as the alternative.
#: Asking the same question with "应该怎么处理" instead of "该怎么处理" routed it to
#: ``complex_workflow`` and produced the diagnosis, the decoy named as an alternative,
#: and the three confirming facts checked. One character of the interrogative decided
#: which of those two products the user got.
#:
#: Both halves are required, which is what keeps this off the artifact lookups: "Find the
#: runbook for the MFA failure" names a symptom and asks to retrieve a document, so it
#: stays a lookup. The boundary is pinned by rows on *both* sides of it -- the incident
#: reports that must reach the pipeline and the artifact lookup that merely mentions a
#: symptom are both in ``evaluation/routing/routing.jsonl``, so an edit to these patterns
#: that moves the line has to move a case with it rather than quietly re-route a shape
#: nothing was measuring.
_INCIDENT_SYMPTOM = (
    r"失败|报错|出错|异常|中断|不通|连不上|无法|不能|未通过|没通过|打不开|被拒|卡住|挂住|超时"
    r"|\b(?:fails?|failing|failed|failure|errors?|broken|cannot|can't|unable|rejected"
    r"|denied|timed? ?out)\b"
)
_INCIDENT_ASK = (
    r"怎么办|怎么处理|如何处理|如何解决|如何修复|怎么解决|该怎么|帮我|求助"
    r"|\bhow\s+(?:do|should|can)\s+i\b|\bhow\s+to\b|\bwhat\s+should\b|\bhow\s+should\b"
)
INCIDENT_PATTERNS = (
    rf"(?:{_INCIDENT_SYMPTOM}).{{0,80}}(?:{_INCIDENT_ASK})",
    rf"(?:{_INCIDENT_ASK}).{{0,80}}(?:{_INCIDENT_SYMPTOM})",
)
#: A question that asks *for* a documented procedure, rather than one that merely
#: mentions one. The artifact noun alone is not enough: "官方手册给出了明确处置。请给出
#: 结论。" names a manual and asks for a conclusion, and sending it down the knowledge
#: fast path would answer the wrong question. What makes a knowledge lookup is the
#: artifact being the thing asked about -- the same shape as the English ``runbook``
#: row above, where ``runbook`` is what ``what`` points at.
#:
#: The English row used to be a bare alternation over those nouns, which does not say
#: "the artifact is what is asked about"; it says "the artifact is mentioned anywhere".
#: Measured over the 200-case quality batch (2026-09-24), that cost more than it looks:
#: "Which team owns the VPN client connectivity procedure, and what is its response
#: commitment?", "...the database failover runbook?" and "...under the current split
#: tunnel policy?" are questions *about* an owner and *about* routed traffic, and each
#: was sent down the retrieval-only fast path, which reaches no analysis and no review
#: and so cannot answer them. The Chinese rows already required the artifact to be what
#: the question asks about; the English rows now do too, in the three shapes a lookup
#: really takes -- ask to retrieve it, ask what it says, or ask how to do something.
#:
#: This is not a tighter guess at intent. Every one of the 25 fast-path cases in
#: ``evaluation/routing/routing.jsonl`` satisfies these rows, and the questions above do
#: not, so the rule is the one the golden set was written against.
_ARTIFACT = (
    r"runbooks?|sops?|standard operating procedures?|guides?|handbooks?|manuals?|"
    r"playbooks?|procedures?|polic(?:y|ies)|knowledge\s?(?:base|article)?|"
    r"troubleshooting|knowledge|"
    r"知识库|运行手册|操作手册|标准流程|排障|文档|矩阵|规范|手册|流程|规程|指引|"
    r"预案|方案|步骤|做法|清单|知识"
)
_RETRIEVE = (
    r"find|show|read|search|look\s?up|get|fetch|retrieve|display|list|"
    r"查询|查找|查一下|搜索|读取|显示|列出|检索|找一下|看一下"
)
KNOWLEDGE_PATTERNS = (
    # Ask to retrieve the artifact: "Find the VPN incident runbook", "查询 VPN 操作手册".
    rf"(?:{_RETRIEVE}).{{0,30}}(?:{_ARTIFACT})",
    # Ask what the artifact says: "What does the incident handling guide say?".
    rf"what\s+do(?:es)?\s+(?:the\s+)?.{{0,30}}(?:{_ARTIFACT}).{{0,30}}"
    r"(?:say|cover|covers|state|states|recommend|recommends|require|requires)",
    # Ask which artifact covers or defines something: "What runbook covers gateway
    # reachability?", "What knowledge article covers MFA enrollment?".
    rf"what\s+(?:{_ARTIFACT})\b",
    # Ask for the artifact's content: "What is the procedure for X?", "What is the
    # company policy on Y?". These are lookups -- whether the corpus can serve them is
    # a question for retrieval, not for the router; see ``fast_knowledge_node``.
    rf"what\s+(?:is|are)\s+(?:the\s+|a\s+|an\s+)?(?:\w+'s\s+)?(?:{_ARTIFACT})\b",
    # Troubleshooting, which is a lookup by construction.
    r"\bhow\s?to\b|\btroubleshoot\b|\bhow\s+do\s+i\b",
    r"如何处理|怎么处理|怎么办|如何排查",
    # Chinese content questions: "知识库里如何处理 MFA 故障？", "知识库有什么 VPN 指南？".
    r"知识库.{0,20}(?:如何|怎么|什么|哪些|有)",
    rf"(?:{_ARTIFACT}).{{0,8}}(?:是|为|有)?.{{0,4}}(?:什么|哪些)",
)
#: A *data* query is selected by the field it asks about, never by the interrogative.
#: "是什么" and "who" appear in questions of every kind -- including knowledge questions
#: that happen to end in them -- so matching on them made this bucket swallow requests
#: it has no way to answer: the run returned the ticket's raw fields and reported
#: SUCCEEDED. Every row below now names a subject the data agent actually serves.
#:
#: Naming a field is still not enough, and the same batch showed why. "What HTTP status
#: does the API gateway return when a client is rate limited?", "Is a service account
#: without a named owner permitted?" and "How often must a SEV1 be updated?" contain
#: ``status``, ``owner`` and ``updated`` -- as the subject matter of a knowledge
#: document, not as a field of any record. All three were answered with the raw JSON of
#: a GLPI ticket. A data answer is a statement about *one identifiable record*, so the
#: question has to name one; every one of the 25 data cases in the routing golden set
#: does ("Ticket #2 当前状态是什么？", "Who owns incident 2?"), and none of the three
#: misrouted questions does.
_RECORD = r"tickets?|incidents?|changes?|problems?|工单|事件|问题单|变更"
_FIELD = (
    r"status|state|assignee|owner|owns?|owned|priority|urgency|impact|created|updated|"
    r"fields?|"
    r"字段|标题|描述|状态|负责人|指派给谁|当前优先级|紧急度|影响度|创建时间|更新时间"
)
#: A record is identifiable by an id ("ticket 2", "工单 2", "#2"), or deictically by
#: pointing at one already in play ("this ticket", "当前工单").
_RECORD_REFERENCE = (
    rf"(?:{_RECORD})\s*#?\s*\d+"
    rf"|#\s*\d+\s*(?:{_RECORD})"
    rf"|\d+\s*号\s*(?:{_RECORD})"
    rf"|(?:this|that|the\s+current|our)\s+(?:{_RECORD})"
    rf"|(?:这个|该|本|当前|目前)\s*(?:{_RECORD})"
    rf"|(?:{_RECORD})\s*的?\s*(?:当前|目前)"
)
DATA_PATTERNS = (
    rf"(?:{_RECORD_REFERENCE}).{{0,40}}(?:{_FIELD})",
    rf"(?:{_FIELD}).{{0,40}}(?:{_RECORD_REFERENCE})",
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
        if request_write:
            return RouteDecision(
                route=RouteType.COMPLEX_WORKFLOW,
                required_capabilities=[*_REVIEWED_PIPELINE, AgentName.ACTION],
                reason_code="analysis_or_action_required",
                confidence=0.98,
            )
        # Before the general analysis row, so a question that is both "分析…" and an
        # incident report is filed under the narrower reason. Nothing routes differently
        # -- both are COMPLEX_WORKFLOW with the same capabilities -- but the reason_code
        # is what a reader has to attribute these runs with, and "the caller reported a
        # failure" says more about why the full pipeline was spent than "analysis was
        # mentioned" does.
        if any(re.search(pattern, normalized) for pattern in INCIDENT_PATTERNS):
            return RouteDecision(
                route=RouteType.COMPLEX_WORKFLOW,
                required_capabilities=list(_REVIEWED_PIPELINE),
                reason_code="incident_symptom_requires_diagnosis",
                confidence=0.9,
            )
        if any(re.search(pattern, normalized) for pattern in COMPLEX_PATTERNS):
            return RouteDecision(
                route=RouteType.COMPLEX_WORKFLOW,
                required_capabilities=list(_REVIEWED_PIPELINE),
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
