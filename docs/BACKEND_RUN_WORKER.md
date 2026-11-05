# 独立 Agent Worker 与租户执行额度

Agent run 可以由独立进程执行。API 在 PostgreSQL 中提交任务和初始事件后返回
202；Worker 从 `agent_runs` 领取任务。默认 `in_process` 模式保留原部署方式。

## 启动

API 与 Worker 必须使用相同的 ServiceMind 数据库、LangGraph checkpointer 数据库、
身份提供方配置，以及相同的租户执行额度。先在目标数据库执行迁移：

当前版本还包含任务提交幂等迁移 `0017_run_submission_idempotency`，新版表单会发送幂等键；
应升级至完整的迁移 head，而非只应用 Worker 最初的 0016。

```bash
uv sync --frozen
uv run alembic upgrade head
```

在 API 和 Worker 的环境中配置：

```dotenv
DATABASE_TYPE=postgres
SERVICEMIND_RUN_EXECUTION_MODE=worker
SERVICEMIND_RUN_QUEUE_LIMIT_PER_TENANT=100
SERVICEMIND_RUN_WORKER_PER_TENANT_LIMIT=2
SERVICEMIND_RUN_WORKER_CONCURRENCY=4
SERVICEMIND_RUN_WORKER_LEASE_SECONDS=120
SERVICEMIND_RUN_WORKER_POLL_SECONDS=1
```

已有 PostgreSQL 连接和 OIDC 身份验证配置也必须有效。checkpointer 表首次使用前，
由现有初始化流程创建；`POSTGRES_AUTO_SETUP=true` 时 Worker 也会执行其 setup。

分别启动两个进程：

```bash
uv run servicemind-api
uv run servicemind-run-worker
```

systemd 使用 `deploy/systemd/servicemind-run-worker.service`；将
`deploy/systemd/servicemind-api.service.d/worker.conf` 安装为 API 单元的 drop-in，
才能同时切换 API 的执行模式。仓库只提供可审查配置，不会自动重启现有部署。

## 执行与恢复

- PostgreSQL 任务行是持久队列。任务提交后不依赖 API 进程内存，也不依赖 Redis 通知。
  原有事务 Outbox / Redis Streams 仍用于工具事件投递。
- 数据库事务锁将同一租户的容量检查与领取串行化；行租约记录持有者和有效期。
  Worker 按租户轮转领取，避免总从同一个租户开始扫描。
- 整个图调用还持有 run 锁和租户执行槽的 PostgreSQL session advisory lock。
  即使心跳迟到导致租约过期，存活的执行者仍占据执行槽。锁连接使用独立 NullPool，
  避免长任务占满业务查询连接池；部署时应为这些额外连接规划数据库连接数。
- 心跳无法续租时取消本地图调用；进程退出后 session 锁由数据库释放，其他 Worker
  在租约到期后可以领取同一任务，从持久化 checkpoint 恢复。
- 队列达到租户上限时，创建任务与新 webhook 返回 429 和 `Retry-After: 5`。
  同一 webhook 的重复投递仍返回原任务，不因队列已满拒绝。
- 人工请求在执行前重新验证 analyst 角色，并取提交时与当前实体/组权限的交集。
  身份服务不可用时任务延迟 60 秒再尝试；不会虚构权限继续运行。
- webhook 服务身份必须同时具有签名接入路径原子写入的首条 `webhook.accepted`
  事件，且任务保持只读；仅将用户 subject 命名为 `glpi-webhook` 不会跳过身份验证。
- 等待审批或人工复核的任务不占运行槽。审批/复核的 HTTP 续跑保留现有授权边界，
  在 Worker 模式下也取得同一执行锁。如果 API 在审批记录提交后退出，Worker 会
  找到已有决策的审批等待任务；未决策的审批等待任务不会被领取。
- 恢复批准动作前再次验证请求者权限；已失效的批准动作失败关闭，需新建任务。
  持久化的拒绝决策可以直接完成拒绝路径，不要求请求者仍能被身份服务查询。

SIGTERM 会停止领取新任务，并等待现有任务至 `GRACEFUL_SHUTDOWN_TIMEOUT`；超时后
取消执行并保留租约，供其他进程在到期后恢复。业务写操作继续由现有 PostgreSQL
幂等记录、审批动作哈希及写后校验约束。进程退出可能导致尚未形成 checkpoint 的
模型/只读工具调用再次发生，因此不承诺每个外部请求只发送一次。

## 验证与前后对照

`tests/servicemind/test_run_worker.py` 验证 API 只提交任务、队列满的 429、权限交集、
角色撤销、心跳失效和轮转领取。`test_run_queue_postgres.py` 使用真实 PostgreSQL
验证多领取者的租户上限、重复执行锁、审批决策恢复，以及杀死 Worker 子进程后由
存活者重新领取。测试使用模拟任务，不调用付费模型。

| 行为 | 原执行方式 | Worker 模式 |
|---|---|---|
| 接收任务 | 返回 202 后在 API 内运行 | 返回 202 后由独立进程领取 |
| API 发布 | 与任务执行共享进程生命周期 | 执行进程可以继续运行 |
| 多进程任务领取 | 启动恢复不负责统一租约 | 数据库租约和执行锁协调 |
| 租户运行额度 | 没有跨进程 run 额度 | 所有 Worker 共享数据库执行槽 |
| Worker 退出 | 不适用 | 真实子进程被杀后，存活者可在租约到期后重新领取 |

这是执行隔离与恢复正确性的对照；原接口本来就返回 202，本次不据此宣称 API 延迟
或模型吞吐量自动提升。实际 Agent 端到端耗时仍受模型与工具服务影响。
