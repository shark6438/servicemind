# 后端能力升级与前后对照（2026-11-05）

本次围绕已有 Agent 运维流程，补齐独立执行、租户资源控制、数据库查询、实时进度和运行指标。
验证使用模拟模型、隔离 PostgreSQL 与 Redis，没有新增付费模型调用。

## 具体修改

### 1. 独立 Agent Worker

- 新增 `servicemind-run-worker` 入口。Worker 模式下，API 提交任务后返回 202，由独立进程领取执行。
- `agent_runs` 是 PostgreSQL 持久任务队列；原有事务 Outbox / Redis Streams 继续承担工具事件投递。
- 增加任务租约、心跳、任务执行锁及租户执行槽。多个 Worker 协调领取，按租户轮转扫描。
- 执行锁使用独立连接，避免长任务占据业务查询连接池。进程被终止后，存活者可在租约到期后接管。
- 等待人工审批或复核时释放执行槽；已记录审批决定的任务可以恢复，未作决定的任务保持暂停。
- 审批和复核的 HTTP 续跑仍同步执行，并取得同一执行锁，防止与 Worker 并发运行同一个图。

实现：[执行器](../src/servicemind/orchestration/run_worker.py)、[数据库队列](../src/servicemind/persistence/run_queue.py)、
[进程入口](../src/service/run_worker.py)、[操作说明](BACKEND_RUN_WORKER.md)。

### 2. 租户准入与权限

- 新增租户待执行任务上限，容量检查与任务插入在同一事务内串行完成。
- 队列满时创建任务或新 webhook 返回 `429` 和 `Retry-After: 5`；重复 webhook 仍返回原任务。
- 被拒绝的新 webhook 不留下孤立的 receipt、任务或初始事件。
- 新任务执行前重新验证 analyst 角色，实体和组权限取提交时与执行时权限的交集。
- 身份服务不可用时延迟重试；恢复已批准动作时重新检查请求者权限。
- webhook 执行依据事务内写入的首条来源事件识别，禁止写入；同名用户 subject 无法绕过身份验证。

实现：[任务仓库](../src/servicemind/persistence/repository.py)、[HTTP 接口](../src/servicemind/api.py)、
[执行配置](../src/servicemind/orchestration/execution_config.py)。

### 3. 数据库查询与事务

- 增加租户、创建时间、任务 ID 组合索引，以及仅覆盖可恢复状态的部分索引。
- 事件流每批读取 100 条，旧启动恢复实际采用每批 100 条的时间与 ID 游标分页。
- Worker 模式下，任务与 `run.created` 初始事件原子提交，事件写入失败会回滚任务。
- 新增迁移 `0016_run_queue_paging`，包含执行租约字段与索引。

实现：[迁移](../migrations/versions/0016_run_queue_paging.py)、[基准脚本](../scripts/benchmark_run_query_paging.py)。

### 4. 实时运行进度

- SSE 从一次历史快照升级为持续读取新事件，支持 `Last-Event-ID`、心跳、断开检测和有界补读。
- 终态状态提前提交时等待对应事件，避免按固定一秒宽限期结束；遇到终态事件后先排空已提交批次。
- 单连接最长 60 秒，超时提示重连，不误发完成标记。
- 运行详情页接入带 Bearer Token 的 fetch 事件流，按序号合并去重，切换运行时取消旧订阅。
- 保留 SWR 快照和 10 秒轮询作为降级；事件流更新运行时间线，运行状态刷新按 250 ms 合并。

实现：[事件接口](../src/servicemind/api.py)、[前端订阅](../frontend/src/hooks/use-live-run.ts)、
[流解析](../frontend/src/lib/run-events.ts)。

### 5. 可观测性

- 增加 HTTP 返回响应头前耗时，避免将后台任务或 SSE 连接时间算成 API 准入延迟。
- 增加路由耗时、任务排队时间、执行尝试耗时与恢复结果指标。
- 指标使用固定类别和路由模板，不把用户请求、任务 ID 或租户名称作为指标标签。
- 复用已有 OTLP Collector 配置导出指标；未配置 Collector 时不产生网络导出。

实现：[HTTP 计时](../src/servicemind/observability/http.py)、[指标](../src/servicemind/observability/metrics.py)、
[OTLP 配置](../src/servicemind/observability/tracing.py)。

设置 `SERVICEMIND_OTEL_ENDPOINT=http://collector:4318/v1/traces` 后，默认向同一 Collector 的
`/v1/metrics` 导出指标；可以用标准 `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` 指定其他指标端点。
执行耗时按一次执行尝试计算，不包含人工审批等待，也不等于整个任务生命周期。

## 实现前后对照

