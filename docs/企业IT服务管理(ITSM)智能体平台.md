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
- 生产推理：BGE-M3 与 BGE-Reranker-v2-M3 分别绑定独立 GPU，模型 revision 与镜像 digest 固定；
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

生产检索实现固定使用 100/100/100 dense、BM25 与候选深度，OpenSearch `pagination_depth`
与返回 `size` 分离；重排输入包含标题与子块正文，最终排序保留 15% 归一化 RRF 信号。
索引 schema v3 只写入 PostgreSQL 权威文档哈希，且必须在 refresh 后才发布新别名，
避免合法候选被哈希误删或空新代无法激活。

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

三类长期记忆，Preference 是 Semantic subtype：

- Semantic：稳定的领域事实，以及用户明确同意保存的 Preference；
- Episodic：经过验证的历史处置案例；
- Procedural：从至少两个不同成功 Run 的 Episodic Memory 中归纳出的做法；正式 Runbook 和组织流程仍属于 RAG，不复制为 Memory。

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
provenance / taint_labels
supporting_episode_ids
consent_ref
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

固定权威关系为 `Memory < Skill < Policy`。Procedural 永不自动激活；从 quarantine
转为 active 必须有人审引用、重新执行 secret/PII/injection/evidence/TTL 检查，并验证至少
两个来自不同成功 Run、**不同工单**且当前仍 active 的 Episodic 证据。生产候选只在
`recurring_incident=true` 且同一规范化 problem/change 建议跨工单重复时生成；模式键不包含
工单号、自由推理、工具参数或资源 ID。撤销或过期 Episode 时必须在服务前撤销依赖它的
Procedural Memory，并写入 `PROCEDURAL_SUPPORT_INVALIDATED` 审计事件。激活、候选读取和最终
重校验复用同一支撑判定，Procedure 自身较长的 TTL 不能覆盖其论证证据已经失效的事实。所有读取
在向量计算后再次用 PostgreSQL 权威状态、scope、entity、group、TTL 和 taint 重校验，关闭异步
检索期间的撤销/权限竞态。

人工复核面必须提供 tenant/entity/group ACL 过滤的 quarantine 队列，复核人必须持有
`approver` 角色及 OIDC `glpi_group_ids` 授权。激活/拒绝决定在仓储锁内绑定
`expected_version + expected_content_hash + expected_status`，防止陈旧页面或并发相反决定覆盖
先完成的审核；每次决定写 append-only 审计事件。缺少组声明时按空权限失败关闭。
现有 Streamlit 控制台必须提供 Keycloak OIDC 保护的 **Memory Review** 页面；页面只能调用上述
API，不复制或放宽后端授权，并使用加载时的版本、内容摘要和状态提交原子决定。

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

调用前/失败路径的 token 估算必须完全离线并采用保守上界，不得在请求热路径下载 tokenizer；
成功调用以供应商 usage 为权威，审计明确区分 `provider` 与 `estimated` 来源。

验收不能只看模型能否回答，应比较质量、延迟和成本。

Phase 5 验收必须把“工程控制通过”和“业务效果认证”分开：RAG/Memory/Context/Skill/Model Gateway
的隔离、撤销、权限、审计和失败关闭由自动化门禁验证；Memory A/B、RAG Recall/拒答、业务任务成功率
必须由固定数据集的实际指标证明。没有生产查询日志或 ITSM 专家签署时可以工程关闭，但不得写成
生产质量全面认证。

------

# Phase 6：Governed Tool Platform、MCP 与可靠执行

## 6.0 最终裁定与边界

Phase 6 冻结为四个相互独立、由统一合约连接的模块：

1. Governed Tool Platform：Tool Registry、唯一 Tool Gateway、Policy、Audit；
2. MCP：当前无会话协议、OAuth 受保护资源发现、Native/MCP Provider parity；
3. Reliability：PostgreSQL Outbox、Redis Streams、限流、超时、重试、熔断、舱壁、取消与任务 lease；
4. Agentic Security：能力交集、taint、审批绑定、密钥隔离、审计和资源上限。

本阶段只修改 ServiceMind 项目业务链路，不把服务器上的通用示例 Agent、外部项目 Agent 或运行实例 Agent 纳入架构和验收。MCP 是传输与能力暴露边界，不能成为第二套权限系统，也不能绕过 Phase 2 Harness。工具层不暴露直接 GLPI 写接口；写请求只能创建 `ActionIntent`，随后仍由 Reviewer、HITL、幂等执行器和 read-back verification 控制。

## 6.1 Tool Registry 与唯一执行边界

