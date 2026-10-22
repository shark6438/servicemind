# 免费模型测评：隔离、溯源与正常任务失败定位

日期：2026-10-22。范围：本轮约定的前 1～2 个工程人日工作；不替代原安全方案的最终完整验收。

## 本阶段交付

- 可复用入口：`scripts/local_model_evaluation.py`。
- 独立的应用 PostgreSQL、LangGraph PostgreSQL 检查点与 store、Redis、Neo4j、OpenSearch。测评不连接主服务的数据库或检查点，不通过关闭恢复功能掩盖共享存储问题。
- 固定的本地模型代理与模型白名单；未配置商业模型密钥，不调用付费模型。
- 每次调用的独立 HTTP 开始/完成记录，与 PostgreSQL 模型台账按 Run、request_id、角色、模型、首次提示哈希和尝试次数对账。缺失、替换、隐藏重试、尚在执行的调用均不能作为完整通过证据。
- 原 ACC-01 的 300 秒验收预算、鉴权、证据审核和副作用断言保持原样。

本轮证据目录为 `evaluation/security/completion_2026-10-22_r3/`，吞吐排查与不可达判定的证据目录为 `evaluation/security/completion_2026-10-22_r4/`。r2 及此前封存证据保持原字节。r2 的清单共 440 个文件，其中 439 个仍逐字节相符；唯一变化的是 `docs/SECURITY_ASSESSMENT_EXECUTION_PLAN_2026-10-22.md`——冻结时向其追加了 §15「交付冻结状态」，属文档漂移，不涉及任何证据文件（详见 r4 `README.md` 的「与 r2 封存的关系」）。

## 已确认的原因与修复

| 原因 | 原始证据 | 本轮处理 |
| --- | --- | --- |
| 共享持久化使另一 API 的恢复进程接管测评 Run | r2 `model-validity-correction.json` | 数据与检查点分别指向独立 PostgreSQL；新增环境预检与调用溯源门禁 |
| Supervisor / Planner 忽略配置超时，调用上下文始终使用默认 30 秒 | 原失败 Run 的 Planner 调用各约 60 秒、2 次尝试 | 控制上下文读取配置，并受剩余 Run 截止时间约束 |
| 网关每次重试重新获得完整超时额度 | 原网关使用 `asyncio.timeout(context.timeout_seconds)` 包围每次尝试 | 整次调用及其重试、回退共享同一总预算 |
| Data 任务外层 20 秒截止先于模型规划失败回退 | 原失败 Run 的 `Task T1 failed`，随后进入重规划 | 默认给 Data 规划 8 秒，保留执行已授权只读工具的时间；回退结果明确标记为 degraded |
| 检索改写存在同样的内外超时冲突 | 真实 CPU 测评中的改写取消 | 改写默认 8 秒，随后由既有确定性检索完成证据获取 |
| 超时被当成方案形状错误，再向外层规划器重复提问 | 原失败 Run 连续三次 `replanner.proposal_rejected: TimeoutError` | 超时不再作为形状纠正重问；失败尝试计入运行计数，形状错误仍可受限纠正 |
| Supervisor 使用全局动作枚举，动作与 task ID 可不匹配 | 控制平面原使用固定 `SupervisorDecision` schema | 根据当前合法选项生成 schema，并验证完整 action/task ID 配对；保留原确定性策略校验 |
| 结构化输出包装丢弃调用层追踪请求头 | 本轮首个 SDK / 代理试验 | 在结构化模型绑定层传入；新增真实 SDK + MockTransport 的 HTTP 回归验证 |

原 CPU 失败约 300 秒由多段调用累加产生，不能据此认定系统存在固定的“300 秒全局运行截止”。ACC-01 自身的验收截止确为 300 秒。

## 实际测评与有效性

**结论：本轮没有通过业务效用验收。原因是模型吞吐在 300 秒预算内不可达，不是上下文超窗，也不是上下文预算未接线。** 完整证据见 `evaluation/security/completion_2026-10-22_r4/README.md` 与 `observations.json`。

### 三个必须先纠正的口径

1. **模型台账中的 `input_tokens` 是 UTF-8 字节上界，不是 token 数。** 定义见 `src/servicemind/model_gateway/gateway.py:276-284`（`len(str(value).encode("utf-8"))`）。因此 r4-c 记录的 28,080 与 CPU 日志的「约 1.1 万 token」并不矛盾，二者是同一输入的两个量纲。
2. **不存在上下文溢出。** 真实 token 数低于 `--ctx-size 16384`；此前「超窗 2.1 倍、物理不可能」的判断作废。
3. **上下文预算确实生效。** `environment.json` 中的 `SERVICEMIND_CONTEXT_ENABLED` 为 `true`，且运行中出现 `context.assembly_failed` 事件，说明装配路径被真实执行，而不是被关闭后回退到无界全量证据。

