结论：Phase 2 已经完成“多租户、可恢复、受控写入”的企业级垂直闭环。下一步不应继续堆 GLPI 接口，而应按依赖顺序完成 1+5、多智能体 Harness、企业 RAG、Memory、MCP、可靠性、安全评测和产品化。

需要重新锁定阶段编号：`ITSM实际方案.md` 中原来的 Phase 2 表示“1+5 MVP”，但实际已交付的 Phase 2 是企业安全垂直闭环。以后以当前代码为准，后续从 Phase 3 开始。

# 总体路线

| 阶段    | 核心成果                                                | 建议周期 |
| ------- | ------------------------------------------------------- | -------- |
| Phase 3 | 真正的 Supervisor + 5 Agents + Harness v2               | 3–4 周   |
| Phase 4 | Enterprise Hybrid RAG + Graph-RAG                       | 3–4 周   |
| Phase 5 | Memory + Context Engineering + Skills + Model Gateway   | 2–3 周   |
| Phase 6 | Tool Platform + MCP + Reliability + Security Governance | 3–4 周   |
| Phase 7 | Observability + Evaluation + Red Team + CI Gate         | 2–3 周   |
| Phase 8 | 产品界面、生产部署、负载测试、简历交付                  | 2–3 周   |

单人高质量完成约需 15–20 周。每阶段都必须包含代码、自动测试、异常场景、真实演示和验收报告，不能以“依赖已安装”代替完成。

------

# Phase 3：完成真正的 1+5 与 Harness v2

这是下一步最高优先级。

## 3.1 固定 1+5 职责

```
Supervisor
├── Knowledge Agent    只读知识与历史方案
├── Data Agent         只读 GLPI/CMDB 事实
├── Analysis Agent     分类、优先级、Problem/Change 判断
├── Reviewer Agent     证据、安全、策略审核
└── Action Agent       只生成 ActionIntent
```

严格权限：

| Agent      | 允许                            | 禁止                              |
| ---------- | ------------------------------- | --------------------------------- |
| Supervisor | 规划、路由、重规划              | 直接查询 GLPI、生成业务结论、写入 |
| Knowledge  | KB/RAG 检索                     | 写 GLPI、写长期记忆               |
| Data       | Ticket/Problem/Change/CMDB 查询 | 推断最终结论、写业务数据          |
| Analysis   | 基于 Evidence 推理              | 调用写工具                        |
| Reviewer   | 审核事实、引用、风险、策略      | 自己修复结论、写业务数据          |
| Action     | 生成 ActionIntent               | 持有凭据、直接 HTTP、绕过审批     |

## 3.2 新增模块

```
src/servicemind/orchestration/
├── router.py
├── planner.py
├── task_dag.py
├── dispatcher.py
├── join.py
├── supervisor.py
├── replanner.py
├── handoff.py
├── budget.py
└── loop_guard.py

src/servicemind/agents/
├── knowledge.py
├── reviewer.py
├── data.py
├── analysis.py
└── action.py

src/servicemind/domain/
├── plan.py
├── task.py
├── evidence.py
├── review.py
└── handoff.py
```

## 3.3 两条执行路径

简单查询必须走 Fast Path：

```
“INC-1234 当前负责人是谁？”
Router → Data Agent → Answer
```

复杂任务才启动多 Agent：

```
Router
  → Supervisor
  → Plan/DAG validation
  → Data + Knowledge 并行
  → Evidence Join
  → Analysis
  → Reviewer
      ├─ passed → Handoff
      ├─ retrieve_more → Data/Knowledge
      ├─ replan → Supervisor
      └─ reject/escalate → Human
  → ActionIntent
  → Phase 2 Harness/HITL
  → GLPI
```

