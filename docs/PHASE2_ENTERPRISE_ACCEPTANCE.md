# ServiceMind Phase 2 企业级改造与验收报告

> 最后验收日期：2026-09-05  
> 项目目录：`D:\FastAPI\agent-service-toolkit`  
> 上游基线：`JoshuaC215/agent-service-toolkit`，完整 Git clone，基线提交 `fe3b2dc`  
> Phase 2 定义：企业级多租户、可恢复、受控写入的 ITSM Agent 垂直闭环

## 1. 结论

Phase 2 在其约定范围内已经达到“企业应用语义与工程控制基线”，可以作为简历项目和企业 Agent 岗位面试的有效实战成果：

- 真实 GLPI，而不是 mock ITSM 或玩具数据库；
- Keycloak 身份、RBAC 与 GLPI Entity ABAC；
- PostgreSQL `FORCE RLS` 双层租户隔离；
- LangGraph PostgreSQL Checkpoint、审批中断与宕机恢复；
- ActionIntent、人审、动作哈希和确定性执行器；
- HMAC Webhook、事务型幂等收据、确定性 Run；
- PostgreSQL advisory lock、外部系统写前对账、写后回读验证；
- 追加型审计、SSE 事件回放和可选 OTLP Trace；
- 单元测试、类型检查、静态检查、真实集成和浏览器验收证据。

必须区分两个结论：

| 验收层级 | 结论 | 含义 |
| --- | --- | --- |
| Phase 2 企业级垂直闭环 | **通过** | 多租户、可恢复、受控写入和 exactly-once 语义已落地并验证 |
| 整个 ServiceMind 最终方案 | **尚未完成** | 1+5、企业 RAG、完整 MCP、Memory、Evaluation 等属于后续阶段 |
| 面向正式生产环境直接上线 | **有条件，不建议直接上线** | 仍需 TLS、Vault/KMS、Keycloak production mode、HA worker、限流、备份恢复和监控告警 |

因此，准确表述应是：

> Phase 2 已完成并通过企业级垂直闭环验收；项目具备企业工程设计与可演示证据，但不是已经完成全部生产基础设施认证的最终 SaaS 产品。

## 2. 最终架构

```text
Authenticated REST                         GLPI Webhook
       |                                        |
       v                                        v
Keycloak JWT                           HMAC + timestamp
iss/aud/exp/sub                        tenant/entity check
tenant/roles/entities                           |
       +-------------------+--------------------+
                           v
                     TenantContext
                           |
                  PostgreSQL transaction
                 SET LOCAL app.tenant_id
                    FORCE RLS policies
                           |
                           v
                 Durable AgentRun + Events
                           |
                    LangGraph Checkpoint
                           |
               Data -> Analysis -> ActionIntent
                                      |
                              interrupt / approval
                                      |
                         approver role + action hash
                                      |
                               Controlled Harness
                         advisory lock + idempotency
                                      |
                         GLPI private Followup write
                                      |
                             read-after-write verify
                                      |
                      append-only audit + SSE + OTel
```

当前垂直闭环使用三个职责明确的 Agent：

| Agent | 权限 | 职责 |
| --- | --- | --- |
| Data Agent | 只读 | 读取租户范围内的 GLPI Ticket 事实 |
| Analysis Agent | 无业务写权限 | 结构化分类、优先级、负责组和置信度 |
| Action Agent | 只生成意图 | 产生规范化 ActionIntent 和 action hash，不持有 GLPI 凭据 |

这三个 Agent 是 Phase 2 安全垂直切片，不等于最终固定的 Supervisor + 5 Agents。Knowledge、Reviewer 和 Supervisor 是下一阶段工作，不能写成已经完成。

## 3. 基础设施与真实业务系统

### 3.1 Docker Compose

`deploy/glpi/compose.yaml` 部署：

- GLPI `11.0.8`，仅绑定 `127.0.0.1:8088`；
- MariaDB `11.8`，不向宿主机暴露端口；
- ServiceMind PostgreSQL `16`，绑定 `127.0.0.1:55432`；
- Keycloak `26.7.3`，绑定 `127.0.0.1:8090`，管理/健康端口为 `9001`；
- 独立 Keycloak PostgreSQL `16`；
- 所有有状态组件使用命名卷；
- 数据库和身份服务具有健康检查与依赖启动顺序。

