# ServiceMind Phase 3 企业级实施与验收报告

> 最后验收：2026-09-05  
> 项目：`D:\FastAPI\agent-service-toolkit`  
> 阶段：1 Supervisor + 5 Professional Agents + Multi-Agent Harness v2  
> 状态：**SUPERSEDED — 历史静态编排验收，不是最终 Phase 3**  
> 最终 Supervisor Runtime 验收见：`docs/PHASE3_SUPERVISOR_RUNTIME_ACCEPTANCE.md`  
> 纠正说明：本文件记录的早期实现把固定 StateGraph 控制流误称为 Supervisor/Handoff。该静态实现及专属测试已经删除，不能再作为最终完成依据。本文件保留用于审计问题发现和整改历史。

## 1. Final Status

Phase 3 的全部关键 Gate 已通过：

- 固定 1 Supervisor + 5 Professional Agents；
- Fast Path、Structured Router、100 条 Golden Dataset；
- Structured Planner、Task DAG、确定性 Validator；
- PENDING → READY → RUNNING → SUCCESS/FAILED/CANCELLED 状态机；
- Data + Knowledge 使用 LangGraph `Send` 真并行；
- Evidence Join 保留 tenant、source、resource 和 content hash provenance；
- Analysis 只从 JoinedEvidence 推理；
- Reviewer 支持 PASSED、RETRIEVE_MORE、REPLAN、REJECT、ESCALATE；
- Reviewer 未 PASSED 时 Action 在图上不可达；
- bounded replan、deadline、step/model/tool budget、loop guard；
- Supervisor → Action 的唯一 HandoffEnvelope；
- Action Agent 只产生 ActionIntent；
- 所有真实写入 100% 复用 Phase 2 `ControlledActionExecutor.execute()`；
- PostgreSQL Checkpoint 跨进程 crash/resume；
- 已完成 Data/Knowledge 分支不会在 resume 后重复调用；
- Phase 2 多租户、审批、幂等、并发锁、回读和审计回归全部通过；
- 真实 GLPI UI 可见最终私有 Followup #16。

不能由本报告推导出 Phase 4+ 已完成。Enterprise Hybrid RAG、Graph-RAG、Memory、MCP、OPA、Redis Worker 和完整观测栈仍是后续阶段。

## 2. Current Architecture Inspection

编码前的完整审计记录位于：

`docs/PHASE3_CURRENT_ARCHITECTURE_MAP.md`

审计确认：

| 现有能力 | Phase 3 处理 |
| --- | --- |
| FastAPI entry/lifespan | 复用；同时配置 Phase 2/3 Checkpointer |
| Keycloak TenantContext | 原样复用并传入所有 Agent 隔离输入 |
| PostgreSQL FORCE RLS | 原样复用，无第二套数据访问层 |
| GLPI OAuth/Entity Resolver | 仅 Data Agent 和 Phase 2 Harness 可达 |
| LangGraph PostgreSQL Saver | 复用为 DAG durable state |
| ActionIntent/Approval | 复用现有持久化记录和 action hash |
| ControlledActionExecutor | 冻结为唯一写入口 |
| 幂等、advisory lock、Marker、read-back | 100% 复用 |
| Audit/RunEvent/SSE | 复用并增加 Phase 3 事件 |

## 3. Existing Phase 2 Harness

唯一正式业务写入口：

```text
ControlledActionExecutor.execute(TenantContext, ActionIntent)
```

实际文件：`src/servicemind/harness/executor.py`

写入链：

```text
ActionIntent persisted
→ approver JWT
→ exact action hash
→ tenant-scoped idempotency record
→ PostgreSQL advisory lock
→ GLPI Marker reconciliation
→ private Followup
→ read-after-write verification
→ append-only audit
```

Phase 3 没有新增 GLPI Writer、凭据解密器、Approval 表、Idempotency 表或第二套 Harness。

## 4. Phase 3 Architecture

