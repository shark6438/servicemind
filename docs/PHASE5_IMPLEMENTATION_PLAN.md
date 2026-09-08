# ServiceMind Phase 5 实施计划

> 版本：v1.1（最终冻结版）
> 制定日期：2026-09-08
> 范围：Memory、Context、Skills、Model Gateway
> 前置决定：`docs/PHASE4_ENGINEERING_CLOSURE_V1_2.md`
> 执行状态：已实现并通过工程验收；证据见 `docs/PHASE5_FINAL_ARCHITECTURE_AND_ACCEPTANCE.md`

## 1. 目标与约束

Phase 5 将现有 1+5 Multi-Agent 从“每个节点自行组织模型输入”升级为统一的上下文和模型治理层，同时加入可追溯、可撤销、受证据约束的企业记忆与版本化 ITSM Skills。

本阶段复用现有 PostgreSQL RLS、LangGraph checkpoint、Evidence Contract、Tool Gateway、OpenSearch 和审计事件。不会新增第二套 workflow 真相源，也不会把 Memory Writer 实现成第六个 Agent。MCP、Redis Worker、OPA 和完整红队平台仍按总路线保留在 Phase 6/7。

## 2. 目标架构

```text
Supervisor / Sub-Agent
  -> Context Builder
       -> task + selected state
       -> ACL-filtered evidence
       -> ACL-filtered active memory
       -> selected skill metadata/body
       -> allowed tool schemas
       -> token/cost envelope
  -> Model Gateway
       -> tenant/model/purpose policy
       -> structured output
       -> timeout/retry/fallback
       -> usage + cost + prompt version audit
  -> Agent result
  -> existing Reviewer / Action Harness
  -> post-run Memory Writer middleware
       -> evidence/consent/PII/secret checks
       -> exact + semantic dedup
       -> conflict/version/TTL policy
       -> activate or quarantine
```

## 3. 实施切片

### P5.0 合约、Schema 与观测基线

先冻结四个核心合约：

- `MemoryRecord` / `MemoryCandidate` / `MemoryDecision`；
- `ContextEnvelope` / `ContextItem` / `ContextBudget`；
- `SkillManifest` / `ResolvedSkill`；
- `ModelRequest` / `ModelResult` / `ModelRouteDecision`。

PostgreSQL migration `0008` 与 `0009` 已覆盖：

- `memory_records`：tenant、scope、type、content、source trace、evidence refs、confidence、importance、version、validity、TTL、status、creator、content hash、taint；
- `model_invocations`：run/task/agent、provider/model、route reason、prompt/template version、input/output token、latency、cost、retry/fallback、status；
- `context_artifacts`：只保存必要的内容哈希、选择原因、token 分配和外置对象引用，不保存模型隐藏推理。

所有 tenant 表启用并强制 PostgreSQL RLS；append-only 审计记录 activate、quarantine、supersede、revoke、expire 和 route/fallback 决策。

### P5.1 Model Gateway

建立一个所有 Agent 共用的模型入口，迁移 Planner、Analysis、Reviewer、query rewrite 和后续 Memory Writer 的直接模型调用。

核心规则：

- 路由键包含 `tenant_id + agent_role + purpose + risk + required_capabilities`；
- 模型和 provider 必须同时命中 tenant allowlist；
- Structured Output 统一走 schema 验证，修复重试有独立上限；
- 超时、限流和 5xx 只在幂等模型请求内重试；
- fallback 只能降到满足同一 schema、数据边界和风险等级的模型；高风险评审没有合格 fallback 时显式升级；
- 每次调用记录模型 revision、prompt/template version、token、成本、延迟和 fallback 原因；
- semantic cache key 至少含 tenant、agent role、purpose、policy version、model revision、prompt/template hash、tool schema hash 和规范化输入 hash；安全/写动作判断默认不缓存。

先接入当前 DeepSeek provider，再保留兼容 OpenAI-style endpoint 的 provider adapter。Phase 5 不以 provider 数量作为完成标准。

### P5.2 Context Builder

为五个子 Agent 分别声明输入 allowlist，统一构造 `ContextEnvelope`：

- Data：任务、ticket/entity 标识、只读 tool schema、最小状态；
- Knowledge：规范化 query、ACL、检索配置，不接收写工具或无关对话；
- Analysis：已 join 的 evidence、适用 memory/skills、诊断 schema；
- Reviewer：proposal、evidence、citation、policy 和预算，不接收可执行 credential；
- Action：已通过 review 的 ActionIntent 所需最小字段和允许工具。

Context Builder 实现确定性 token 预算：系统/合约保留额、任务额、evidence、memory、skills、tool schema 和输出预留分别计账。超限时按来源权威、相关性、新鲜度和任务必要性裁剪；大工具结果外置并只传摘要、hash 和可审计引用。所有输入保留 provenance/taint，PII/secret 在模型边界前按 agent purpose 脱敏。

### P5.3 企业 Memory 与 Writer Middleware

支持四种记忆：

- Semantic：稳定、经证据支持的域事实；
- Episodic：已完成且 Reviewer/最终状态验证通过的历史处置；
- Procedural：指向有效 Runbook/流程版本，不复制为无来源事实；
- Preference：仅保存用户明确允许持久化的输出偏好。

Writer 只在 run 到达受认可终态后运行，顺序固定为：候选提取 → evidence/最终状态验证 → 保存价值判断 → PII/secret/taint 检查 → exact/semantic dedup → conflict 检测 → version/TTL → activate 或 quarantine。