### 3.2 GLPI 初始化

新增脚本：

- `deploy/glpi/bootstrap_oauth.php`：启用 GLPI High-Level API v2.3 并创建 OAuth Client；
- `deploy/glpi/bootstrap_phase2.php`：创建 Acme、Globex Entity 及各自技术用户；
- `deploy/glpi/bootstrap_webhooks.php`：创建按 Entity 隔离的 Ticket Webhook；
- `deploy/glpi/local_define.php`：保留 GLPI SSRF 防护，仅精确放行本地 ServiceMind Webhook URL。

实际隔离域：

| Tenant | Tenant UUID | GLPI Entity | Keycloak 用户 |
| --- | --- | --- | --- |
| Acme | `11111111-1111-4111-8111-111111111111` | 1 | `acme-analyst`、`acme-approver` |
| Globex | `22222222-2222-4222-8222-222222222222` | 2 | `globex-analyst` |

GLPI 默认弱账户已经处置：默认 tech、normal、post-only 禁用，管理员默认密码已更换。真实密码和密钥只存在于 Git 忽略的本地 `.env` 文件，未写入本文档。

## 4. 身份、授权与租户隔离

### 4.1 Keycloak OIDC

`src/servicemind/security/auth.py` 实现：

- 校验 RS256 签名；
- 强制 `iss`、`aud`、`exp`、`iat`、`sub`、`tenant_id`；
- JWKS 缓存五分钟，未知 `kid` 时强制刷新以支持密钥轮换；
- 从 `realm_access.roles` 构建 RBAC；
- 从 `glpi_entity_ids` 构建 Entity ABAC；
- Request Body 不能覆盖 TenantContext。

角色边界：

- `viewer`：租户范围读取和 GLPI 健康检查；
- `analyst`：创建分析 Run；
- `approver`：批准或拒绝持久化 ActionIntent；
- Webhook 使用 HMAC 服务身份，不接受匿名 Body 自声明的跨 Entity 权限。

上游通用 Agent 路由同时启用了独立 `AUTH_SECRET`。Phase 2 `/v1/servicemind/*` 不依赖该共享 Secret，始终使用 Keycloak JWT；Webhook 只使用其租户专属 HMAC Secret。

### 4.2 数据库 RLS

数据库所有租户表均启用：

```sql
ENABLE ROW LEVEL SECURITY;
FORCE ROW LEVEL SECURITY;
```

策略基于：

```sql
tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
```

`tenant_session()` 在事务内执行 `set_config(..., true)`。运行账户为 `NOSUPERUSER NOBYPASSRLS`，迁移账户与运行账户分离。没有 tenant context 时，运行账户在租户表中看到零行。

RLS 覆盖表：

- `tenant_memberships`
- `glpi_integrations`
- `agent_runs`
- `run_events`
- `action_intents`
- `approvals`
- `tool_invocations`
- `audit_events`
- `idempotency_records`

GLPI 侧再使用独立技术账户、非递归 Entity Header 和 Profile Header，形成 JWT/业务适配器/RLS 三重边界。

## 5. 持久化模型与迁移

新增 Alembic：

| Revision | 作用 |
| --- | --- |
| `0001_phase2` | 显式创建企业核心表、索引、外键和 9 条 RLS Policy |
| `0002_webhook_secrets` | 增加租户级加密 Webhook Secret |
| `0003_append_only_audit` | 增加拒绝 `audit_events` UPDATE/DELETE 的数据库 Trigger |

首个迁移已改为完全显式 Schema，不再导入会随应用变化的 `Base.metadata.create_all()`。这保证空库安装和历史升级不会因未来 ORM 变化产生不同数据库。

核心表：

- `tenants`、`tenant_memberships`：租户目录与成员关系；
- `glpi_integrations`：每租户 GLPI 地址、Entity/Profile 和加密凭据；
- `agent_runs`、`run_events`：持久化运行状态和顺序事件；
- `action_intents`、`approvals`：动作意图、人审决策和 action hash；
- `tool_invocations`：工具调用记录模型；
- `idempotency_records`：租户内唯一幂等键、动作哈希与执行结果；
- `audit_events`：追加型结构化审计。