```text
                              Router
                    ┌───────────┴───────────┐
                    │                       │
               Fast Path              Complex Workflow
           ┌────────┴────────┐               │
           ▼                 ▼               ▼
       Data Agent      Knowledge Agent   Supervisor
                                            │
                                      Planner / DAG
                                            │
                                    Deterministic Validator
                                            │
                                        Dispatcher
                                   ┌────────┴────────┐
                                   ▼                 ▼
                              Data Agent      Knowledge Agent
                                   └────────┬────────┘
                                            ▼
                                      Evidence Join
                                            │
                                       Analysis Agent
                                            │
                                       Reviewer Agent
                     ┌──────────────┬───────┼───────┬──────────────┐
                     ▼              ▼       ▼       ▼              ▼
                  PASSED      RETRIEVE  REPLAN   REJECT       ESCALATE
                     │            MORE      │       │              │
                     │              └───────┴───────┘        Human interrupt
                     ▼
                 Supervisor
                     │
                HandoffEnvelope
                     │
                 Action Agent
                     │
                 ActionIntent
                     │
          Existing Phase 2 Harness
                     │
              Approval / Execute
                     │
                    GLPI
```

Supervisor 是确定性 Control Plane，不调用 GLPI、不产生专业分析，也不能直接访问执行器。

## 5. New Domain Models

### Evidence

文件：`src/servicemind/domain/evidence.py`

包含：

- `evidence_id`：tenant/source/resource/content hash 派生的稳定 ID；
- `tenant_id`；
- `source_type`：GLPI/KNOWLEDGE，预留 MEMORY/GRAPH/EXTERNAL；
- `source_ref`；
- `resource_type/resource_id`；
- 有界 `content`；
- provider、retrieval method、retrieved_at、SHA-256 content hash；
- confidence 和 metadata。

Join 会拒绝跨 Tenant Evidence，并按 source_ref + content hash 去重。

### Task / TaskPlan

文件：`src/servicemind/domain/task.py`

TaskStatus：

```text
PENDING / READY / RUNNING / SUCCESS / FAILED / SKIPPED / CANCELLED
```

ErrorPolicy：

```text
FAIL_FAST / RETRY / SKIP / REPLAN / ESCALATE
```

TaskPlan 包含 plan ID、goal、tasks、parallel 上限、deadline 和完整 Budget。

### AnalysisResult

文件：`src/servicemind/domain/analysis.py`

包含 classification、impact、urgency、priority、recommended group、recurring incident、Problem/Change recommendation、proposed actions、reasoning summary、evidence refs、confidence。

不保存 Chain-of-Thought；只保存业务可审核 rationale。

### ReviewResult

文件：`src/servicemind/domain/review.py`

包含 decision、unsupported claims、missing evidence、conflicts、policy issues、risk、feedback 和 reviewed refs。

### HandoffEnvelope

文件：`src/servicemind/domain/handoff.py`

只允许 PASSED Review，只允许 `append_ticket_followup`，并拒绝 secret/password/credential/token/api_key 等敏感键。

不包含完整 messages、scratchpad、内部推理或凭据。

### ActionIntent

原有 Phase 2 ActionIntent 增加 tenant、requested_by、evidence refs 和 idempotency context，但 action type 仍固定为 `append_ticket_followup`。

## 6. Agent Contracts

文件：`src/servicemind/orchestration/registry.py`

| Agent | Input | Output | Allowed tools | Scope | Timeout |
| --- | --- | --- | --- | --- | ---: |
| Knowledge | KnowledgeTaskInput | Evidence[] | `knowledge.read.baseline` | read only | 10s |
| Data | DataTaskInput | Evidence[] | `glpi.read.ticket` | read only | 20s |
| Analysis | JoinedEvidence | AnalysisResult | none | no tools | 30s |
| Reviewer | Analysis + Evidence | ReviewResult | none | no tools | 20s |
| Action | HandoffEnvelope | ActionIntent | none | intent only | 10s |

