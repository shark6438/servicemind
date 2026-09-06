# ServiceMind Phase 3 — Supervisor Runtime 企业验收报告

> 验收日期：2026-09-05  
> 最终运行图：`src/servicemind/orchestration/supervisor_workflow.py`  
> Supervisor 模型：`deepseek-v4-flash`  
> 当前状态：**PHASE 3 COMPLETE**

## 1. 纠正背景

早期 Phase 3 实现虽然具备 Evidence、DAG Validator、并行 Agent、Reviewer、Budget 和安全 Harness，但控制流由固定 LangGraph edges 决定：

```text
plan → dispatch → data/knowledge → join → analysis → reviewer
```

代码里没有独立 Supervisor Agent；`supervisor` 只是 trajectory 标签；Handoff 只是静态 `handoff → action` 边。这不符合“1 Supervisor + 5 Agents、动态 Task DAG、Agent Loop、Bounded Replan、真实 Handoff”的任务定义。

本次整改没有通过改文档掩盖问题，而是：

1. 将旧报告标记为 SUPERSEDED；
2. 删除旧固定 Phase 3 Graph、固定 Planner、旧 Graph 专属测试和旧 durable 脚本；
3. 接入真实 DeepSeek V4 Flash；
4. 新建 SupervisorDecision、SupervisorAgent、SupervisorPolicy；
5. 新建动态 Planner 和受保护的 Plan Revision；
6. 将所有专业 Agent 返回真实 Supervisor Node；
7. 使用 Supervisor `Command` 控制下一节点；
8. 使用 control_owner 完成 Supervisor → Action → Human → Harness 权限转移；
9. 重建 E2E、恢复、安全和报告证据。

## 2. 最终架构

```text
                           Fast Path Router
                   ┌─────────────┴─────────────┐
                   │                           │
            simple data/knowledge          COMPLEX
                   │                           │
                   ▼                           ▼
                  END                    Supervisor Agent
                                            DeepSeek V4
                                                 │
                                      Structured SupervisorDecision
                                                 │
                                      Deterministic SupervisorPolicy
                                                 │
       ┌───────────────┬───────────────┬─────────┼──────────┬───────────────┐
       ▼               ▼               ▼         ▼          ▼               ▼
     PLAN           DISPATCH          JOIN     ANALYZE     REVIEW          REPLAN
       │               │               │         │          │               │
 Dynamic Planner   Ready Tasks      Evidence  Analysis   Reviewer      Plan Revision
       │               │              Join      Agent      Agent           │
 DAG Validator    Send fan-out          │         │          │               │
       │          Data/Knowledge        └─────────┴──────────┴───────────────┘
       │               │                              │
       └───────────────┴──────────────────────────────┘
                              every result returns to Supervisor
                                                 │
                                     ReviewResult == PASSED
                                                 │
                                      SupervisorDecision.HANDOFF_ACTION
                                                 │
                                      SupervisorPolicy + HandoffEnvelope
                                                 │ Command(goto="action")
                                                 ▼
                             control_owner: supervisor → action
                                                 │
                                             Action Agent
                                                 │ ActionIntent only
                                                 ▼
                               control_owner: action → human → harness
                                                 │
                                    Existing Phase 2 Harness
                                                 │
                                  idempotency / lock / GLPI / verify
```

## 3. DeepSeek V4 Flash 接入

官方模型 ID：

```text
deepseek-v4-flash
```

Base URL：

```text
https://api.deepseek.com
```

API Key 只保存于 Git 忽略的 `.env`，未写入源码、报告、Trace、Checkpoint、RunEvent 或 Audit。

运行配置：

```text
temperature=0
thinking=disabled
streaming=false for control-plane calls
```

原因：Supervisor/Planner 是控制面，需要稳定 JSON、低随机性和可重复验证，而不是创意生成。

### Structured Output 兼容处理

首次真实探针发现 DeepSeek 当前拒绝 LangChain 默认 `json_schema` response format：

```text
400: This response_format type is unavailable now
```

最终 Provider Adapter 使用：

```text
JSON Object mode
+ explicit JSON Schema in system instruction
+ Pydantic validation
+ deterministic policy validation
+ bounded correction retry
```