审计 Trigger 防止应用运行角色篡改已有审计记录。它是数据库内 append-only 控制，不等同于外部 WORM/SIEM：数据库 Owner 仍然可以执行 DDL，这属于生产基础设施后续项。

## 6. GLPI 适配器

`src/servicemind/integrations/glpi/` 实现：

- GLPI v2.3 OAuth Token 缓存；
- 遇到 401 时仅强制刷新并重试一次；
- 默认禁用代理继承 `trust_env=False`；
- 固定超时；
- 每请求附加 Entity、Profile 和 non-recursive Header；
- Pydantic 稳定领域模型，不把 GLPI 巨型原始 JSON 直接交给 LLM；
- HTML、script、style 内容清洗；
- Ticket 读取、最近 Ticket、私有 Followup 写入和 Followup 回读；
- 每租户凭据使用 Fernet 加密后持久化，由 Resolver 在 Harness 内解密；
- Agent 和 ActionIntent 中不出现 OAuth Client Secret 或 GLPI 密码。

当前 GLPI 使用 password grant 是本地 GLPI v2.3 技术账户兼容方案。正式生产应替换为平台批准的 service-to-service grant/凭据治理方式。

## 7. LangGraph 编排与恢复

`src/servicemind/orchestration/` 提供确定性 StateGraph：

```text
START -> data -> analysis -> [read-only finalize | action]
action -> approval interrupt
approval -> [execute | cancelled finalize]
execute -> finalize -> END
```

关键控制：

- 状态包含 `run_id`、`tenant_id`、用户、角色、允许 Entity、Ticket 和目标；
- PostgreSQL Checkpointer 使用独立 `thread_id`；
- `interrupt()` 在写入前冻结状态；
- `Command(resume=...)` 从审批点继续；
- `request_write=false` 路径不会创建 ActionIntent；
- 服务启动扫描 `pending/running` Run；
- 已持久化审批但尚未 resume 的 Run，启动时重建 ApprovalDecision 并从 interrupt 恢复；
- 未审批的 `waiting_approval` Run 保持等待，不会被恢复器自动授权；
- 恢复身份只有 `viewer + analyst`，不能绕过 approver 边界。

模拟的两类崩溃均已通过：

1. PENDING Run 在 Agent 开始前终止，重启后自动执行成功；
2. Approval 已提交、API 在 `resume_run` 前终止，重启后从 checkpoint 恢复并 exactly-once 写入。

## 8. 受控写入 Harness

`src/servicemind/harness/executor.py` 是 Phase 2 唯一允许执行 GLPI 写操作的组件。

### 8.1 写操作白名单

Phase 2 只允许：

```text
append_ticket_followup(is_private=true)
```

不允许关闭 Ticket、删除数据、修改 CMDB 或创建 Change。未知 action type 在确定性执行器内直接拒绝。

### 8.2 审批和 TOCTOU 防护

Action Agent 产生规范化参数并计算 SHA-256 `action_hash`。Approver 必须提交看到的同一个 hash；如果动作内容在审阅后发生变化，API 返回 409。

审批记录和 Run 状态转换位于同一个 `SELECT ... FOR UPDATE` 事务中：

- 同一个 ActionIntent 只能存在一个 Approval；
- 冲突决策返回 409；
- 第一个审批者原子地把 Run 从 `waiting_approval` 置为 `running`；
- 并发审批者不会再次调用 `resume_run`。

### 8.3 exactly-once 副作用

执行链：

```text
tenant/run/action idempotency key
  -> PostgreSQL unique constraint
  -> tenant-scoped pg_advisory_xact_lock
  -> reload idempotency record
  -> search GLPI marker before every write
  -> append private Followup when marker absent
  -> read returned Followup and verify marker
  -> complete idempotency record
  -> append audit event
```

Marker 格式：

```text
[ServiceMind run=<run_uuid> action=<action_hash_prefix>]
```

这处理三类重试：