LangGraph 官方当前支持通过 `Send` 做并行路由、Subgraph 隔离，以及让 Subgraph 继承 Checkpointer。大部分一次性子任务应使用 per-invocation persistence，避免不同子 Agent 共享污染状态。[LangGraph Subgraphs](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)、[Router](https://docs.langchain.com/oss/python/langchain/multi-agent/router)

## 3.4 Task DAG 不能只由 LLM 随便输出

必须做确定性校验：

- Task ID 唯一；
- Agent 名称必须在 Registry；
- DAG 不允许环；
- 依赖必须存在；
- 最大 Task 数，例如 12；
- 最大并行数，例如 4；
- 最大 replan 轮次，例如 2；
- 最大模型调用与工具调用预算；
- Deadline 和取消传播；
- 每个 Task 有输入 Schema、输出 Schema、状态和 Error Policy。

## 3.5 Handoff 的位置

只允许：

```
Reviewer passed
    ↓
Supervisor
    ↓ HandoffEnvelope
Action Agent
```

`HandoffEnvelope` 只携带：

- tenant/run/user；
- 已验证 Evidence 引用；
- ReviewResult；
- 允许的操作范围；
- 风险级别；
- 预算；
- 幂等上下文。

不传完整内部消息或思维过程。官方 Handoff 文档也强调只传必要上下文，并保持 AI Tool Call 与 ToolMessage 配对，避免上下文膨胀和消息历史损坏。[LangChain Handoffs](https://docs.langchain.com/oss/python/langchain/multi-agent/handoffs)

## 3.6 Phase 3 验收

必须至少满足：

- 100 个路由样本，routing accuracy ≥ 95%；
- 简单任务不触发无关 Agent；
- Data/Knowledge 能并行执行；
- Reviewer 失败能触发补检索或 replan；
- 超预算、超轮次必定终止；
- Reviewer 未通过时 Action Agent 永远不可达；
- 所有写入继续复用 Phase 2 Harness；
- Checkpoint 重启后 DAG 状态不丢失；
- Cross-tenant leakage、approval bypass、forbidden write 均为 0；
- 浏览器演示复杂 Incident 从分析到 GLPI 回写。

------

# Phase 4：Enterprise Hybrid RAG + Graph-RAG

## 4.1 技术选择

建议：

- 文档解析：Docling；
- 结构化切块：Docling `HybridChunker`；
- 文本检索：Elasticsearch/OpenSearch；
- Dense Embedding：模型通过配置选择；
- Sparse：BM25；
- Fusion：RRF；
- Reranker：Cross-Encoder，通过统一接口可替换；
- 原始文件：MinIO；
- 关系图：Neo4j；
- Canonical Metadata：PostgreSQL。

Docling 的 HybridChunker 会在文档层级切块基础上进行 token-aware split/merge，并能保留标题、表格等结构，适合 Runbook/SOP。[Docling Chunking](https://docling-project.github.io/docling/concepts/chunking/)

Neo4j 已提供官方 `neo4j-graphrag` Python 包，并支持 Python 3.14、向量检索和图检索组合。[Neo4j GraphRAG](https://neo4j.com/docs/neo4j-graphrag-python/current/index.html)

## 4.2 文档管道

```
GLPI KB / PDF / DOCX / HTML / Markdown / Runbook
  → Source validation
  → Malware/type/size check
  → Docling parse
  → UnifiedDocument
  → Structure-aware chunking
  → Parent-child relation
  → Tenant/ACL metadata
  → Dense + BM25 indexes
  → Version activation
```

每个 Chunk 必须包含：

```
tenant_id
source_id
document_id
version
parent_id
section_path
acl_subjects
content_hash
effective_from
effective_to
ingestion_run_id
```

ACL 必须在召回阶段过滤，不能先跨租户检索后再让 LLM过滤。

## 4.3 检索链

```
Query classification
→ Query rewrite
→ Identifier/entity extraction
→ Tenant + ACL pre-filter
→ Dense + BM25 parallel
→ RRF fusion
→ Cross-Encoder rerank
→ Parent expansion
→ Evidence dedup
→ Citation packaging
→ Reviewer citation verification
```

## 4.4 Graph-RAG 的真实用法

只同步 GLPI API 数据，不直接读取 GLPI 数据库：

```
Ticket ─AFFECTS→ CI
CI ─DEPENDS_ON→ Service
Ticket ─SIMILAR_TO→ Ticket
Ticket ─LINKED_TO→ Problem
Problem ─RESOLVED_BY→ Change
Change ─MODIFIES→ CI
```

演示场景：

> 查询最近 30 天 VPN/MFA 相似 Incident，找到共同受影响 Authentication Service，建议建立 Problem，并给出相关 Change 与 Runbook 证据。

## 4.5 验收指标

- Tenant/ACL 泄漏为 0；
- Recall@10、Recall@20、MRR、NDCG@10；
- RRF 相比 dense-only 有可量化增益；
- Reranker 相比 RRF 有可量化增益；
- Citation correctness ≥ 95%；
- Citation completeness ≥ 90%；
- 文档版本切换后旧版本不可召回；
- 删除/失效文档能从索引撤销；
- Prompt Injection 文档不能改变 Agent 权限或调用写工具。

------

# Phase 5：Memory、Context、Skills 与 Model Gateway

## 5.1 企业 Memory

四类记忆：

- Semantic：稳定的领域事实；
- Episodic：经过验证的历史处置案例；
- Procedural：Runbook 和组织流程；
- Preference：用户允许保存的输出偏好。

Memory 数据模型：

```
tenant_id
scope_type / scope_id
memory_type
content
source_trace_id
evidence_ids
confidence
importance
version
valid_from / valid_to
expires_at
status
created_by
```

## 5.2 Memory Writer 不是第六个 Agent

它属于 Harness Middleware：

```
Run completed
→ candidate extraction
→ evidence verification
→ worth-saving policy
→ PII/secret scan
→ exact + semantic dedup
→ conflict detection
→ quarantine or activate
→ version + TTL
```

未经证据支持的网页内容、用户指令或 Tool 输出不得直接成为永久企业事实。

## 5.3 Context Builder

每次模型调用只组装该 Agent 必需的内容：

```
Harness rules
+ Agent contract
+ Current task
+ Selected state
+ Ranked evidence
+ Allowed memory
+ Selected tool schemas
+ Token/cost budget
```

加入：

- Context ranking；
- Summarization；
- Tool result offloading；
- Context isolation；
- Token budgeting；
- PII/secret redaction；
- provenance/taint label。

官方目前将 Context Engineering 分为 Model、Tool 和 Lifecycle Context，并推荐通过 Middleware 做 Summarization、Guardrail 和动态工具选择。[Context Engineering](https://docs.langchain.com/oss/python/langchain/context-engineering)

## 5.4 Agent Skills

创建版本化 ITSM Skills：

```
skills/
├── incident-triage/SKILL.md
├── recurring-problem/SKILL.md
├── change-risk/SKILL.md
├── vpn-mfa/SKILL.md
└── major-incident/SKILL.md
```

要求：

- Progressive disclosure；
- Skill 版本和 checksum；
- tenant/global scope；
- allowed agents；
- required tools；
- risk classification；
- 配套测试样本；
- Skill 不能改变 Tool Policy。

## 5.5 Model Gateway

统一管理：

- 模型路由；
- Provider fallback；
- Structured Output；
- Timeout/retry；
- token/cost accounting；
- tenant/model allowlist；
- 高风险任务强模型、简单路由小模型；
- prompt/template version；
- semantic cache 必须包含 tenant、role、policy version。

验收不能只看模型能否回答，应比较质量、延迟和成本。

------

# Phase 6：Tool Platform、MCP、可靠性与安全治理

## 6.1 Tool Registry

每个 Tool 必须注册：

```
name / version
provider
input_schema / output_schema
read_write_type
risk_level
allowed_roles
allowed_entities
requires_approval
timeout
retry_policy
idempotency_strategy
verification_strategy
data_classification
```

统一执行：

```
Schema validation
→ Tenant/RBAC/ABAC
→ Prompt-injection/taint policy
→ Risk classification
→ Rate limit
→ Approval
→ Idempotency/lock
→ Provider execute
→ Read-back verify
→ Audit/trace
```

## 6.2 MCP 的正确实现

最终一定包含 MCP，但 MCP 不能绕过 Harness：

```
Agent
→ Tool Calling
→ Tool Gateway
→ Policy Engine
→ Native Provider / MCP Provider
→ GLPI API
```

实现两个 Provider：

- `NativeGlpiProvider`
- `McpGlpiProvider`

二者必须通过同一组 Contract/Parity Tests。

MCP 采用当前 `2026-07-28` 规范：

- stateless core；
- 不依赖 `Mcp-Session-Id`；
- `Mcp-Method`、`Mcp-Name` header routing；
- list cache hints；
- Tasks 扩展处理长任务；
- `tasks/get/update/cancel`；
- issuer validation；
- CIMD/Enterprise Managed Authorization；
- 不新建 legacy HTTP+SSE；
- 不使用已弃用 Roots、Sampling、Logging 作为新架构基础。

这些均来自 MCP 当前正式规范更新。[MCP 2026-07-28](https://blog.modelcontextprotocol.io/posts/2026-07-28/)

GLPI MCP Server 第一批能力：

```
resources:
  glpi://tickets/{id}
  glpi://problems/{id}
  glpi://changes/{id}
  glpi://cmdb/items/{id}
  glpi://knowledge/{id}

tools:
  search_tickets
  get_ticket_context
  search_knowledge
  query_cmdb_dependencies
  submit_action_intent
```

不暴露 `direct_update_ticket`。写入必须提交 ActionIntent，再进入 Phase 2 Harness。

## 6.3 Policy-as-Code

建议引入 OPA/Rego：

```
input:
  identity
  tenant
  roles
  resource
  action
  arguments
  risk
  evidence
  environment

output:
  allow
  requires_approval
  allowed_fields
  redactions
  reason
```

Policy Decision 必须写入 Audit，并记录 policy version。

## 6.4 可靠性

增加：

- PostgreSQL transactional outbox；
- Redis Streams worker queue；
- worker lease/heartbeat；
- visibility timeout；
- dead-letter queue；
- exponential backoff + jitter；
- per-tool timeout；
- circuit breaker；
- bulkhead；
- tenant rate limit；
- cancellation propagation；
- graceful shutdown；
- poison-task quarantine。

LangGraph 当前已经提供 node retry、timeout、error handler 和 resume-safe failure，可用于认知节点；外部副作用仍必须复用现有幂等 Harness。[LangGraph Fault Tolerance](https://docs.langchain.com/oss/python/langgraph/fault-tolerance)

不建议现在引入 Temporal：它会与 LangGraph Checkpoint 形成两套 Workflow 真相来源。现阶段用 PostgreSQL Outbox + Redis Worker 更清晰。

## 6.5 Agentic Security

安全基线映射：

- OWASP Top 10 for Agentic Applications 2026；
- NIST AI RMF / Generative AI Profile；
- 企业自身 GLPI Policy。

重点攻击：

- indirect prompt injection；
- tool misuse；
- identity/privilege abuse；
- goal manipulation；
- memory poisoning；
- excessive agency；
- cascading multi-agent failure；
- insecure inter-agent communication；
- sensitive data leakage；
- resource/cost exhaustion。

OWASP 已将这些作为自主 Agent 的核心风险类别；NIST AI RMF 则要求把治理、测量和风险处置贯穿整个生命周期。[OWASP Agentic Top 10](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/)、[NIST AI RMF](https://www.nist.gov/itl/ai-risk-management-framework)

------

# Phase 7：Observability、Evaluation、红队与 CI Gate

## 7.1 可观测栈

```
Application
→ OTel SDK
→ OTel Collector
├── Tempo/Jaeger traces
├── Prometheus metrics
├── Loki logs
└── Grafana dashboards

LLM semantic traces
→ Langfuse or LangSmith
```

Trace 层次：

```
invoke_workflow
├── invoke_agent: supervisor
├── retrieval
├── invoke_agent: data
├── invoke_agent: knowledge
├── invoke_agent: analysis
├── invoke_agent: reviewer
├── handoff
└── execute_tool
```

OpenTelemetry 已定义 `invoke_agent`、`invoke_workflow`、`execute_tool`、`retrieval` 等 GenAI operation；但 Tool 参数和结果可能包含敏感信息，因此默认不记录原始 Prompt、凭据和完整 Tool payload。[OTel GenAI Semantic Conventions](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/)

指标：

- agent/task latency；
- routing/handoff/replan 次数；
- token、cache token、cost；
- tool success/retry/timeout；
- approval latency；
- duplicate suppression；
- RAG retrieval/rerank latency；
- queue depth、DLQ；
- per-tenant quota；
- policy denial；
- security violation。

## 7.2 Evaluation

建立版本化 Golden Dataset：

```
evaluation/
├── datasets/
│   ├── routing.jsonl
│   ├── triage.jsonl
│   ├── problem_detection.jsonl
│   ├── change_risk.jsonl
│   ├── rag.jsonl
│   ├── memory.jsonl
│   └── security.jsonl
├── graders/
├── runners/
└── reports/
```

评测分三层：

- Final response；
- Single step；
- Full trajectory。

这也是当前官方推荐的 Agent 评测分类。[LangSmith Agent Evaluation](https://docs.langchain.com/langsmith/evaluation-approaches)

同时适配 ServiceNow ITSM-SafetyBench 到 GLPI，测试：

- 篡改时间骗 SLA；
- 降低 P1 Priority；
- 绕过 Change Approval；
- 删除证据；
- 伪造 Root Cause；
- 越权修改 CMDB；
- 用户施压下是否坚持拒绝/升级。

[ServiceNow ITSM-SafetyBench](https://github.com/ServiceNow/ServiceNow-itsm-safety-bench)

## 7.3 CI 硬门槛

```
ruff
pyrefly/mypy
pytest
migration-from-empty
integration tests
MCP contract tests
RAG regression
trajectory eval
security eval
Docker build
SBOM / dependency scan
container scan
load smoke
```

绝对门槛：

```
Cross-Tenant Leakage = 0
Approval Bypass = 0
Forbidden Write = 0
Duplicate Side Effect = 0
Critical Safety Violation = 0
Secret In Trace/Log = 0
```

非安全指标先建立 baseline，再要求新版本不得显著退化，不能先拍一个漂亮数字。

------

# Phase 8：产品化与交付

## 8.1 Demo UI

至少包含：

1. ITSM Workbench；
2. Agent Run/DAG；
3. Evidence/Citations；
4. Problem/Change 建议；
5. Approval Center；
6. Trace/Audit；
7. Evaluation Dashboard。

前端可先用 Streamlit 完成流程验证，最终再用 React/Next.js + AG-UI。不要在核心 Agent 未稳定前消耗大量时间做视觉效果。

## 8.2 生产基础设施

- Keycloak production mode；
- Authorization Code + PKCE；
- service account/client credentials；
- TLS reverse proxy；
- Vault/KMS 和密钥轮换；
- PostgreSQL backup/PITR；
- Redis persistence；
- MinIO versioning；
- Elasticsearch/Neo4j backup；
- 数据保留和删除策略；
- Docker Compose dev/demo/full profiles；
- Kubernetes/Helm 只在负载和多副本确有需要时加入；
- Locust/k6 性能与故障注入；
- SBOM、镜像签名、漏洞扫描；
- Restore drill，而不只是“有备份配置”。

------

# 企业技术取舍

## 必须实现

- LangGraph StateGraph、Subgraph、Send、Command；
- Supervisor、Agent-as-Tool、Handoff；
- Task DAG、fan-out/join、replan；
- Durable Execution、HITL；
- Harness、Tool Registry、Policy Engine；
- MCP 2026 stateless + Tasks + Authorization；
- Hybrid RAG、RRF、Reranker、Citation；
- Graph-RAG；
- Memory Governance；
- Context Engineering、Skills；
- Model Gateway、Fallback、Budget；
- OWASP Agentic Security；
- OTel、Evaluation、SafetyBench；
- Multi-tenant、RBAC、ABAC、Audit；
- Queue、outbox、rate limit、circuit breaker。

## 有明确条件才实现

A2A 只用于独立部署、不同所有者或不同框架的 Agent Service。官方也明确区分：MCP 是 Agent-to-Tool，A2A 是独立 Agent-to-Agent。[A2A Protocol](https://a2a-protocol.org/latest/)

当前五个 Agent 在同一个 ServiceMind Runtime 内，因此不应使用 A2A 替代 LangGraph Subgraph。可以保留 Adapter 接口，未来接入独立 Monitoring Agent 时再实现。

## 明确不加入主线

- Agent Swarm；
- Voice；
- Computer Use；
- Fine-tuning/RL；
- Kafka；
- Service Mesh；
- Multi-region；
- 为展示而引入的区块链或向量数据库堆叠。

这些技术对当前 Incident → Problem → Change 主线没有足够收益。

------

# 下一步的具体任务

现在应立即启动 Phase 3，第一批开发顺序是：

1. 定义 `Evidence`、`ReviewResult`、`TaskPlan`、`HandoffEnvelope`；
2. 实现 Knowledge Agent 和 Reviewer Agent；
3. 实现 deterministic Fast Path Router；
4. 实现 Supervisor + DAG Validator；
5. 用 `Send` 并行 Data/Knowledge；
6. 实现 Join、Reviewer 和 bounded replan；
7. 建立 Reviewer → Action 的显式 Handoff；
8. 全部写操作复用 Phase 2 Harness；
9. 增加 routing/trajectory/security Golden Dataset；
10. 完成浏览器演示和 Phase 3 验收报告。

在 Phase 3 完成前，不增加 `close_ticket`、`create_problem`、`create_change` 等新写工具。先把控制面、证据面和审核面做正确，再扩展业务写能力。