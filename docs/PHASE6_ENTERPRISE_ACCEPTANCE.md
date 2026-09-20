# ServiceMind Phase 6 企业验收报告

> 版本：v1.0
> 执行与复核日期：2026-09-14
> 范围：Governed Tool Platform、MCP 2026-07-28、Reliable Execution、Agentic Security
> 结论：**ServiceMind 支持面工程验收通过**

## 最终裁定

Phase 6 已从计划转为实际交付。它只覆盖 ServiceMind 业务 Agent 链路；服务器上的通用示例 Agent、外部项目 Agent 和运行实例 Agent 不在本次修改与验收范围。

MCP 只是能力暴露与传输边界。Native 和 MCP 调用共享同一个 Tool Gateway、OPA policy、审批绑定、可靠性控制和审计，不存在第二套权限系统。生产不暴露直接 GLPI 写工具；`submit_action_intent` 只能创建受审 ActionIntent，仍需 Reviewer、HITL、幂等执行器和 read-back verification 才能形成外部副作用。

## 已执行路径

| 阶段 | 交付 | 状态 |
| --- | --- | --- |
| P6.0 Contract freeze | 不可变 Registry、Draft 2020-12 schema、semver、checksum、risk、provider parity | COMPLETE |
| P6.1 Security boundary | 唯一 Gateway、RBAC/ABAC/capability 交集、taint、OPA、审批绑定、append-only audit | COMPLETE |
| P6.2 MCP + GLPI v2 | 无会话 JSON-RPC、RFC 9728、Keycloak JWT、resources/tools、durable Tasks、GLPI High-Level API v2 | COMPLETE |
| P6.3 Reliability | PostgreSQL outbox、Redis metadata Stream、lease、重试、熔断、舱壁、限流、取消、幂等 | COMPLETE |
| P6.4 Hardening | 服务身份、RLS、密文、hash constraint、真实 HTTP/故障验证、独立 worker | COMPLETE |

## 架构与安全不变量

- Tool Registry 冻结 provider、输入输出 schema、读写类型、风险、数据分类、角色/实体、审批、超时、重试、限流、舱壁、幂等和验证策略。
- Gateway 依次执行 schema、tenant、RBAC/ABAC、能力交集、taint/secret/injection、OPA、审批、限流、熔断、舱壁、幂等、provider、输出 schema、read-back 和审计。
- OPA 不可用、超时或返回非法结构时 fail closed；本地 mandatory policy 始终先执行。
- 审计只保存 policy/tool/schema 版本和参数/输出哈希，不保存凭据、正文或隐藏推理；数据库强制 RLS、append-only 与哈希/状态约束。
- Redis Streams 只承载 event、tenant、aggregate 和 idempotency 引用；PostgreSQL 是唯一权威源，失败终态保留为 `dead`。
- MCP Task 使用数据库密文结果、request-id 幂等、分布式 lease/heartbeat、启动恢复和持久取消。
- 标准客户端只看到四个只读工具；显式声明 `com.servicemind/governed-execution` 的受管客户端才看到第五个 `submit_action_intent`。

## MCP 协议复核

实现固定到 MCP `2026-07-28`：protocol version 与 client capabilities 为必填；`clientInfo` 是 SHOULD，可省略，提供时必须通过 schema；`Mcp-Method`、`Mcp-Name` 和 body 双向核验；未知方法返回 404/`-32601`；header mismatch 返回 400/`-32020`；任务缺少能力时返回 400，工具运行错误使用 `CallToolResult.isError=true`。

官方 `@modelcontextprotocol/conformance@0.2.0-alpha.10` 结果：

| 场景 | 结果 | 解释 |
| --- | --- | --- |
| server-stateless | 21 SUCCESS，5 SKIPPED，4 NOT TESTABLE | 4 项要求生产不存在的 `test_missing_capability`、`test_streaming_elicitation`、`test_logging_tool`；5 项是未声明 subscriptions |
| tools-list | 2/2 SUCCESS | 标准客户端四个只读工具全部通过名称与结构校验；受管能力下为五个 |
| HTTP header validation | 12/13 可直接判定通过 | 剩余项的空白 header 已被接受，随后套件以 `{}` 调用必填 `ticket_id` 的业务工具，被正确返回 invalid params |
| caching | 5 SUCCESS，1 SKIPPED，1 unsupported | tools/resources/template 与 TTL/scope 通过；项目未声明 Prompts |
| JSON Schema 2020-12 | N/A | 场景硬编码要求名为 `json_schema_2020_12_tool` 的诊断工具；生产 Registry schema 已由项目测试独立验证 |

没有为了让套件数字更好看而暴露测试专用 Tool、虚构 Prompts，或放宽业务参数 schema。结论限定为“实际声明和支持的协议面通过”，不写成整个 alpha 套件无条件 100%。

## 验收证据

| 门禁 | 结果 | 证据 |
| --- | --- | --- |
| 全仓自动化 | PASS | 422 passed，6 skipped，0 failed，38 条第三方弃用 warning |
| Phase 6 专项 | PASS | 15 passed，0 failed |
| Phase 4–6 联合专项 | PASS | 89 passed，1 skipped，0 failed |
| 静态与 schema | PASS | Ruff 0 error；ServiceMind 生产路径 Pyrefly 0 error；Alembic drift 0 |
| 迁移往返 | PASS | 空库 upgrade → full downgrade → upgrade；head=`0013_phase6_hash_guards`；8 张 Phase 5/6 表 |
| 数据库治理 | PASS | 四张 Phase 6 表强制 RLS；审计 append-only；hash/status constraints |
| Tool live | PASS | 真实 OPA gateway、decision/invocation audit、receipt、Redis tenant rate limit |
| Outbox live | PASS | PostgreSQL transaction → lease relay → metadata-only Redis Stream |
| MCP/身份 live | PASS | RFC 9728 challenge、Keycloak OIDC、discover、五个受管工具、密文 durable Task |
| GLPI live | PASS | High-Level API v2 OAuth 服务身份、RSQL 服务端过滤、真实 ticket context |
| 常驻运行 | PASS | API、Streamlit、独立 `servicemind-outbox.service` active；依赖容器健康 |

## 边界与下一阶段

Phase 6 工程闭环不改变 RAG 业务质量结论。由于没有 ITSM 专家 gold set 和真实生产查询日志，Phase 4 仍为 `QUALITY_EXCEPTION_ACCEPTED`。质量不达标的具体归因、已修复的测量层缺陷与检索侧实测证据见 [`PHASE4_RAG_QUALITY_ROOT_CAUSE_2026-09-15.md`](PHASE4_RAG_QUALITY_ROOT_CAUSE_2026-09-15.md)。本阶段也不替代 Phase 8 的并发容量、长稳和灾备演练，因此不声明生产容量已认证。

下一步进入 Phase 7：把当前脚本证据固化为 CI release gate，增加 OTel/Langfuse 可观测性、离线/在线评测、红队安全集、轨迹质量、漂移检测和回滚门禁。执行顺序应为 P7.0 指标与 trace contract → P7.1 可观测栈 → P7.2 固定 eval harness → P7.3 red-team → P7.4 CI/release gate → P7.5 SLO 与验收。

## 签署

Phase 6 在 ServiceMind 实际支持面内达到当前工程冻结标准，验收范围内没有已知未修复缺陷。该结论基于固定自动化、真实基础设施和当前部署，不构成对未来供应商故障、未知攻击或未执行负载形态的绝对无缺陷保证。机器可读证据见 `evaluation/reports/phase6_acceptance_latest.json`。