- 普通 HTTP 重放：直接返回持久化结果；
- 两个进程并发执行：advisory lock 串行化；
- GLPI 已提交但本地在 commit 前崩溃：重试先扫描 Marker，恢复已有 Followup 而不是再次写入。

这里保证的是业务层“effectively/exactly once observable side effect”。跨数据库和 GLPI 不存在分布式事务，因此依赖幂等键、锁、业务 Marker 和对账恢复组合实现。

## 9. Webhook 可靠性

`src/servicemind/harness/webhooks.py` 与 API 实现：

- `HMAC-SHA256(raw_body + timestamp)`；
- 常量时间签名比较；
- 五分钟时钟窗口，拒绝重放；
- Tenant UUID、Ticket ID、Entity ID 强类型解析；
- Tenant Integration 与 payload Entity 再匹配；
- 每租户 Secret 不同且加密存储；
- `idempotency_record + deterministic UUIDv5 AgentRun + first RunEvent` 在一个事务内提交；
- 相同签名重试返回相同 Run ID 和 `duplicate=true`；
- 接收成功返回 HTTP 202；
- 进程在 BackgroundTask 启动前终止时，持久化 PENDING Run 由下次服务启动恢复。

## 10. API 合同

| Method | Endpoint | 权限/用途 |
| --- | --- | --- |
| GET | `/v1/servicemind/glpi/health` | viewer；租户级 GLPI OAuth 与 Entity 健康 |
| POST | `/v1/servicemind/runs` | analyst；创建并执行/中断 Run |
| GET | `/v1/servicemind/runs/{run_id}` | TenantContext + RLS |
| GET | `/v1/servicemind/runs/{run_id}/events` | TenantContext + RLS；SSE 和 Last-Event-ID |
| POST | `/v1/servicemind/runs/{run_id}/approval` | approver + expected action hash |
| POST | `/v1/servicemind/runs/{run_id}:cancel` | 租户范围内取消可取消 Run |
| POST | `/v1/servicemind/webhooks/glpi` | 租户 HMAC + timestamp + entity match |

旧的全局 `/servicemind/glpi/health` 已从 OpenAPI 和路由中移除，避免绕过 TenantContext 使用全局 GLPI 配置。

SSE 当前提供持久化快照回放并以 `end` 结束，支持 `Last-Event-ID`。它不是持续长轮询 tail；实时 tail/消息总线属于后续扩展。

## 11. 审计与可观测性

已实现：

- 审批、取消、GLPI 写入、恢复事件的结构化 Audit；
- Audit 关联 tenant、actor、resource、run、action hash 和 Followup ID；
- Audit 数据库级 append-only Trigger；
- 顺序化 RunEvent 与 SSE 回放；
- Data Agent、Analysis Agent、Tool Execute 的 OpenTelemetry Span；
- 稳定关联属性：tenant ID、run ID、Ticket ID、agent/tool name；
- 配置 `SERVICEMIND_OTEL_ENDPOINT` 时启用 OTLP/HTTP BatchSpanProcessor；
- 可与上游 Langfuse callback tracing 并存。

未在本地部署 OTel Collector、Prometheus 和 Grafana，因此本阶段只能声明“Trace 采集与导出代码路径完成”，不能声明监控看板/SLO/告警已经完成。

## 12. 启动、演示与可重复验收

### 12.1 启动

```powershell
Set-Location D:\FastAPI\agent-service-toolkit
.\start-servicemind.ps1
```

启动脚本会：

1. 启动或连接 Docker Desktop；
2. 启动 GLPI、MariaDB、PostgreSQL、Keycloak；
3. 执行 Alembic upgrade；
4. 初始化 LangGraph PostgreSQL Schema；
5. 幂等 Seed 两个 Tenant/Integration；
6. 启动 FastAPI 与 Streamlit；
7. FastAPI lifespan 恢复未完成 Run。

### 12.2 人审演示

```powershell
.\scripts\demo_phase2.ps1 -TicketId 2 -Approve
```

### 12.3 企业验收脚本

```powershell
.\scripts\verify_phase2.ps1
```

该脚本自动验证：