每个 Tool 必须注册并冻结：

```
name / semver / checksum / provider
input_schema / output_schema
read_write_type / risk_level / data_classification
allowed_roles / allowed_entities
requires_approval / approval_binding
timeout / retry_policy / rate_limit / bulkhead
idempotency_strategy / verification_strategy
```

唯一执行路径为：

```
Agent
→ ToolCall contract
→ JSON Schema Draft 2020-12
→ tenant + RBAC + ABAC + capability intersection
→ taint / secret / prompt-injection guard
→ OPA policy decision
→ approval binding（需要时）
→ tenant rate limit + per-tool circuit breaker + bulkhead
→ idempotency lock
→ NativeGlpiProvider / McpGlpiProvider
→ output schema + read-back verify
→ append-only policy/invocation audit
```

读操作只对明确的可重试故障执行 full-jitter 有界重试；副作用不在 Tool Gateway 内盲重试。进程内幂等缓存有容量和 TTL，跨进程写入不变量由 PostgreSQL advisory lock、唯一约束、ActionIntent hash 和现有 Harness 保证。

## 6.2 MCP 2026-07-28

MCP 固定采用正式 `2026-07-28` 协议：

- JSON-RPC 2.0 envelope；
- 每个请求携带 protocol version 和 client capabilities；`clientInfo` 按协议为 SHOULD，可省略，携带时必须通过 schema 校验；
- `Mcp-Method`、`Mcp-Name` header 与 body 交叉校验；
- `server/discover` 与带 `ttlMs/cacheScope` 的 list/read；
- 不使用 `Mcp-Session-Id`、legacy HTTP+SSE 或 initialize handshake；
- 长 CMDB 查询由服务端决定升级为 `io.modelcontextprotocol/tasks`；
- 任务支持 `tasks/get/update/cancel`，状态和密文结果持久化在 PostgreSQL；
- task request ID 幂等绑定，执行器采用分布式 lease/heartbeat，跨实例取消通过持久状态传播，过期 lease fail closed；
- Tool 运行故障返回标准 `CallToolResult.isError=true`，协议/参数故障返回 JSON-RPC error；
- 响应 receipt 绑定 request ID、output hash 与持久审计结果。

HTTP 授权端实现 RFC 9728 `/.well-known/oauth-protected-resource` 与 401 `WWW-Authenticate resource_metadata`；Keycloak JWT 必须同时通过签名、issuer、audience、时效、tenant claim 与 entity claim 校验。企业客户端采用 IdP 受管预注册，运行时不开放 Dynamic Client Registration。CIMD 属于 MCP 客户端与授权服务器的注册能力，只有 IdP 明确支持并通过互操作测试后才能声明启用，不能由资源服务器伪造“已支持”。

MCP 第一批能力严格限定为：

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

`NativeGlpiProvider` 与 `McpGlpiProvider` 必须通过同一组 Contract/Parity Tests。GLPI 搜索使用 High-Level API v2 的服务端 RSQL 过滤和有界结果，不允许只在最近若干记录中做客户端过滤；富文本在进入 Agent 前转换为有界纯文本。
标准客户端会自动获得只读上下文；只有显式声明 `com.servicemind/governed-execution`
能力的受管客户端才会看到 `submit_action_intent`，且调用仍不会直接产生 GLPI 副作用。