| 项目 | 实现前 | 实现后 |
| --- | --- | --- |
| Agent 执行 | 与 API 共用进程，启动时恢复未完成任务 | 可独立部署 Worker，数据库租约与执行锁协调 |
| 租户执行资源 | 无跨进程 run 并发额度 | 共享租户执行槽、轮转领取、积压上限与 429 |
| 事件推送 | 发送已有事件后结束 | 持续推送、断线续读，详情页实际订阅 |
| 恢复扫描 | 一次加载全部匹配任务 | 100 条一批，稳定游标遍历 |
| 数据库观测 | 有功能测试 | 新增执行计划、固定数据集及可复现对照 |
| 运行指标 | 已有部分链路追踪 | 新增准入、排队、路由、执行和恢复指标 |

数据库对照在 PostgreSQL 16.15 的隔离临时表上运行：12 万条模拟任务、5 个租户、6% 非终态，
以及一条含 3 万事件的时间线。每组五次暖缓存观测，以下为中位数：

| 查询 | 实现前 | 实现后 |
| --- | ---: | ---: |
| 最近 100 条任务，完整行查询 | 28.480 ms | 0.088 ms |
| 最早 100 条待恢复任务，完整行查询 | 11.794 ms | 0.116 ms |
| 事件读取 | 全量 30,000 条，59.724 ms | 首批 100 条，0.744 ms |
| 单批事件载荷 | 23.04 MB | 76.8 KB |

任务查询比较保持返回行数相同；事件比较衡量首次有界读取，不能解释成全部 3 万事件的传输加速。
这些值不含真实 ACL/RLS 负载、索引写入成本或模型调用，不能表示生产端到端加速。
完整方法与原始结果见 [数据库报告](BACKEND_DB_PAGING_RESULTS.md) 和
[JSON](../evaluation/reports/db_paging_before_after.json)。

Worker 故障测试实际启动子进程持有数据库锁与两秒测试租约，再发送 SIGKILL；存活者在租约到期后
重新领取，旧持有者无法续租，终态任务不会再次领取。这是故障恢复正确性证据，未将等待时间作为
性能基准。部署默认租约为 120 秒，实际接管时间受租约、轮询、数据库连接释放与 checkpoint 影响。

## 验证与适用范围

完整验证清单和最终计数保存于
[backend_upgrade_validation_2026-11-05.json](../evaluation/reports/backend_upgrade_validation_2026-11-05.json)。

| 检查 | 最终结果 |
| --- | --- |
| Python 全仓默认回归 | 1,637 passed，48 skipped |
| 隔离 PostgreSQL，受限应用角色 | 37 passed |
| 隔离 Redis | 3 passed |
| 全仓 Ruff 格式与规则 | 通过，492 个 Python 文件格式检查 |
| 全仓 Pyrefly | 0 errors |
| 架构契约与报告一致性 | PASS |
| 前端 lint、类型、单测、生产构建 | 全通过，15 个单测 |
| 空库迁移与 Worker 启停 | 通过 |

默认回归跳过需要显式启用的外部依赖测试；其中 PostgreSQL 与 Redis 部分另行在真实隔离服务上执行。
其余依赖完整已部署栈、Neo4j 或 OpenSearch 的测试没有在本轮全部重跑。
PostgreSQL 测试使用隔离数据库和 `NOSUPERUSER NOBYPASSRLS` 应用角色，包含租户隔离、真实并发领取、
准入竞争与 SIGKILL 接管；Redis 测试使用独立容器。前端使用 Node 24 与锁定依赖完成 lint、类型检查、
单元测试和 Next.js 16.4.2 生产构建。迁移在空数据库中完整升级、降级再升级，并检查无 schema drift。
实际安装的 Worker 入口也在空数据库完成 checkpointer 初始化、启动和 SIGTERM 正常退出。

默认执行模式仍为兼容原部署的 `in_process`。启用独立执行需要先迁移目标数据库，再让 API 与 Worker
使用相同数据库、身份配置及租户额度；具体命令见 [部署说明](BACKEND_RUN_WORKER.md)。本次没有重启现有部署。
已有部署的只读检查结果为 API `/health` 返回 `status=ok`、前端 HTTP 200。

本次没有重跑付费模型或真实 GLPI 写入的完整端到端流程，不能据此宣称已验证所有外部服务故障。
进程中断可能重复尚未形成 checkpoint 的模型或只读调用，业务写入仍依靠现有审批、幂等与写后校验。
当前 SSE 使用数据库轮询；高连接数需要另行测量。终态之后才提交的附加后台事件需要重新读取才能补齐。
自动测试能覆盖已验证的路径，不能证明项目不存在任何 bug。

## 简历可用表述

> **异步任务与可靠执行：** 将 Agent 长任务解耦至独立 Worker，基于 PostgreSQL 持久队列、租约心跳及
> 跨进程执行锁实现多实例领取与故障接管；增加租户并发和积压额度，通过真实进程终止及并发竞争测试验证恢复与隔离。
>
> **数据访问与运行观测：** 优化租户任务查询索引与恢复游标分页，在 12 万条模拟任务基准中将最近任务查询
> 中位耗时由 28.48 ms 降至 0.088 ms；接入 SSE 断线续读与 OpenTelemetry 准入、排队和执行指标。