- Keycloak analyst/approver 身份；
- Acme/Globex 租户级 GLPI Health；
- 跨租户 Run 读取为 404；
- analyst 审批为 403；
- 错误 action hash 为 409；
- 正确审批、GLPI 回写和回读；
- 重放审批不重复写；
- SSE 关键事件；
- Webhook 签名与确定性去重；
- RLS、无 tenant context 零可见性、Alembic Head；
- Audit UPDATE 被数据库拒绝；
- 双并发审批最终只有一条 GLPI Followup。

辅助脚本：

- `scripts/verify_phase2_database.py`
- `scripts/verify_phase2_concurrency.py`
- `scripts/init_langgraph_postgres.py`
- `scripts/seed_phase2.py`

## 13. 最终验收证据

### 13.1 代码质量

```text
pytest: 203 passed, 4 skipped, 40 warnings
Ruff: All checks passed
Pyrefly: 0 errors
ServiceMind targeted tests: 13 passed
```

40 个 warning 来自现有/第三方 LangGraph、Pydantic、Starlette 和 Google SDK deprecation，不是 Phase 2 测试失败。应在后续依赖升级阶段消除，但不影响本次功能判定。

### 13.2 空库迁移

临时创建全新 PostgreSQL 数据库并从零执行 `alembic upgrade head`：

```text
core tables sampled: 4/4
RLS policies: 9
append-only audit trigger: 1
revision: 0003_append_only_audit
```

临时验收数据库随后已删除，未影响本地业务数据。

### 13.3 最终自动化闭环

```text
PHASE 2 ENTERPRISE VERIFICATION PASSED
Run ID: f8905f46-d964-4cbd-8817-dae832235c8a
Verified GLPI Followup ID: 8
Webhook Run ID: 99f61069-6c5c-5d81-88de-38be7cae45bb
Concurrent Run ID: fe31e30e-14f8-4b55-9930-1f0ae85ffbf6
Concurrent GLPI Followup ID: 9
Concurrent marker copies: 1
```

### 13.4 宕机恢复

```text
PENDING recovery run: 22236e09-3619-44b0-9954-24d59953b414 -> succeeded
Approved-before-resume run: 2f0cfe7d-65c3-4a3e-9857-f143b758a0b1 -> succeeded
Approved-before-resume GLPI Followup: 7
run.recovered audit count: 1
```

### 13.5 浏览器验收

真实 GLPI 页面：

```text
http://127.0.0.1:8088/front/ticket.form.php?id=2
Title: Ticket - Acme VPN MFA outage - ID 2 - GLPI
Ticket timeline entries: 9
```

浏览器可见文本同时包含：

```text
[ServiceMind run=f8905f46-d964-4cbd-8817-dae832235c8a ...]
[ServiceMind run=fe31e30e-14f8-4b55-9930-1f0ae85ffbf6 ...]
by servicemind-acme
```

这证明变化确实存在于真实 GLPI UI，而非只存在于 Agent 返回值或 ServiceMind 数据库。

### 13.6 安全端点

```text
/v1/servicemind/glpi/health present in OpenAPI: true
/servicemind/glpi/health present in OpenAPI: false
Phase 2 health without JWT: HTTP 401
Upstream /info without AUTH_SECRET: HTTP 401
```

## 14. Phase 2 文件级改造清单

### 14.1 ServiceMind 新模块

```text
src/servicemind/
├── agents/
│   ├── data.py
│   ├── analysis.py
│   └── action.py
├── domain/models.py
├── integrations/glpi/
│   ├── client.py
│   ├── models.py
│   ├── resolver.py
│   └── tools.py
├── orchestration/
│   ├── state.py
│   ├── workflow.py
│   └── runtime.py
├── persistence/
│   ├── models.py
│   ├── database.py
│   └── repository.py
├── security/
│   ├── auth.py
│   └── crypto.py
├── harness/
│   ├── executor.py
│   ├── recovery.py
│   └── webhooks.py
├── observability/tracing.py
└── api.py
```

### 14.2 基础设施与迁移