协议依据：[MCP 2026-07-28](https://blog.modelcontextprotocol.io/posts/2026-07-28/)、[MCP Tasks extension](https://tasks.extensions.modelcontextprotocol.io/specification/draft/tasks)、[GLPI RESTful API v2](https://help.glpi-project.org/documentation/modules/configuration/general/api/restful-api-v2)。

## 6.3 Policy-as-Code 与审计

生产路径固定使用 OPA/Rego，OPA 不可用、超时或输出非法时 fail closed；本地 mandatory policy 在调用 OPA 前继续执行，避免外部 PDP 配置错误取消最低安全控制。OPA 输入只携带身份、租户、角色、实体、工具元数据、风险、taint、审批引用和 argument hash，不发送 GLPI 参数正文或凭据。

Policy Decision 与 Invocation 分表记录，保存 policy version、OPA decision ID、tool checksum、argument/output hash、attempt、latency、verification 与稳定 error code。两张表强制 RLS、数据库 append-only trigger 和 hash/status check constraint；审计不保存 token、密码、工具正文或隐藏推理。

OPA 决策接口和决策日志能力依据官方 REST API：[OPA REST API](https://www.openpolicyagent.org/docs/rest-api)、[OPA Decision Logs](https://www.openpolicyagent.org/docs/management-decision-logs)。

## 6.4 可靠性与资源治理

- 审批记录与 `action.approved` outbox 在同一 PostgreSQL 事务提交；
- relay 使用 `FOR UPDATE SKIP LOCKED`、owner lease、visibility timeout、full-jitter backoff 和最大尝试次数；
- 失败终态保留在 PostgreSQL `dead` 状态，作为可审计 poison-task quarantine；
- Redis Streams 只传 event/tenant/aggregate/idempotency 引用，不传原始参数、结果或凭据；PostgreSQL 始终是权威源；
- consumer primitive 使用 consumer group、`XREADGROUP/XACK/XAUTOCLAIM`，业务 handler 必须幂等；
- 工具执行具有 deadline、只读有界重试、取消传播、per-tool circuit breaker、bulkhead 和分布式 tenant rate limit；
- API shutdown 会取消并收敛本进程 MCP task；异常退出由 task lease 过期和启动恢复收敛；
- outbox relay 由独立、仅属于本项目的 systemd 服务托管并支持 graceful shutdown。

Redis Streams 的 consumer group/Pending Entries List 语义以官方文档为准：[Redis Streams](https://redis.io/docs/latest/develop/data-types/streams/)。

## 6.5 Agentic Security

安全基线映射 OWASP Top 10 for Agentic Applications 2026、NIST AI RMF / Generative AI Profile 与企业 GLPI Policy，门禁覆盖 indirect prompt injection、tool misuse、identity/privilege abuse、goal manipulation、memory poisoning、excessive agency、cascading failure、不安全 inter-agent communication、敏感数据泄露与资源耗尽。

关键不变量：Memory < Skill < Policy；Skill 只能收窄能力；检索内容和 Memory 都是不可信数据；MCP/Native 共享同一 Policy 与 Audit；`submit_action_intent` 不能直接产生外部副作用；任何直接写工具注册、未绑定审批、高风险 fallback、跨租户资源或未解决 taint 都必须 fail closed。

## 6.6 执行路径与验收门禁

执行路径已按下列顺序落地：

1. P6.0 Contract freeze：Registry、schema、checksum、risk、provider parity；
2. P6.1 Security boundary：唯一 Gateway、OPA、tenant/RBAC/ABAC、approval binding、append-only audit；
3. P6.2 MCP：无会话 JSON-RPC、OAuth metadata/challenge、五项 GLPI 能力、Tasks durable lease；
4. P6.3 Reliability：transactional outbox、Redis relay/consumer primitive、rate limit/circuit/bulkhead/retry/cancel；
5. P6.4 Hardening：GLPI 服务身份与 High-Level API 可复现 bootstrap、迁移/RLS/密文/真实 HTTP/故障回归。

最终验收必须同时满足：全仓测试与 Ruff 无 error，ServiceMind 生产代码与本阶段验证器 Pyrefly 无 error；fresh DB `upgrade → downgrade base → upgrade`；在线 `alembic check` 零 drift；四张 Phase 6 表强制 RLS；审计 append-only；MCP 任务密文、租户隔离、幂等、lease/recovery/cancel 通过；真实 OPA、Redis、Keycloak、MCP、GLPI v2 链路通过；API/UI/outbox 和相关容器健康。通用 starter 模板、可选 UI 脚本和无关实例 Agent 的既存类型债务不计入 ServiceMind 生产门禁；任何一项业务范围门禁失败都不得标记 Phase 6 工程关闭。

Phase 6 的工程验收与业务质量认证分开。工具治理通过不改变 Phase 4 RAG 效果结论；没有 ITSM 专家签署和真实生产日志时，RAG 只能依据固定外部 silver 数据集与实际指标声明代理评测结果。

## 6.7 2026-09-14 执行与验收结果

Phase 6 已按 6.6 路径实际执行。当前 Alembic head 为 `0013_phase6_hash_guards`；
空库 upgrade → full downgrade → upgrade 通过；全仓自动测试 422 passed、6 skipped、0 failed；
ServiceMind 生产路径 Ruff、Pyrefly 与 Alembic drift 均为零错误。真实 OPA、Redis、PostgreSQL outbox、
Keycloak OIDC、MCP HTTP、GLPI v2、密文 Task lease 与独立 outbox worker 验证通过。

MCP 官方 conformance alpha 对产品实际支持面执行：`tools-list` 2/2，header 校验
12/13，stateless 21 success/5 skipped。剩余用例依赖官方套件专用的诊断 Tool、
本项目未声明的 Prompts/Subscriptions，或使用空参数调用必填 `ticket_id` 的业务 Tool；
不为追求测试数字暴露生产诊断工具或放宽 schema。详细证据见
`docs/PHASE6_ENTERPRISE_ACCEPTANCE.md`。

RAG 活跃索引已重建为 schema v3，39 文档、446 父块、668 子块，删除对账 0/0；
BGE-M3 与 Reranker 分配到两张 RTX 3090。100 候选重排 0.80 秒，实际端到端检索
5.54 秒，顶部证据正确命中 `runbook://rb-vpn-mfa`。外部 TechQA silver 的四个点估计都低于
租户参考阈值，但外部代理集无权裁定租户门禁；正式状态是 `BELOW_TARGET_DIAGNOSTIC`，
§4.1 六项仍为 `NOT_EVALUATED`。因此继续保留 `DOMAIN_QUALITY_NOT_CERTIFIED /
QUALITY_EXCEPTION_ACCEPTED`，不虚报为已获业务质量认证。`answerable_answer_rate=0.275`
是检索 top-score 阈值代理而非 Reviewer 端到端作答率，完整机器口径见
`evaluation/reports/rag_quality_status_latest.{json,md}`。

2026-09-22 完成项目结构收敛：根项目现为可构建、可安装的 `src` layout 模块化单体，
FastAPI 使用 `create_app` 组合根，Memory Review 位于独立 HTTP adapter；API、Streamlit、
outbox 三类进程清单全部版本化。领域完整性/调用身份、tokenizer、MCP transport、outbox
repository、dynamic planner 与 recovery 已归入各自权威层，`src/servicemind` 包级依赖环由
5 个降为 0。机器门禁为 `scripts/audit_project_structure.py --check`，详细裁定见
`docs/PROJECT_STRUCTURE_ENTERPRISE_AUDIT_2026-09-22.md`。该结构结论不评价项目外实例 Agent，
也不改变 RAG 的 `DOMAIN_QUALITY_NOT_CERTIFIED` 状态。

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

# 当前发布路径（2026-09-22 裁定）

Phase 3–6 的工程路径已经执行，Phase 6 也已有真实 OPA、Redis、PostgreSQL、Keycloak、MCP、
GLPI v2 与 outbox 验收，因此不存在“跳过 Phase 6 再做前端”的待决项。现在可以立即进入前端开发，
同时补齐 Phase 7 的最小发布门禁；两条工作流可并行，但正式企业生产发布不得跳过发布门禁。

前端路径冻结为：

1. 保留 Streamlit 作为内部演示和 Memory Review 运维入口；
2. 正式用户界面采用 React/Next.js + AG-UI，先实现 Workbench、Run/DAG、Evidence/Citations、
   Approval Center、Trace/Audit，再实现 Evaluation Dashboard；
3. 前端只能调用现有受治理 API，不复制 tenant/RBAC/ABAC、审批、Tool Policy 或 Memory Policy；
4. 所有写操作继续形成 `ActionIntent`，前端不能增加直接 GLPI 写旁路；
5. 浏览器端不得持有 service account、MCP service token 或数据库凭据，用户登录采用
   Authorization Code + PKCE。

发布分为两级：

- **内部 preview/demo**：前端 E2E、鉴权、跨租户隔离和关键路径通过后即可发布到受控网络；必须显式标记
  RAG 为 `DOMAIN_QUALITY_NOT_CERTIFIED`，不得宣传为租户业务质量已认证。
- **企业生产发布**：必须先关闭以下最小门禁，缺一项不得发布：
  1. 把当前工作树冻结为可追溯 commit/tag，由 clean checkout 构建 wheel 与镜像；
  2. CI 纳入结构审计、迁移往返、RAG 状态/回归、Memory、MCP contract、trajectory/security eval、
     Docker build、浏览器 E2E、SBOM、依赖与容器漏洞扫描；
  3. OTel traces、关键指标、结构化日志、告警和最小 SLO 可用，并验证审计中不出现 secret/token；
  4. Keycloak production mode、TLS reverse proxy、PKCE、生产 secret/KMS 和密钥轮换完成；
  5. PostgreSQL/Redis/OpenSearch/Neo4j 备份恢复演练、数据保留策略、负载与故障注入通过；
  6. 生产发布声明继续保留 Phase 4 质量例外，直到取得租户域标注集或真实流量证据。

因此当前最优顺序是“前端与 P7 发布门禁并行 → 受控 preview → 生产基础设施与恢复/负载验收 →
正式发布”，而不是重新执行 Phase 6，也不是完成页面后直接对外上线。