验证结果：

```text
connectivity: PASS
json_object structured output: PASS
SupervisorDecision parsing: PASS
PlanProposal parsing: PASS
PlanRevisionProposal parsing: PASS
```

## 4. 真实 Supervisor Agent

文件：

```text
src/servicemind/agents/supervisor.py
src/servicemind/domain/supervisor.py
src/servicemind/orchestration/supervisor_policy.py
```

Supervisor 有独立：

- Agent class；
- Contract；
- Graph Node；
- Pydantic input view；
- Pydantic output；
- model invocation；
- OTel/RunEvent 关联；
- policy correction loop；
- budget accounting。

Supervisor Contract：

```text
allowed_tools = []
read_write_scope = control_only
forbidden = GLPI write / credentials / HTTP / database
```

Supervisor 不能：

- 读取或修改 GLPI；
- 检索知识；
- 自己分析 Incident；
- 自己审核；
- 持有写工具；
- 绕过 Reviewer；
- 绕过 Phase 2 Harness。

## 5. SupervisorDecision

允许动作：

```text
PLAN
DISPATCH
JOIN_EVIDENCE
ANALYZE
REVIEW
RETRIEVE_MORE
REPLAN
HANDOFF_ACTION
ESCALATE
FINALIZE
```

输出包含：

```text
action
selected_task_ids
rationale_summary
confidence
```

`rationale_summary` 是业务控制说明，不保存或请求 Chain-of-Thought。

## 6. Agent 决策与确定性规则分层

最终模式：

```text
Supervisor LLM proposes
        ↓
SupervisorPolicy validates
        ↓
valid   → Command(goto=...)
invalid → sanitized feedback → Supervisor retries
        ↓
two invalid attempts → fail closed
```

确定性 Policy 只负责安全不变量：

- action 必须在当前 legal actions；
- DISPATCH 只能选择 Ready Data/Knowledge Tasks；
- selected IDs 唯一且不超过 max_parallel；
- ANALYZE/REVIEW 必须选择恰好一个对应 Ready Task；
- Reviewer PASSED 前不能 HANDOFF；
- 非调度动作不能携带无关 Task IDs；
- Budget/Deadline/Loop 限制不能被 LLM 修改。

业务控制决策由 Supervisor 产生，而不是用字符串标签伪造。

## 7. Policy 自我纠正实证

真实写流程 Run：

```text
0f515b23-e5e7-424d-80a9-da4f3e264f00
```

Supervisor 第一次输出：

```text
HANDOFF_ACTION selected_task_ids=[T5]
```

Policy 拒绝：

```text
handoff_action must not select dispatch task IDs
```

Supervisor 收到反馈后第二次输出：

```text
HANDOFF_ACTION selected_task_ids=[]
```

随后才产生 `control.handoff`。这证明不是代码静态替换模型决策，而是模型提议、Policy 拒绝、模型自我纠正的真实循环。

## 8. 动态 Planner

文件：`src/servicemind/agents/dynamic_planner.py`

Planner 输入：

- goal；
- ticket ID；
- request_write；
- Agent capability catalog；
- Registry 中允许的 task types；
- validation correction。

Planner 输出 `PlanProposal`，再由运行时：

1. 绑定 Deadline/Budget；
2. 从 Agent Contract 绑定 ErrorPolicy；
3. 构造 Task/TaskPlan；
4. 执行 Registry 校验；
5. 执行 DAG Validator；
6. 验证写流程必须包含 Action；
7. 验证 Action 必须有 Reviewer ancestor。

### 不同目标真实生成不同 DAG

事实分析目标：

```text
T1 data
T2 data depends T1
T3 analysis depends T1,T2
T4 reviewer depends T3
```

知识辅助写目标：

```text
T1 data
T2 knowledge
T3 analysis depends T1,T2
T4 reviewer depends T3
T5 action depends T4
```

这两个 Plan 均由真实 DeepSeek 调用产生，不是测试 fixture 或固定模板。

## 9. 动态 Dispatch

Supervisor State View 暴露：

- legal actions；
- ready tasks；
- agent/task type；
- dependency status；
- max_parallel；
- evidence count/dirty；
- analysis/review；
- remaining control budget。

