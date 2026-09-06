# Phase 3 企业级专业子 Agent 升级与验收

更新时间：2026-09-05

## 1. 本阶段边界

本阶段基于已经完成的动态 Supervisor 增量建设，没有新建第二套编排器，也没有回退到固定 T1～T5 工作流。

- `DynamicPlanner` 继续负责“LLM 提议 DAG + 确定性编译与校验”。
- Supervisor 继续独占控制面，通过 `DISPATCH`、`ANALYZE`、`REVIEW` 和 `HANDOFF_ACTION` 驱动专业子 Agent。
- Knowledge Agent 按计划保留，后续单独升级为企业 RAG。
- Action Agent 不获得外部写工具；所有副作用仍只能由审批后的 Harness 执行。

采用的基本原则是：模型负责受限提议，代码负责授权、边界、验证和副作用。

## 2. 现有 Runtime 的增量扩展

没有重建 Runtime。原有 `src/servicemind/runtime/structured.py` 继续作为 DeepSeek/其他 Provider 的结构化输出适配器，新增：

- `AgentInvocationContext`：不可变、无凭据的调用上下文，包含 tenant/run/task/trace/deadline/capability/model budget/tool budget/prompt version/policy version。
- `AgentResultEnvelope[T]`：Supervisor 与子 Agent 之间的统一类型边界。
- `AgentRunMetrics`：模型调用、工具调用、重试和延迟。
- `ToolInvocationRecord`：不含凭据和原始敏感参数的工具审计记录。
- `stable_digest()`：Review、Evidence、Action 上下文的规范化 SHA-256 摘要。
- `TenantGlpiReadGateway`：服务端租户配置解析和只读 GLPI 工具执行边界。

Agent Registry 的工具声明现在由 `supervisor_workflow._invocation()` 编译为实际 `allowed_capabilities`，不再只是文档型字符串。

## 3. Data Agent：受控 Plan-and-Execute 子图

### 子图

```text
START -> plan -> execute -> normalize -> END
```

### 实现

- DeepSeek 只提出 `DataAcquisitionPlan`，不能执行 HTTP，也不能看到 Token。
- 编译器强制 `ticket_id` 等于 Supervisor 选定资源。
- 编译器强制基础采集包含 Ticket 和租户 Support Groups。
- 允许的只读能力只有：
  - `glpi.read.ticket`
  - `glpi.read.groups`
  - `glpi.read.ticket_followups`
- 最大 6 次工具调用；默认最小计划 2 次。
- 429、可恢复 5xx 和 Timeout 最多重试一次；权限错误和业务 4xx 不重试。
- 模型规划失败时只能降级到固定只读最小计划，不能扩大权限。
- `TenantContext` 在 Gateway 中服务端注入，且在解析租户凭据前校验 tenant 一致性。
- GLPI 内容统一标记为 `UNTRUSTED_EXTERNAL_CONTENT`，并转换为带 provenance/content hash 的 Evidence。
- 工具 input hash、结果摘要、状态和尝试次数进入 `tool_invocations` 表与运行结果。

简单数据 Fast Path 保留无模型的确定性查询，避免为低价值请求消耗模型调用。

## 4. Analysis Agent：有界 Draft–Check–Revise 子图

### 子图

```text
START -> draft -> deterministic check
                       | pass
                       +------> END
                       | fail
                       +------> revise once -> check -> END
```

### 实现

- 输出新增 `AnalysisStatus`、`AnalysisClaim`、alternatives、unresolved questions 和 validation feedback。
- 每个 Claim 必须绑定 Evidence ID；Pydantic 拒绝 Claim 使用未声明证据。
- 确定性检查覆盖：Evidence 引用、Action allowlist、目标 Ticket、读请求不得提出写、写请求必须有受控提案、推荐组必须存在于租户 GLPI Evidence。
- 最多修订一次，总模型调用不超过 Invocation budget。
- 模型故障、预算耗尽或二次 Grounding 失败产生显式 `DEGRADED`，不再静默伪装成功。
- 降级结果置信度最高 0.49，并带 failure code；Reviewer 必须阻断自动 Handoff。
- Prompt 明确把 Evidence 当作不可信数据而不是指令，并禁止工具调用和隐藏思维链输出。

## 5. Reviewer Agent：Rule Gate + Semantic Judge + Adjudicator

### 子图

```text
START -> deterministic rule gate
              | blocking decision -> END
              | clean
              v
        semantic LLM judge -> deterministic adjudicator -> END
```

### 实现

- 原有硬规则全部保留，继续优先于模型。
- 独立 DeepSeek Semantic Judge 检查 Claim entailment、Action 一致性、矛盾和 Prompt Injection。
- Semantic Judge 没有工具，不能覆盖规则门禁。
- Judge 不可用或模型预算耗尽时 fail closed，返回 `ESCALATE + degraded`。
- 确定性 Adjudicator 映射到 `PASSED/RETRIEVE_MORE/REPLAN/REJECT/ESCALATE`。
- `ReviewResult` 新增：
  - `review_id`
  - `findings[]`
  - `reviewed_claim_ids[]`
  - `policy_version`
  - `reviewer_model`
  - `confidence`
  - `degraded`
- 模型校验器禁止 degraded 或带 error/critical finding 的 Review 被标为 PASSED。

Supervisor 耦合已同步修改：

- `supervisor_view` 只向控制面提供决策所需的 Review 摘要和 reason codes。
- `SupervisorPolicy.legal_actions()` 对 degraded Review fail closed，只允许 Escalate/Finalize。
- `review.completed` 审计事件记录 review ID、policy version 和 reason codes。
- Dynamic Replanner 继续消费完整结构化 Review feedback，且保留原有 completed-task identity 约束。