关键策略：

- 网页、检索文档、用户消息和 tool 输出本身都不能直接写成 active enterprise fact；
- Semantic/Procedural 必须绑定可追溯 evidence；Episodic 必须绑定成功 run 和 final-state verifier；Preference 必须绑定 consent event；
- 高冲突、低置信、含未清除 taint 或来源失效的候选进入 quarantine；
- source/evidence 被撤销或过期时，依赖 memory 自动失效或重新审核；
- exact dedup 使用规范化 hash，semantic dedup 只生成合并建议，不能覆盖来源和版本冲突；
- 写入使用确定性 idempotency key，checkpoint replay 不产生重复记忆。

Memory retrieval 与 RAG 一样在检索前执行 tenant/scope/status/time ACL，返回结果再次做 PostgreSQL 权威校验。

### P5.4 版本化 ITSM Skills

建立项目内 registry：

```text
skills/
├── incident-triage/SKILL.md
├── recurring-problem/SKILL.md
├── change-risk/SKILL.md
├── vpn-mfa/SKILL.md
└── major-incident/SKILL.md
```

每个 manifest 包含 skill id、semver、checksum、tenant/global scope、allowed agents、required tools、risk class、输入/输出 schema、证据要求和测试样本。加载分为 metadata discovery 与命中后的正文加载，避免每次把全部 Skill 塞入上下文。

有效工具集合始终按以下交集计算：

```text
Agent contract ∩ tenant policy ∩ runtime risk policy ∩ skill required tools
```

Skill 可以收窄工具和提高审查等级，不能扩权、绕过 Reviewer/HITL、修改系统 policy 或把自身内容写入永久 Memory。checksum、scope 或 schema 不合法时 fail closed。

### P5.5 集成、迁移与发布门

按 `Planner -> Data/Knowledge -> Analysis -> Reviewer -> Action` 的调用顺序接入 Gateway 和 Context Builder，再启用只读 Memory retrieval，最后启用 post-run Writer。每一步保留 feature flag 和旧路径对照，完成对应测试后再切下一步；同一 run 不混用两种上下文合约版本。

首批 rollout：

1. Reviewer 和 Analysis 先接 Model Gateway，验证 schema/fallback/计费；
2. Knowledge/Data 接 Context Builder，只读观测裁剪结果；
3. Preference Memory 小范围启用；
4. Semantic/Episodic 先 quarantine-only，人工抽查可用时再允许自动激活的低风险规则；
5. 五个内置 Skills 逐个启用；
6. 全链 feature flag 开启并冻结 Phase 5 报告。

## 4. 必须通过的测试

| 维度 | Phase 5 关闭门 |
| --- | --- |
| Memory tenant/scope/RLS 泄漏 | 0 |
| 无 evidence/无 consent/含 secret 的候选进入 active | 0 |
| source 撤销后依赖 memory 仍可见 | 0 |
| replay/并发产生重复 active memory | 0 |
| Context 超预算调用 | 0；每次记录预算分配和裁剪原因 |
| 不允许字段进入某 Agent 的 ContextEnvelope | 0 |
| Skill 扩权、绕过 Reviewer/HITL | 0 |
| 不合规 fallback 或跨 tenant semantic-cache 命中 | 0 |
| Structured Output 故障矩阵 | timeout、429、5xx、非法 JSON、schema mismatch、provider unavailable 均有确定终态 |
| 现有 RAG/Agent 回归 | 不低于 Phase 4 自动化基线；当前全仓 389 项通过且不得新增失败 |
| TechQA 外部代理回归 | 同一 400 条、同一模型与配置下不得低于 Phase 4 五项指标；它仍是外测，不升级为租户金标 |

另外执行跨 tenant × scope × memory type × status × validity 的确定性矩阵、恶意 Skill fixture、prompt/tool taint 传播、TTL/版本冲突、checkpoint replay 和双并发写入测试。

## 5. 交付物

- `src/servicemind/model_gateway/`：contracts、policy、providers、routing、accounting、cache；
- `src/servicemind/context/`：builder、ranking、budgeting、redaction、offload；
- `src/servicemind/memory/`：contracts、repository、retrieval、writer、policy；
- `src/servicemind/skills/` 与根目录 `skills/`：registry、loader、resolver 和五个 ITSM Skills；
- 对应 migration、Settings、审计事件、feature flags 和回归测试；
- `evaluation/reports/phase5_*`：model routing、context budget、memory safety、skill policy、端到端回归与最终关闭报告。

## 6. 推荐执行顺序

| 周期 | 工作包 | 完成标志 |
| --- | --- | --- |
| 1 | P5.0 + P5.1 | 合约/schema 冻结；现有模型调用统一经过 Gateway |
| 2 | P5.2 | 五个 Agent 的 ContextEnvelope、预算、隔离、脱敏接通 |
| 3 | P5.3 | Memory RLS、检索、quarantine-first Writer、TTL/冲突/回放完成 |
| 4 | P5.4 + P5.5 | 五个 Skills、全链集成、故障矩阵、回归和关闭报告完成 |

这是已经执行完毕的依赖顺序，不是日历承诺。最终实现保留独立 feature flag；生产配置已启用 Memory、Memory Vector、Context、Skills 与 Model Gateway 审计，semantic cache 仍关闭。