Supervisor 在 `DISPATCH` 中选择 Ready Task IDs。Dispatcher 再执行：

```text
PENDING → READY → RUNNING
```

通过 LangGraph `Send` 动态创建 Data/Knowledge 并行分支。分支完成后 Barrier 更新：

```text
SUCCESS / FAILED
output refs
attempts
tool/model counters
evidence_dirty
```

所有分支完成后返回 Supervisor，不沿固定业务边自动前进。

## 10. 真实 Supervisor Loop

最终 Complex Read Run：

```text
64d7b8fa-faed-4ae3-a415-e6f1da80b022
```

统一验收记录 Supervisor Decisions：

```text
8 control decisions
```

典型轨迹：

```text
router
→ supervisor / PLAN
→ planner / dag_validator
→ supervisor / DISPATCH
→ dispatcher / Data + Knowledge
→ dispatch_barrier
→ supervisor / JOIN_EVIDENCE
→ evidence_join
→ supervisor / ANALYZE
→ analysis
→ supervisor / REVIEW
→ reviewer
→ supervisor / FINALIZE
```

这里的每个 `supervisor` 都对应真实 Node 调用和真实模型请求，不再手工写标签代替。

## 11. 动态 Plan Revision

最终 Retrieve-More Run：

```text
aafd9818-4a55-42e9-b2fd-374ebda0ecd6
```

另一条真实验证 Run `390070e3-3825-4d57-a9c1-487ec6b11144` 展示完整轨迹：

```text
initial data-only plan
→ Data tasks
→ Analysis
→ Reviewer RETRIEVE_MORE
→ Supervisor RETRIEVE_MORE
→ PlanRevision
→ preserve completed T1-T4
→ add Knowledge T5/T6
→ add Analysis T7
→ add Reviewer T8
→ execute new evidence tasks
→ reanalyze
→ second review PASSED
→ Supervisor FINALIZE
```

Plan Revision 安全规则：

- `preserved_task_ids` 必须精确匹配成功任务；
- 成功 Task 的 agent/type 不允许变化；
- 成功 Task 的 status/output/attempts 从可信旧 Plan 合并；
- 模型不必复制成功 Task 内容；
- 新 Task 可以依赖历史成功 Task；
- Retrieve-More 必须增加 Evidence、Analysis、Reviewer；
- 写流程必须增加受 Reviewer 约束的 Action；
- 合并后重新执行完整 DAG Validator。

## 12. 有界循环与容量规划

真实 Supervisor 每轮产生结构化控制决策，因此旧静态图的 `max_model_calls=12` 不足以覆盖合法 Plan Revision。

根据真实轨迹重新容量规划：

```text
max_steps=64
max_model_calls=32
max_tool_calls=32
max_replans=2
max_tasks=12 per plan
max_parallel=4
deadline=600 seconds
```

预算仍然是硬限制，没有为了通过验收而删除。一次真实 Retrieve-More 使用 17 次 Supervisor 决策；加入 Planner/Replanner/Analysis 后仍低于 32 次模型调用上限。

## 13. 真实 Handoff

最终写 Run：

```text
092a4894-6fe2-434b-ab90-d44817bdeca3
```

Handoff 不是静态 edge，而是：

```text
SupervisorDecision.HANDOFF_ACTION
→ SupervisorPolicy validates ReviewResult.PASSED
→ HandoffEnvelope
→ Command(goto="action")
→ control_owner: supervisor → action
```

随后：

```text
ActionIntent persisted
→ control_owner: action → human
→ Approval interrupt
→ control_owner: human → harness
→ Existing Phase 2 executor
→ control_owner: harness → none
```

持久化事件：

```json
{
  "type": "control.handoff",
  "from": "supervisor",
  "to": "action",
  "allowed_operations": ["append_ticket_followup"]
}
```

Action 当前是同一持久化 StateGraph 中的受限执行分支，而不是独立进程。隔离由 HandoffEnvelope、control_owner、Agent Contract、无工具绑定和 Phase 2 Harness 共同实现。

## 14. Write Safety

Supervisor、Planner、Knowledge、Analysis、Reviewer 均不能访问 GLPI Write。