## 6. Action Agent 与 Harness：保持确定性并加强不可篡改绑定

当前只有一个允许操作，不引入自主 ReAct 写 Agent。Action Agent 仍是 credential-free deterministic compiler。

`ActionIntent v2` 的 hash 绑定：

- tenant ID
- requester
- operation、target 和 canonical arguments
- evidence refs 与 evidence digest
- review digest
- policy version
- idempotency context
- expires_at

Harness 在任何 GLPI 写入前强制执行：

1. Intent 已持久化；
2. Tenant 与执行上下文一致；
3. Intent 未过期；
4. 完整性 hash 重新计算通过；
5. 幂等 claim 与跨副本数据库锁；
6. GLPI 写前 reconciliation；
7. 写后读取校验。

Action 仍然没有 `GlpiClient`、CredentialCipher 或写工具；真实写入只能发生在 Harness。

## 7. Supervisor 驱动方式

```text
Dynamic Supervisor
  -> DISPATCH -> Data Agent subgraph / Knowledge Agent
  -> JOIN_EVIDENCE
  -> ANALYZE -> Analysis Agent subgraph
  -> REVIEW -> Reviewer Agent subgraph
  -> RETRIEVE_MORE / REPLAN -> DynamicPlanner revision
  -> HANDOFF_ACTION -> Action compiler
  -> Human approval
  -> Harness
```

专业子 Agent 不是另起独立 workflow。每次执行都由动态 DAG 中的 ready task 和 SupervisorDecision.selected_task_ids 驱动，完成后回写 TaskPlan 状态，再把控制权交回 Supervisor。

## 8. 测试与真实验收

新增自动化测试覆盖：

- 模型企图修改 Ticket ID 时被编译器拒绝并安全降级。
- 跨租户 Tool Gateway 请求在凭据解析前被拒绝。
- Data Tool budget、capability allowlist 和审计记录。
- Analysis 缺 Claim 时只修订一次并完成 Grounding。
- Analysis 模型失败/预算耗尽显式降级。
- Reviewer 硬门禁优先于 Semantic Judge。
- Reviewer 模型不可用时 fail closed。
- ReviewResult schema 与 Supervisor legal-action 映射。
- ActionIntent v2 审后参数篡改检测。
- 旧 Phase 2 兼容路径及旧 hash v1 行为。

2026-09-05 真实环境验收结果：

- GLPI 11.0.8、PostgreSQL 16、Keycloak 26.7.3 容器健康。
- 本地 FastAPI 与 Streamlit 已重启加载新代码。
- 全部 Fast Path、复杂读、Retrieve-More、受控写、越权拒绝和持久恢复场景通过。
- 真实复杂读运行：`4148c3be-9c14-40db-b611-dfe393c86091`。
- 真实受控写运行：`383ceb1d-ae9e-4034-bd78-43f5c555e2e9`。
- 写入 GLPI 私有 Followup ID：`23`。
- 真实结果包含 4 个 claim-level citations。
- Data invocation 记录模型规划和 2～3 个只读工具调用。
- Reviewer 使用 `deepseek-v4-flash` 和 `servicemind-review-policy-v2`。
- 100 条路由评测准确率为 1.0。
- 跨进程 Checkpoint 恢复没有重复已完成的 Evidence 分支。

验收原始报告：`evaluation/reports/phase3_e2e_latest.json`。

## 9. 仍然明确不在本阶段伪装完成的能力

- Knowledge Agent 尚不是企业 RAG；后续需要向量/关键词混合检索、rerank、ACL、版本与删除传播、citation evaluation。
- MCP 尚未放入 ServiceMind Tool Gateway；后续只能作为 Provider adapter，不能绕过 capability、tenant 和 audit。
- 当前 Action operation 只有 private Followup；Problem/Change/Assignment 等操作必须逐项建立风险策略、审批和补偿语义后才能开放。
- 当前没有声称使用无界 reflection、swarm 或 tree search；这些模式不适合受控 ITSM 写链路。

## 10. 主要文件

- `src/servicemind/runtime/contracts.py`
- `src/servicemind/runtime/structured.py`
- `src/servicemind/runtime/tool_gateway.py`
- `src/servicemind/agents/data.py`
- `src/servicemind/agents/analysis.py`
- `src/servicemind/agents/reviewer.py`
- `src/servicemind/agents/action.py`
- `src/servicemind/domain/analysis.py`
- `src/servicemind/domain/review.py`
- `src/servicemind/domain/handoff.py`
- `src/servicemind/domain/models.py`
- `src/servicemind/orchestration/supervisor_workflow.py`
- `src/servicemind/orchestration/supervisor_policy.py`
- `src/servicemind/persistence/repository.py`
- `src/servicemind/harness/executor.py`
- `tests/servicemind/test_enterprise_subagents.py`

## 11. 设计依据

- LangChain Multi-agent patterns: <https://docs.langchain.com/oss/python/langchain/multi-agent/index>
- LangGraph Custom workflows: <https://docs.langchain.com/oss/python/langchain/multi-agent/custom-workflow>
- LangChain Tools and runtime context: <https://docs.langchain.com/oss/python/langchain/tools>
- LangChain Structured output: <https://docs.langchain.com/oss/python/langchain/structured-output>
- LangChain Guardrails: <https://docs.langchain.com/oss/python/langchain/guardrails>
- LangGraph Fault tolerance: <https://docs.langchain.com/oss/python/langgraph/fault-tolerance>
