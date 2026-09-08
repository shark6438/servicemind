# ServiceMind Phase 5 最终架构与验收签署

> 版本：v1.0  
> 签署日期：2026-09-08  
> 范围：Governed Memory、Context Engineering、Versioned Skills、Model Gateway  
> 结论：**工程验收通过，生产配置已启用**

## 1. 最终裁定

Phase 5 冻结为四个独立模块，并以共享治理合约连接。Memory 不是第六个 Agent，不参与 Action 执行控制；LangGraph checkpoint 仍负责短期运行状态，长期 Memory 单独治理；RAG 继续作为官方知识和 SOP 权威来源。

原方案经过审计后作了四项必要修订：

1. Model Gateway 提前成为所有结构化模型调用的唯一入口，覆盖 Supervisor、Planner、Data、Knowledge Query Rewrite、Analysis 与 Reviewer；
2. Preference 是 Semantic Memory 的受同意子类型，不新增互相重叠的第四种顶层存储类型；
3. Context Builder 的输出成为子 Agent 模型输入边界，修复原始 evidence 绕过脱敏和 token 裁剪的问题；
4. Memory 读取接入已固定 revision 的本地 BGE-M3 向量服务，采用有界候选与派生向量缓存，PostgreSQL 仍是唯一权威记录。

Procedural Memory 只能由至少两个已验证 episode 与证据提出，并且必须带人工审核引用才能激活。Skill 的能力始终取 Agent、用户、租户与工具策略的交集，不能扩权、跳过 Reviewer、绕过 HITL 或直接写永久 Memory。

## 2. 已交付架构

### 2.1 Governed Memory

- 支持 Semantic（learned fact / consented preference）、Episodic、Procedural；
- 候选依次经过 secret、PII/consent、taint、evidence、final state、importance、confidence 和 conflict 门禁；
- 支持 candidate、quarantine、active、superseded、revoked、expired 生命周期及合法转换；
- 并发写入通过事务级 advisory lock 串行化，同内容跨 replay/run 去重，冲突版本进入 quarantine；
- 读取先执行 tenant、scope、status、validity、expiry 过滤，再进行 BGE-M3/词面、时效、置信、重要度、来源联合排序，返回前再次校验权限；
- evidence 撤销会同步撤销依赖 Memory；post-run writer 只处理成功且最终状态已验证的运行。

### 2.2 Context Engineering

- Data、Knowledge、Analysis、Reviewer、Action 分别拥有独立来源 allowlist；
- 每项上下文包含 provenance、trust、taint、authority、relevance 与内容哈希；
- token 预算预留 system/output 空间，按 required、authority、relevance、recency 确定性选择；
- secret、PEM、AWS key、Bearer、JWT、数据库凭据、邮箱和中国手机号在模型边界前脱敏；
- 未解决的 prompt injection、secret、cross-tenant taint 直接拒绝；
- 审计表只保存输入哈希、选择清单、预算和脱敏计数，不保存提示词正文或隐藏推理。

### 2.3 Versioned Skills

- 已交付 incident-triage、recurring-problem、change-risk、vpn-mfa、major-incident 五个 Skill；
- manifest 包含 semver、scope、agent、required tools、risk、输入输出 schema、证据要求、测试和 checksum；
- registry 先发现 metadata，命中后才读取正文，并校验路径、符号链接、大小、checksum 与危险绕权指令；
- effective capabilities 只能收窄现有权限。

### 2.4 Model Gateway

- 所有 ServiceMind structured output 已由 AST 门禁证明只从统一 Gateway 发起；
- provider/model 采用全局与租户 allowlist 交集，配置租户映射后，未登记租户 fail closed；
- timeout、429、5xx、非法 JSON、schema mismatch 和 provider unavailable 均有受限重试和确定终态；
- 高风险 Reviewer 禁止降级 fallback；semantic cache 对高风险、Reviewer、Memory Extraction、个人/易变输入关闭，当前生产全局关闭；
- 审计包含 provider/model revision、prompt/schema hash、provider token、延迟、尝试、fallback、成本和计价版本；不落提示词正文。
- 每次生产模型调用设 0.05 USD 硬上限，超限结果记为 `MODEL_COST_BUDGET_EXCEEDED` 并拒绝返回。

## 3. 数据库与隔离