Action Agent：

- 不导入 GlpiClient；
- 不导入 CredentialCipher；
- 不持有 OAuth/Password；
- 只接受 PASSED HandoffEnvelope；
- 只生成 `append_ticket_followup` ActionIntent。

所有副作用继续进入：

```text
ControlledActionExecutor.execute()
```

Phase 2 回归最终通过：

```text
PHASE 2 ENTERPRISE VERIFICATION PASSED
sequential Followup: 20
concurrent Followup: 21
concurrent copies: 1
```

## 15. PostgreSQL Durable Supervisor

脚本：

```text
scripts/verify_supervisor_durable.py
```

两个独立 Python 进程：

### Process A

```text
Supervisor PLAN
Supervisor DISPATCH
Data SUCCESS
Knowledge SUCCESS
Supervisor JOIN
Analysis intentional failure
process exits
```

### Process B

```text
same PostgreSQL thread checkpoint
new process/new graph/new services
resume Analysis
Data calls = 0
Knowledge calls = 0
Supervisor continues REVIEW/FINALIZE
checkpoint test thread deleted
```

最终 durable thread：

```text
supervisor-durable-a17a3727-6430-40bd-a66f-cb7b6b3fc7e0
```

实际 API 启动恢复也已改为：有 checkpoint 时 `ainvoke(None)`，仅无 checkpoint 的 PENDING Run 才重新构造初始 State。

## 16. Fast Path

Fast Path 继续保持确定性，这是成本和延迟优化，不属于 Supervisor 缺失：

```text
simple data      Router → Data → END
simple knowledge Router → Knowledge → END
complex          Router → Supervisor Loop
unsupported      Router → reject
```

最终：

```text
Fast Data Run: 3b788497-778e-4bb7-8447-1f0772a24967
Fast Knowledge Run: 3336e498-3dff-4ee7-a96d-ef28531de36a
```

## 17. Evaluation

Routing Golden Dataset：

```text
samples=100
accuracy=100%
fast_path_recall=100%
unnecessary_agent_rate=0%
complex_misroute_rate=0%
```

Supervisor Runtime tests：

- real Supervisor node invocation count；
- state-driven decision trajectory；
- different goals → different DAGs；
- illegal Handoff rejected；
- policy feedback self-correction；
- ANALYZE/REVIEW target selection；
- control_owner transition；
- existing Harness reached exactly once；
- completed tasks preserved in revision；
- completed task identity mutation rejected；
- cross-process checkpoint resume。

## 18. Unified E2E Result

报告：

```text
evaluation/reports/phase3_e2e_latest.json
```

结果：

```text
status: passed
supervisor_model: deepseek-v4-flash
supervisor_decisions: 8
routing_samples: 100
routing_accuracy: 1.0
plan_revisions: 1
retrieve_more_rounds: 1
parallel_branches: 4
complex_read_run: 64d7b8fa-faed-4ae3-a415-e6f1da80b022
retrieve_more_run: aafd9818-4a55-42e9-b2fd-374ebda0ecd6
complex_write_run: 092a4894-6fe2-434b-ab90-d44817bdeca3
GLPI Followup: 18
```

## 19. Browser Verification

GLPI Ticket：

```text
http://127.0.0.1:8088/front/ticket.form.php?id=2
```

浏览器已确认：

- Followup #18 对应真实 Supervisor 写 Run；
- Run Marker 可见；
- ServiceMind reviewed analysis 可见；
- Reviewer PASSED 文本可见；
- 写入用户为 `servicemind-acme`。

## 20. Files Added

```text
src/servicemind/domain/supervisor.py
src/servicemind/runtime/structured.py
src/servicemind/agents/supervisor.py
src/servicemind/agents/dynamic_planner.py
src/servicemind/orchestration/supervisor_policy.py
src/servicemind/orchestration/supervisor_workflow.py
scripts/verify_supervisor_durable.py
tests/servicemind/test_supervisor_runtime.py
tests/servicemind/test_dynamic_planner.py
docs/PHASE3_SUPERVISOR_RUNTIME_ACCEPTANCE.md
```

## 21. Files Removed as Incorrect/Obsolete