```text
deploy/glpi/compose.yaml
deploy/glpi/keycloak/servicemind-realm.json
deploy/glpi/postgres-init/01-runtime-role.sh
deploy/glpi/bootstrap_oauth.php
deploy/glpi/bootstrap_phase2.php
deploy/glpi/bootstrap_webhooks.php
deploy/glpi/local_define.php
alembic.ini
migrations/env.py
migrations/script.py.mako
migrations/versions/0001_phase2_enterprise_core.py
migrations/versions/0002_tenant_webhook_secrets.py
migrations/versions/0003_append_only_audit.py
```

### 14.3 启动和验收

```text
start-servicemind.ps1
stop-servicemind.ps1
start-local.ps1
stop-local.ps1
scripts/demo_phase2.ps1
scripts/init_langgraph_postgres.py
scripts/seed_phase2.py
scripts/verify_phase2.ps1
scripts/verify_phase2_database.py
scripts/verify_phase2_concurrency.py
```

### 14.4 上游集成改动

- `src/service/service.py`：挂载 Phase 2 Router、配置 Checkpointer、启动恢复、Telemetry 生命周期；
- `src/memory/postgres.py`：支持关闭 runtime 自动建表，落实 migration/runtime 账户分权；
- `src/run_service.py`：Windows Python 3.14 使用 SelectorEventLoop，兼容 psycopg async；
- `src/core/settings.py`：Phase 2 DB/OIDC/GLPI/Webhook/OTel 配置；
- `.env.example`：对应非敏感配置模板；
- `pyproject.toml`、`uv.lock`：Alembic、SQLAlchemy Async、PyJWT Crypto、OTel SDK/Exporter 直接依赖；
- `.gitignore`：排除本地部署 Secrets；
- `tests/conftest.py`、`tests/service/conftest.py`：Windows 测试环境和部署认证隔离；
- `tests/app/test_streamlit_app.py`：Windows 首次 Streamlit AppTest 超时容忍。

### 14.5 测试

```text
tests/servicemind/test_glpi_client.py
tests/servicemind/test_phase2_contracts.py
tests/servicemind/test_webhooks.py
tests/servicemind/test_recovery.py
```

## 15. 企业要求矩阵

| 领域 | Phase 2 状态 | 证据 |
| --- | --- | --- |
| 真实 ITSM 集成 | 通过 | GLPI 11 API + 页面 Followup #8/#9 |
| 身份认证 | 通过 | Keycloak JWT，JWKS，issuer/audience/expiry |
| RBAC/ABAC | 通过 | analyst/approver + Entity allowlist |
| 多租户隔离 | 通过 | FORCE RLS + 独立 GLPI Entity/User |
| 最小权限 | 通过 | migration/runtime 分权，Action Agent 无凭据 |
| Durable State | 通过 | PostgreSQL Checkpointer + AgentRun/Event |
| HITL | 通过 | interrupt/resume + approver token |
| TOCTOU 防护 | 通过 | canonical ActionIntent + action hash |
| Webhook 安全 | 通过 | HMAC、timestamp、entity match、SSRF allowlist |
| Webhook 幂等 | 通过 | 单事务收据 + deterministic UUIDv5 Run |
| 并发写安全 | 通过 | approval row lock + advisory lock；双请求只写一次 |
| 崩溃恢复 | 通过 | PENDING 与 approval-before-resume 两类恢复实测 |
| 写后验证 | 通过 | GLPI Followup read-back marker |
| 审计 | 通过 | structured + DB append-only trigger |
| Trace 接入 | 通过（代码路径） | OTel Agent/Tool spans + optional OTLP batch export |
| Metrics/SLO Dashboard | 未完成 | 后续 Prometheus/Grafana |
| Production Secrets | 未完成 | 本地 Fernet/.env；生产需 Vault/KMS |
| HA Worker/Queue | 未完成 | 当前 startup recovery；生产需 durable worker lease/queue |
| Rate Limit/Circuit Breaker | 未完成 | 后续 Reliability 阶段 |
| TLS/Keycloak Production | 未完成 | 当前 localhost + Keycloak start-dev |
| Backup/DR | 未完成 | 后续部署阶段 |
| 1+5 Multi-Agent | 未完成 | 当前完成 Data/Analysis/Action 垂直切片 |
| GLPI MCP Server | 未完成 | 最终方案保留，Phase 2 使用 Native Adapter |

## 16. 安全不变量

Phase 2 代码与验收应持续保持以下不变量：