所有 Contract 都禁止 `glpi.write`、`glpi.credentials`、direct HTTP 和 direct database。

运行时使用 `asyncio.timeout()` 强制 Agent timeout。Data/Knowledge 的瞬时错误执行有界 retry；失败后 Task 标记 FAILED，后续 Task 取消。

## 7. Agent Registry

Registry 只包含：

```text
knowledge / data / analysis / reviewer / action
```

Planner 生成的每个 Task 都必须通过 Registry 的 Agent 和 task type 校验。未知 `security_agent` 会得到 `UNKNOWN_AGENT`，不能动态生成 Agent。

## 8. Router

文件：`src/servicemind/orchestration/router.py`

Route Enum：

```text
SIMPLE_DATA_QUERY
SIMPLE_KNOWLEDGE_QUERY
COMPLEX_WORKFLOW
UNSUPPORTED
```

Router 返回结构化 `RouteDecision`，自然语言不直接作为 graph edge。

禁止删除、关闭工单、读取密码/Token 等请求在最早阶段进入 UNSUPPORTED，不启动 Supervisor 或任何业务写入。

## 9. Routing Evaluation

Dataset：`evaluation/routing/routing.jsonl`

实际结果：

```text
samples: 100
accuracy: 100%
fast_path_recall: 100%
unnecessary_agent_rate: 0%
complex_task_misroute_rate: 0%
failures: 0
```

Confusion Matrix：

| Expected | Data | Knowledge | Complex | Unsupported |
| --- | ---: | ---: | ---: | ---: |
| Data | 25 | 0 | 0 | 0 |
| Knowledge | 0 | 25 | 0 | 0 |
| Complex | 0 | 0 | 25 | 0 |
| Unsupported | 0 | 0 | 0 | 25 |

报告：`evaluation/reports/phase3_routing_latest.json`

## 10. Planner

文件：`src/servicemind/orchestration/planner.py`

复杂读计划：

```text
T1 Data
T2 Knowledge
T3 Analysis depends T1,T2
T4 Reviewer depends T3
```

复杂写额外增加：

```text
T5 Action depends T4
```

Planner 输出 Pydantic TaskPlan；安全合法性由独立 Validator 决定。Fast Path 不调用 Planner。

## 11. Deterministic DAG Validator

文件：`src/servicemind/orchestration/task_dag.py`

代码级校验：

1. Task ID 唯一；
2. Agent 必须在 Registry；
3. task type 必须属于 Agent Contract；
4. dependency 必须存在；
5. 禁止 self dependency；
6. Kahn topological sort 检测 cycle；
7. 最大 12 Tasks；
8. 最大并行 4；
9. 最大 replan 2；
10. timezone-aware deadline；
11. Reviewer 必须有 Analysis ancestor；
12. Action 必须有 Reviewer ancestor。

限制均可通过环境变量配置。

## 12. Dispatcher

文件：`src/servicemind/orchestration/dispatcher.py`

Dispatcher 根据成功依赖和 parallel capacity 计算 READY Tasks，不依赖 LLM 记忆任务状态。

实际状态转换：

```text
PENDING → READY → RUNNING → SUCCESS/FAILED
                         └→ CANCELLED/SKIPPED
```

非法状态跳转会抛出错误。取消、预算终止和关键错误会取消未完成 Task。

## 13. Parallel Execution

复杂流程使用 LangGraph `Send` 同时调度 Data 和 Knowledge，非顺序 await 冒充并行。

最终真实 Complex Read Run：

```text
Run: 0f05368f-6835-4357-98c8-28cf9b855f90
parallel branches: 4
round 1: Data + Knowledge overlap = true
round 2: Data + Knowledge overlap = true
```

此前测得两轮启动时间差约 0.7–0.9 ms。每个 Agent 只接收 tenant identity、当前 Task 所需字段和必要 Evidence，不接收完整聊天历史。

## 14. Evidence Join

Join：