```text
src/servicemind/orchestration/phase3_workflow.py
src/servicemind/orchestration/planner.py
tests/servicemind/test_phase3_workflow.py
scripts/verify_phase3_durable.py
```

这些文件属于未提交的旧静态 Phase 3 实现。删除原因是避免两套运行图、两套 Planner 和两套恢复测试成为双重真相来源。共用的 Evidence、Registry、DAG Validator、Dispatcher、Budget、Agents 和 Harness 全部保留。

## 22. Files Modified

```text
.env.example
src/core/llm.py
src/core/settings.py
src/service/service.py
src/servicemind/agents/analysis.py
src/servicemind/harness/recovery.py
src/servicemind/orchestration/registry.py
src/servicemind/orchestration/runtime.py
src/servicemind/orchestration/state.py
scripts/verify_phase3.ps1
tests/core/test_llm.py
tests/servicemind/test_phase3_router_planner.py
docs/PHASE3_ENTERPRISE_ACCEPTANCE.md
```

## 23. Security Gates

| Gate | Result |
| --- | --- |
| Supervisor has GLPI tools | PASS — none |
| Supervisor direct write | PASS — impossible by binding |
| Invalid Supervisor transition | PASS — rejected |
| Invalid decision self-correction | PASS |
| Planner unknown Agent/task | PASS — validator rejects |
| Cyclic DAG | PASS — validator rejects |
| Completed task mutation | PASS — rejected |
| Reviewer bypass | PASS — Handoff illegal |
| Handoff without control ownership | PASS — rejected |
| Action direct GLPI client | PASS — absent |
| Phase 2 Harness bypass | PASS — absent |
| Cross-tenant leakage | PASS — 0 |
| Approval bypass | PASS — 0 |
| Concurrent duplicate write | PASS — 0 |
| Secret stored in Git | PASS — ignored `.env` only |

## 24. Test Result

删除旧静态 Graph 专属测试后的最终有效基线：

```text
248 passed
4 skipped
40 warnings
0 failed

Ruff: PASS
Pyrefly: 0 errors
Markdown lint: PASS
Phase 2 enterprise regression: PASS
Phase 3 DeepSeek Supervisor E2E: PASS
Browser GLPI verification: PASS
```

删除旧测试导致测试数量从历史报告下降，但不是覆盖率缩水：对应的旧实现文件也同时删除，并由 `test_supervisor_runtime.py`、`test_dynamic_planner.py` 和 `verify_supervisor_durable.py` 覆盖新的唯一运行图。

## 25. Remaining Boundaries

Phase 3 已完成真实 Supervisor Runtime，但以下仍不属于本阶段：

- Enterprise Hybrid RAG；
- Graph-RAG；
- Memory Lifecycle；
- MCP Server/Gateway；
- OPA/Rego；
- Redis Worker/Outbox；
- Model Gateway 多模型 fallback；
- Prometheus/Grafana 完整观测；
- ITSM SafetyBench 全量适配；
- React 产品前端。

DeepSeek 是当前 Supervisor/Planner 运行模型；Provider 熔断、跨模型 fallback、配额和成本路由属于后续 Model Gateway 阶段，不能在 Phase 3 冒充完成。

## 26. Final Assessment

整改后的 Phase 3 已满足“真实 Supervisor”定义：

- Supervisor 是真实 Agent 和 Graph Node；
- 每个复杂阶段完成后回到 Supervisor；
- Supervisor 每轮调用 LLM 并输出结构化决策；
- Planner 生成动态 DAG；
- Dispatcher 执行 Supervisor 选择的 Ready Tasks；
- Reviewer feedback 驱动动态 Plan Revision；
- Policy 约束而不取代 Supervisor；
- Handoff 由 SupervisorDecision 和 Command 发起；
- control_owner 明确记录权限转移；
- Action 和外部写入仍受 Phase 2 Harness 控制；
- checkpoint 能跨进程恢复而不重复完成分支。

因此，本阶段可准确标记为：

```text
SERVICE MIND PHASE 3
TRUE SUPERVISOR + DYNAMIC DAG + AGENT LOOP + CONTROLLED HANDOFF
COMPLETE
```