1. 客户端不能通过 Body 或 Query 覆盖 `tenant_id`；
2. 没有 `app.tenant_id` 的 runtime 事务看不到任何租户数据；
3. Data/Analysis Agent 永远没有 GLPI 写凭据；
4. LLM 输出永远不能直接触发外部写入；
5. 写入前必须存在持久化 ActionIntent；
6. 审批必须来自 approver 且绑定相同 action hash；
7. 未审批的 `waiting_approval` 不能被恢复器自动执行；
8. 同一幂等键并发执行最多产生一个可观察 GLPI 副作用；
9. 外部写入必须 read-after-write 验证；
10. 应用运行角色不能更新或删除既有 AuditEvent；
11. 密钥、Token、密码不能进入 State、RunEvent、Audit payload 或 Trace attribute；
12. 跨租户资源统一返回 404，减少资源枚举信息泄漏。

## 17. 尚不能声称完成的能力

为了保证简历与面试表述可信，以下内容不能写成 Phase 2 已完成：

- 完整 Supervisor + Knowledge + Data + Analysis + Reviewer + Action 的 1+5；
- Task DAG、parallel fan-out/join、replan、handoff 安全边界；
- 企业 Hybrid RAG、BM25/RRF/Reranker、Citation 和 ACL Retrieval；
- Graph-RAG/Neo4j；
- 长期 Memory Writer、TTL、Conflict、Poisoning 防御；
- 统一 Tool Registry/Policy Engine；
- GLPI MCP Server 与企业 MCP Gateway；
- Prometheus/Grafana、SLO 与告警；
- Agent/Tool/RAG/Safety Benchmark 和 CI Quality Gate；
- Redis 限流、分布式 worker lease、circuit breaker、bulkhead；
- Kubernetes、HA、TLS、Vault/KMS、PITR/灾备。

Phase 2 已经形成 Harness 的真实骨架：Identity、Tenant Context、State、Checkpoint、Approval、Controlled Executor、Idempotency、Recovery、Audit、Trace。最终 Harness 会在此基础上继续加入 Tool Registry、Policy、Budget、Loop Guard、Memory、MCP 和 Evaluation。

## 18. 下一阶段建议

按主方案，下一阶段应优先完成最终 1+5 中缺失的三个控制/认知能力，而不是继续增加 GLPI 写接口：

1. Supervisor：Fast Path + Plan-and-Execute + Task DAG；
2. Knowledge Agent：租户 ACL 的最小 Hybrid RAG；
3. Reviewer Agent：Evidence/Policy/Prompt Injection/Approval 判定；
4. 将 Data 与 Knowledge 作为可并行子图，Analysis 在 Join 后执行；
5. Reviewer fail 返回 Supervisor replan；
6. Supervisor 到 Action 使用显式 Handoff，复用本 Phase 2 Harness；
7. 给上述路径增加 trajectory、routing、security golden tests。

MCP 不应为了“技术清单”立即替换稳定的 Native Adapter。正确顺序是先冻结 Tool Contract/Policy，再让同一 Tool Gateway 同时支持 Native GLPI Provider 与 GLPI MCP Provider，最后做 contract parity test。

## 19. 可写入简历的真实亮点

可使用以下表述，并在面试时展示本文证据：

> 基于 LangGraph/FastAPI 二次开发企业 ITSM Agent ServiceMind，对接真实 GLPI 11，构建 Keycloak JWT + PostgreSQL FORCE RLS + GLPI Entity 三层租户隔离；设计 ActionIntent、SHA-256 审批绑定、LangGraph interrupt/resume 和确定性写入 Harness，通过事务幂等、PostgreSQL advisory lock、业务 Marker 对账及 read-after-write 实现并发/崩溃场景下的 exactly-once 可观察副作用；完成 HMAC Webhook、启动恢复、append-only Audit、SSE replay 与 OTel Trace，真实双并发审批仅产生一条 GLPI Followup，项目回归 203 tests passed。

不要写：

> 已完成生产级 1+5、MCP、RAG、Memory、Kubernetes 全栈平台。

因为这些属于明确记录的下一阶段，而不是 Phase 2 已交付事实。