- 拒绝 tenant mismatch；
- 保留 source_ref、resource、content hash 和 retrieved_at；
- 稳定去重；
- 不把 Evidence 降级为匿名字符串；
- Task output_ref 指向 Joined Evidence；
- Analysis/Reviewer 使用 evidence IDs。

## 15. Data Agent

Data Agent 通过 tenant-scoped GLPI v2.3 API 并行读取：

- Ticket；
- 当前 Entity 可见的 Support Groups。

真实 Acme Evidence：

```text
GLPI support group: Network Team
GLPI support group: Service Desk
```

Globex 有独立 Entity 下的同名 Group。Reviewer 只接受当前 tenant GLPI Evidence 中真实存在的推荐 Group；Runbook 中出现组名不再被视作“Group 存在”的充分证据。

## 16. Knowledge Agent

Phase 3 使用只读、稳定、带 provenance 的基础 Runbook Provider，覆盖 VPN/MFA、优先级矩阵和通用 Incident Triage。

它不是 Enterprise Hybrid RAG，不能在简历中写成 BM25/Dense/RRF/Reranker。其内部将在 Phase 4 被替换，Evidence Contract 和 Agent Contract 保持不变。

## 17. Analysis Agent

Analysis 使用 Structured Output，失败时采用确定性保守 fallback。输入只包含 JoinedEvidence 和目标，不持有 GLPI Client 或 write tools。

输出只允许 `append_ticket_followup` proposal；Reviewer 仍会二次检查 operation allowlist 和 evidence refs。

## 18. Reviewer

Reviewer 执行：

- Analysis refs 是否存在；
- Evidence tenant 是否一致；
- ProposedAction refs 是否存在；
- operation 是否在 Phase 3 allowlist；
- GLPI Ticket facts 是否存在；
- Knowledge Evidence 是否存在；
- 推荐 Group 是否真实存在于 tenant-scoped GLPI；
- conflict metadata；
- confidence 与重大优先级；
- 写请求是否包含有界 Action proposal。

五个输出路径均有测试：PASSED、RETRIEVE_MORE、REPLAN、REJECT、ESCALATE。

## 19. Retrieve-More Feedback Loop

最终 Complex Read 实际轨迹：

```text
router
→ supervisor
→ planner
→ dag_validator
→ dispatcher/fanout
→ data + knowledge
→ evidence_join
→ analysis
→ reviewer
→ review:retrieve_more
→ retrieve_more
→ dispatcher/fanout
→ data + knowledge
→ evidence_join
→ analysis
→ reviewer
→ review:passed
→ finalize
```

第二轮保留旧 Evidence 并追加新 Evidence，Join 去重后再分析，没有从零丢弃第一轮事实。

## 20. Bounded Replan、Loop Guard 与 Budget

可配置限制：

```text
max_tasks=12
max_parallel=4
max_replans=2
max_steps=24
max_model_calls=12
max_tool_calls=16
deadline=600 seconds
consecutive_failures=3
```

结构化终止码：

```text
budget_exceeded
replan_limit_exceeded
deadline_exceeded
loop_guard_triggered
critical_error
cancelled
unsupported
```

持续返回 REPLAN 的 Integration Test 在第三次被确定性终止，Action invocation count 为 0。

## 21. Error Policy

- Data/Knowledge：有界 retry，最终失败进入 `task.failed`；
- Analysis/Reviewer/Planner/Handoff/Action/Execute：LangGraph NodeError handler；
- 失败 Task 标记 FAILED；
- 未执行下游 Task 标记 CANCELLED；
- error state 只保存 node 和 exception type，不保存可能包含敏感内容的原始异常；
- 外部写入错误继续由 Phase 2 幂等和 Marker reconciliation 保护。

## 22. Handoff

唯一 Handoff：

```text
Reviewer PASSED → Supervisor → HandoffEnvelope → Action Agent
```

真实最终 Handoff 只允许：

```text
append_ticket_followup
```

非 PASSED 的四类 Reviewer 决策均经 graph reachability test 证明 Action invocation count 为 0。