当前 Alembic head 为 `0009_model_accounting_provenance`。四张 Phase 5 表 `memory_records`、`memory_events`、`model_invocations`、`context_artifacts` 均启用并强制 PostgreSQL RLS。零 tenant context 查询返回零行；跨租户查询被拒绝；Memory event、Model invocation 与 Context artifact 由数据库触发器强制 append-only。

完整 migration 链已在一次性空库执行 `upgrade head -> downgrade base -> upgrade head`。Alembic autogenerate drift 为零；LangGraph 自有 checkpoint/store 表通过明确 ownership 过滤，不会被 ServiceMind migration 错误删除。

## 4. 验收结果

| 门禁 | 结果 | 证据 |
| --- | --- | --- |
| 全仓自动化 | PASS | 389 passed，6 skipped，0 failed；6 项带 Docker 标记 |
| Phase 5 专项 | PASS | 25 项 Memory/Context/Skill/Gateway 对抗与故障测试 |
| Docker 权威层 | PASS | 与 ServiceMind 相关的 PostgreSQL job/RLS 与 Neo4j projection 两项独立执行，2 passed |
| 范围排除 | PASS | 其余 4 项指向 8080 端口通用 fake `chatbot` 模板服务，属于无关 Agent/实例测试，未纳入 ServiceMind 验收 |
| 静态检查 | PASS | Ruff 全仓 0 error；Pyrefly 0 error |
| Schema 漂移 | PASS | `alembic check`：No new upgrade operations detected |
| 全新数据库迁移 | PASS | upgrade → base downgrade → upgrade，head=0009，Phase 5 tables=4 |
| 在线数据库治理 | PASS | forced RLS、zero-context denial、tenant isolation、并发幂等、BGE-M3 排序、append-only |
| 真实模型网关 | PASS | DeepSeek structured result、tenant route、provider token、cost provenance、durable audit |
| 真实工作流 | PASS | Router → governed Knowledge Context → RAG → Model Gateway → PostgreSQL audit |
| API readiness | PASS | 历史运行后台恢复不阻塞服务；重启后 8.3 秒就绪，鉴权 `/info` 返回 ServiceMind 项目注册表内 10 Agents / 2 Models |
| 数据集完整性 | PASS | Phase 4 manifest v1.2；TechQA 910、EnterpriseOps 四组各 103、AgentDojo archive hash |

全量测试中的第三方弃用 warning 共 38 条，来自 LangGraph Supervisor、AG-UI、Starlette/httpx 和 langchain-community；它们没有形成当前功能失败，但应在依赖升级周期内消除。

## 5. 生产配置

服务器 `.env` 已启用：

- Governed Memory 与 BGE-M3 vector ranking；
- Context Builder；
- 五个 Skills；
- Model Gateway durable audit；
- ACME、GLOBEX 两个现有租户的 DeepSeek Flash 显式 allowlist。
- 单次模型调用成本硬上限 0.05 USD。

Semantic cache 保持关闭。任何新增租户在加入模型 allowlist 前无法调用模型，属于预期的 fail-closed 行为。
Agent API 与 Streamlit 分别由 `servicemind-api.service`、`servicemind-streamlit.service` 用户级 systemd 单元托管，均设置为 enabled、restart-on-failure，并启用 user linger。API 与 UI 重启后的健康端点均返回 200；启动时的历史运行恢复作为受 lifespan 管理的后台任务执行，不再阻塞 API readiness。
本次 Agent 验收范围严格限定为 ServiceMind 仓库注册表及其编排链路，不包含服务器上的其他项目、外部服务或运行实例 Agent。

## 6. Phase 4 边界

Phase 4 可以确认的是工程闭环和可复现代理评测，不是企业业务质量认证。没有专家签署和真实生产查询日志时，现有 TechQA/EnterpriseOps/AgentDojo 只能作为最方便、权威且可复现的替代证据。`phase4_proxy_release_latest` 的高质量阈值仍未全部通过，因此 Phase 4 保留 `QUALITY_EXCEPTION_ACCEPTED`，不能因 Phase 5 通过而改写为“RAG 已达到企业前沿质量”。

## 7. 签署

Phase 5 的代码、迁移、生产配置与可获得的在线基础设施证据达到本阶段冻结标准；验收范围内没有已知未修复缺陷。该结论不等同于对所有未来输入、供应商故障或尚未发生的生产负载作绝对无缺陷保证。详细机器可读证据见 `evaluation/reports/phase5_acceptance_latest.json`。