### 实测吞吐（本机 CPU，`--parallel 1`）

| 线程 | prompt token | 生成 token | prefill | decode | 墙钟 |
| --- | --- | --- | --- | --- | --- |
| 12 | 465 | 32 | 76.9 tok/s | 15.4 tok/s | 8.3 s |
| 32 | 465 | 64 | 95.3 tok/s | 14.4 tok/s | 9.3 s |

加核只加快 prefill，decode 不随之提升（略降），说明解码受内存带宽限制，**增加 CPU 不是解法**。

### 三次运行

| 标签 | 配置（输入预算 / 证据上限 / 调用超时） | 结果 | 用途 |
| --- | --- | --- | --- |
| `r4-a-8192-cap1200` | 8192 / 1200 / 240 s | FAIL，173.66 s | **无效配置，不作模型证据**：`ModelCallContext.timeout_seconds` 硬顶 180 s，240 s 触发 `ValidationError`，两个取证任务降级并触发重规划，运行被取消 |
| `r4-b-8192-cap1200-t180` | 8192 / 1200 / 180 s / 12 线程 | BLOCKED，300.57 s | 有效。分析节点约 158 s 才发出，300 s 截止时调用仍在执行 |
| `r4-c-32t-8192-cap1200-t180` | 同上 / 32 线程 | BLOCKED，300.35 s | 有效。分析输入 28,080 字节，在 180 s 单调用超时处输出 0 token；Reviewer 收到降级分析并转入重规划 |

### 预算算术

- 分析之前的墙钟：140～158 秒（计划、派发、两次取证降级、证据合并）。
- 分析单步：≥ 180 秒。
- 其后仍有 Reviewer 与 finalize。
- 因此在 300 秒预算内，**最乐观下界约 330 秒**；缩小上下文预算无解（8192 已是 Reviewer 能完成装配的下限，更小的 4096 会在 `required_item_exceeds_token_budget` 处终止运行）。

### 历史样本处置

`first-control` 是工程试跑，暴露了本轮引入的缺失 datetime 导入，已修复并补充回归；该样本不作为最终验证通过。`qualified-control-1` 保留 BLOCKED 与原始终态/延迟 FAIL 记录。首次 `provenance.json` 的 VALID 只覆盖已经结束的调用；`provenance-correction.json` 明确撤销完整 Run 通过解释。最终代理在发送之前记录调用开始，可识别仍在执行的请求。**所有失败、准备失败和阻塞样本均保留，未挑选成功样本替代。**

## 复跑与清理

前提：已有真实的 loopback llama.cpp `11440` 服务与用户私有的本地 key；GLPI、Keycloak、embedding/reranker 为现有只读测试依赖。测试复用已存在的工单与身份，不创建或修改 GLPI 工单、不改 Keycloak 授权。

```bash
uv run python scripts/local_model_evaluation.py setup
uv run python scripts/local_model_evaluation.py seed
uv run python scripts/local_model_evaluation.py proxy
# 另一个终端
uv run python scripts/local_model_evaluation.py api
# 等待服务启动后，另一个终端；标签必须全新
uv run python scripts/local_model_evaluation.py probe new-control-label
uv run python scripts/local_model_evaluation.py cleanup
```

环境含真实测试凭据，仅存用户私有 state 目录，目录 0700、环境文件 0600；不进入证据目录。清理只停止 PID 与启动时间均匹配的本轮进程，只删除带本轮专属标签的容器及其匿名卷。共享模型权重、主服务、GLPI、Keycloak 与主数据存储保留。

同一份副本不要同时用于数据写入回归与实时模型测评。重跑本批已经存在的标签会被拒绝；新一轮完整归档应使用新的证据目录。独立存储与调用溯源解决的是归因问题，不构成对恶意宿主机、共享 CPU 负载或模型整体鲁棒性的认证。

## 后续验收边界

仍须完成实际模型下稳定的正常任务（ACC-01）、ACC-03 语义根因分析、ACC-12b 流程记忆链路，以及冻结配置后的完整 28 例与原安全规格端到端验证。本阶段的任何通过数都不能替代这些结论。r4 已给出这四项在本机 300 秒预算下不可达的算术与实测依据；解除方式只有两条：放宽运行预算，或换用推理吞吐足以在预算内完成分析的服务（含 CUDA 可用且允许使用的机器）。