## 23. Action Agent 与 Harness Reuse

Action Agent 源码不导入：

- `GlpiClient`；
- `CredentialCipher`；
- 数据库 Session；
- HTTP Client。

AST 安全测试扫描整个 `src/servicemind`：除 GLPI Client 定义本身外，`append_ticket_followup()` 的调用只存在于 `harness/executor.py`。

## 24. Durable DAG Execution

脚本：`scripts/verify_phase3_durable.py`

以两个独立 Python 进程验证：

### Process A

```text
Data SUCCESS
Knowledge SUCCESS
Evidence Join SUCCESS
Analysis intentional failure
PostgreSQL checkpoint committed
process exits
```

### Process B

```text
same thread_id
new graph/runtime process
resume from PostgreSQL
Analysis continues
Data calls = 0
Knowledge calls = 0
Reviewer PASSED
run completes
checkpoint test thread cleaned
```

最终 durable thread：

```text
phase3-durable-b2435712-ccbe-4d7f-9d7b-0b903cdf12e8
```

## 25. Cancellation

- API 可取消 PENDING、WAITING_REVIEW、WAITING_APPROVAL；
- Action 前再次读取持久化 Run 状态；
- 即使 Reviewer 已 PASSED，只要 Run 已取消，Handoff/Action 不可达；
- Finalize 保留 CANCELLED，不会被覆盖为 SUCCEEDED；
- 剩余 Tasks 标记 CANCELLED；
- Human stop、Approval reject、budget/loop termination 同样阻止后续 Action。

## 26. E2E Demonstrations

### A. Fast Data Path

```text
Run: bd394d19-d978-4650-8894-407bbad4f92b
Trajectory: router → data
Status: succeeded
Knowledge/Analysis/Reviewer/Action: 0
```

### B. Fast Knowledge Path

```text
Run: b5b2be9a-5c8b-4021-a180-7c6f5dfa853a
Trajectory: router → knowledge
Status: succeeded
```

### C. Complex Read + Retrieve More

```text
Run: 0f05368f-6835-4357-98c8-28cf9b855f90
Review: passed
Retrieval rounds: 1 supplemental round
Parallel branch records: 4
ActionIntent: none
GLPI writes: 0
```

### D. Complex Write

```text
Run: 9b61578d-861b-481b-92b8-0d680e3a818e
Review: passed
All five planned Tasks: success
Handoff allowed operation: append_ticket_followup
Approval: separate approver JWT
Read-back verified: true
GLPI Followup: 16
```

### E. Bounded Replan

Integration graph fixture持续返回 REPLAN：

```text
replan_count: 3
termination: replan_limit_exceeded
Action invocation: 0
workflow terminated: true
```

### F. Human Escalation

真实 API 已验证：Reviewer ESCALATE 后 Run 进入 `waiting_review`；只有 approver 可调用 `/review-resolution`，stop 后 Run 为 cancelled，未产生 ActionIntent。

## 27. Browser Verification

页面：`http://127.0.0.1:8088/front/ticket.form.php?id=2`

最终浏览器事实：

```text
Title: Ticket - Acme VPN MFA outage - ID 2 - GLPI
Ticket timeline count: 16
Run marker 9b61578d-861b-481b-92b8-0d680e3a818e: visible
ServiceMind reviewed analysis: visible
Recommended group: Network Team: visible
Reviewer passed text: visible
Written by servicemind-acme: visible
```

## 28. Security Regression

| Gate | Result |
| --- | --- |
| Cross-Tenant Leakage | PASS / 0 |
| Approval Bypass | PASS / 0 |
| Forbidden Write | PASS / 0 |
| Reviewer Bypass | PASS / 0 |
| Action Direct GLPI Write | PASS / 0 |
| Secret in Handoff | PASS / 0 |
| Duplicate Side Effect | PASS / 0 |
| Phase 2 RLS/Audit regression | PASS |

最终 Phase 2 回归：

```text
PHASE 2 ENTERPRISE VERIFICATION PASSED
sequential Followup: 12
concurrent Followup: 13
concurrent copies: 1
```

## 29. Tests

最终完整测试：

```text
254 passed
4 skipped
40 warnings
0 failed
```

代码质量：

```text
Ruff: PASS
Pyrefly: 0 errors
Routing Eval: PASS
Phase 2 live verification: PASS
Phase 3 live verification: PASS
Browser GLPI verification: PASS
```

Warnings 为上游/第三方 LangGraph、Pydantic、Starlette、Google SDK deprecation，未造成测试失败。

新增测试：

- `test_phase3_contracts.py`
- `test_phase3_dag.py`
- `test_phase3_router_planner.py`
- `test_phase3_agents.py`
- `test_phase3_workflow.py`
- `test_phase3_budget.py`
- `test_phase3_security.py`
- `test_phase3_routing_evaluation.py`
- GLPI Group adapter test

## 30. Observability Minimal

RunEvent/OTel 能观察：

- route；
- plan ID；
- task/agent；
- evidence refs；
- review decision；
- replan count；
- handoff；
- action hash；
- task/workflow failure；
- execution verification；
- branch start/finish timestamps。

未部署 Prometheus/Grafana/Tempo/Loki，符合 Phase 3 禁止扩大范围的要求。

## 31. Files Changed in Phase 3

### New domain

```text
src/servicemind/domain/analysis.py
src/servicemind/domain/evidence.py
src/servicemind/domain/handoff.py
src/servicemind/domain/review.py
src/servicemind/domain/routing.py
src/servicemind/domain/task.py
```

### New orchestration/runtime

```text
src/servicemind/orchestration/budget.py
src/servicemind/orchestration/dispatcher.py
src/servicemind/orchestration/phase3_workflow.py
src/servicemind/orchestration/planner.py
src/servicemind/orchestration/registry.py
src/servicemind/orchestration/router.py
src/servicemind/orchestration/task_dag.py
```

### New agents/evaluation

```text
src/servicemind/agents/knowledge.py
src/servicemind/agents/reviewer.py
src/servicemind/evaluation/__init__.py
src/servicemind/evaluation/routing.py
```

### Modified Phase 3 integration

```text
src/servicemind/agents/action.py
src/servicemind/agents/analysis.py
src/servicemind/agents/data.py
src/servicemind/domain/models.py
src/servicemind/orchestration/state.py
src/servicemind/orchestration/runtime.py
src/servicemind/api.py
src/servicemind/persistence/models.py
src/service/service.py
src/core/settings.py
.env.example
```

### GLPI read-domain enhancement

```text
src/servicemind/integrations/glpi/client.py
src/servicemind/integrations/glpi/models.py
deploy/glpi/bootstrap_phase2.php
```

### Evaluation, scripts and docs

```text
evaluation/routing/routing.jsonl
evaluation/reports/phase3_routing_latest.json
evaluation/reports/phase3_e2e_latest.json
scripts/evaluate_phase3_routing.py
scripts/verify_phase3.ps1
scripts/verify_phase3_durable.py
docs/PHASE3_CURRENT_ARCHITECTURE_MAP.md
docs/PHASE3_ENTERPRISE_ACCEPTANCE.md
```

### Deleted

```text
None
```

## 32. Upstream vs Personal Work

### Upstream Existing

- FastAPI Agent Service；
- Streamlit client；
- LangGraph base runtime；
- upstream Agent examples；
- AG-UI/SSE basic support；
- base checkpointer/store abstraction。

### Phase 2 Existing

- GLPI 11/MariaDB/Keycloak/PostgreSQL；
- TenantContext/JWT/RLS；
- GLPI typed adapter；
- Data/Analysis/Action vertical slice；
- Approval/HITL；
- idempotency/advisory lock；
- read-back verification；
- audit/recovery/webhook。

### Phase 3 New

- 1+5 domain architecture；
- Agent Registry/Contracts；
- Fast Path Router；
- Planner/Task DAG/Validator；
- Dispatcher/Task state；
- LangGraph Send parallel fan-out；
- provenance Evidence/Join；
- Knowledge/Reviewer Agents；
- five-way Reviewer feedback；
- bounded replan/loop/budget；
- HandoffEnvelope；
- Reviewer-to-Action reachability guard；
- cross-process durable DAG test；
- routing/trajectory/security evaluation；
- real GLPI Group Evidence；
- Phase 3 E2E/browser evidence。

## 33. Acceptance Checklist

| Area | Result |
| --- | --- |
| Fixed 1 Supervisor + 5 Agents | PASS |
| Agent Registry and contracts | PASS |
| Evidence/Task/Plan/Review/Handoff/Action contracts | PASS |
| Fast Data/Knowledge paths | PASS |
| 100+ routing samples | PASS — 100 |
| Routing accuracy ≥95% | PASS — 100% |
| Unnecessary Agent Rate | PASS — 0% |
| Structured Planner | PASS |
| DAG cycle/agent/dependency/order validation | PASS |
| Dispatcher and Task statuses | PASS |
| Real Data/Knowledge parallelism | PASS |
| Evidence Join/provenance | PASS |
| Structured Analysis | PASS |
| Reviewer five decisions | PASS |
| Retrieve-More feedback | PASS |
| Bounded Replan | PASS |
| Loop Guard/Budget/Deadline | PASS |
| Reviewer fail → Action unreachable | PASS |
| Supervisor → Action Handoff only | PASS |
| Action credential-free | PASS |
| All writes through Phase 2 Harness | PASS |
| DAG checkpoint/crash/resume | PASS |
| Completed branches not repeated | PASS |
| Cancellation propagation | PASS |
| Cross-tenant security regression | PASS |
| Fast/Read/Write/Retrieve/Replan demos | PASS |
| Browser-verifiable GLPI change | PASS |

## 34. Remaining Issues / Explicitly Deferred

| Item | Status | Reason |
| --- | --- | --- |
| Enterprise Hybrid RAG | NOT IMPLEMENTED | Phase 4 |
| BM25/Dense/RRF/Reranker | NOT IMPLEMENTED | Phase 4 |
| Neo4j Graph-RAG | NOT IMPLEMENTED | Phase 4 |
| Memory Lifecycle | NOT IMPLEMENTED | Phase 5 |
| Agent Skills | NOT IMPLEMENTED | Phase 5 |
| Model Gateway | NOT IMPLEMENTED | Phase 5 |
| MCP/OPA/Redis Outbox | NOT IMPLEMENTED | Phase 6 |
| Prometheus/Grafana full stack | NOT IMPLEMENTED | Phase 7 |
| SafetyBench full adaptation | NOT IMPLEMENTED | Phase 7 |
| React frontend/Kubernetes | NOT IMPLEMENTED | Phase 8/conditional |

Phase 3 Knowledge Provider 是刻意受限的本地 Runbook baseline。它已经有可替换 Contract，但不冒充企业 Hybrid RAG。

## 35. Resume-ready Project Highlight

> 在真实 GLPI 11 场景中将 LangGraph Agent Runtime 升级为 1+5 企业 ITSM Multi-Agent：设计确定性 Fast Path Router、Pydantic Task DAG 与安全 Validator，通过 LangGraph Send 实现 Data/Knowledge 并行 Evidence Plane，引入 provenance-aware Join、Reviewer 五路反馈、bounded replan、budget/loop guard 及 Reviewer→Action Handoff；Action Agent 仅生成 ActionIntent，所有副作用复用 Keycloak/RLS/HITL/幂等锁/回读审计 Harness。100 条路由集准确率 100%，两个独立进程验证 PostgreSQL checkpoint 恢复时不重复已完成分支，真实 GLPI Followup 写入并经浏览器验证，完整回归 254 tests passed。
