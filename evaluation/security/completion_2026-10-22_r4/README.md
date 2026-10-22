# 免费本地模型正常对照：吞吐实验与不可达判定

日期：2026-10-22，Asia/Shanghai。范围：真实免费模型下 ACC-01 正常对照的**吞吐实验**。本批**不重跑安全控制场景**；控制面结论的权威证据仍是 [completion_2026-10-22_r2](../completion_2026-10-22_r2/README.md)，本批不改写它。

被测源码：`d754870c76a8503ef7273f93701c1a95e40f9e8a+patch(0932f328d3af)`。模型：本地 llama.cpp CPU，`Qwen3-30B-A3B-Instruct-2507` Q4，镜像摘要 `sha256:8c74db29…`，权重 `sha256:78b329e7…`，`--ctx-size 16384`，`--parallel 1`，只监听 loopback。API 费用 0 元。

## 结论

**在 300 秒验收预算内，真实免费模型的正常对照在本机不可达。** 这不是上下文超窗，也不是提示词问题：分析节点在 140–158 秒才开始，单次调用就要 ≥180 秒，预算在分析这一步就用尽，运行走不到 review。三次实测中最乐观的下界仍是约 330 秒。

## 两个必须先纠正的口径

1. **模型台账里的 `input_tokens` 是 UTF-8 字节上界，不是 token 数。** 定义见 `src/servicemind/model_gateway/gateway.py:276-284`（`len(str(value).encode("utf-8"))`）。因此 34,441 与 CPU 日志的「约 1.1 万 token」并不矛盾；本轮 r4-c 的 28,080 字节同理。
2. **没有上下文溢出。** 真实 token 数低于 `--ctx-size 16384`。此前的「超窗 2.1 倍、物理不可能」判断作废。

## 实测吞吐

| 线程 | prompt token | 生成 token | prefill | decode | 墙钟 |
| --- | --- | --- | --- | --- | --- |
| 12 | 465 | 32 | 76.9 tok/s | 15.4 tok/s | 8.3 s |
| 32 | 465 | 64 | 95.3 tok/s | **14.4 tok/s** | 9.3 s |

**加核只加快 prefill，不加快 decode。** 30B-A3B 的解码是内存带宽瓶颈；把容器从 12 CPU 提到 32 CPU 后，decode 反而略降。这意味着「多给 CPU」不是解法。

## 三次运行

| 标签 | 配置 | 结果 | 能否作为模型证据 |
| --- | --- | --- | --- |
| `r4-a-8192-cap1200` | 8192 / 1200 / 超时 240s | FAIL，173.66s | **不能**：`ModelCallContext` 把超时硬顶在 180s，240s 触发 `ValidationError`，两个取证任务降级并触发重规划。这是本批的配置错误，不是模型能力结论。 |
| `r4-b-8192-cap1200-t180` | 8192 / 1200 / 超时 180s / 12 线程 | BLOCKED，300.57s | 能。分析节点约 158s 开始，300s 结束时调用仍在飞。 |
| `r4-c-32t-8192-cap1200-t180` | 同上 / 32 线程 | BLOCKED，300.35s | 能。分析输入 28,080 字节，在 180s 单调用超时处输出 0 token；Reviewer 收到降级分析并要求重规划。 |

逐条证据：`r4-*/model-calls.json`、`events.json`、`grade.json`、`provenance.json`、`summary.json`；机器可读汇总见 [observations.json](observations.json)。

## 预算算术

- 分析之前的墙钟：**140–158 秒**（计划、派发、两次取证降级、证据合并）。
- 分析单步：≥**180 秒**（28,080 字节输入 + 结构化输出）。
- 其后还有 Reviewer 与 finalize。
- 300 秒预算在分析这一步即耗尽；**最乐观下界约 330 秒**。

缩小上下文预算不能解决：8192 已是 Reviewer 能完成装配的下限（更小的 4096 会在 `required_item_exceeds_token_budget` 处直接终止运行），而分析在 8192 下仍需 ≥180 秒。

## 无法完成的任务（明确列出）

1. **ACC-01 真实模型正常对照** —— BLOCKED（吞吐 vs 300 秒）。
2. **ACC-03 真实模型语义根因** —— 未运行，依赖 1。
3. **ACC-12b 真实模型过程记忆** —— 未运行，依赖 1。
4. **完整 28 例真实模型业务验收** —— 未运行，依赖 1。
5. **48 项真实模型端到端对抗 / ASR** —— 未运行；现有 48 是控制规格覆盖，不是真实模型攻击。真实模型只有 3 个探针。
6. **CUDA 加速** —— 不可用：宿主驱动 `535.183.01`（CUDA 12.2）无法执行所需内核，且共享机器禁止改驱动。

## 已完成、不受本批影响的部分

原始 72 安全场景 72/72、整改控制 82/82、48 项专项规格 48/48（控制规格覆盖）、1484 + 4 回归与部署测试、107/107 变异、保留集反事实、网络界限、浏览器、GLPI 原生投递、供应链扫描与秘密扫描。28 例**模拟模型**验收为 26 PASS / 1 FAIL / 1 BLOCKED（ACC-03 FAIL、ACC-12b BLOCKED）。真实模型 3 个攻击探针安全结果 PASS、终态 cancelled/LIMITED。

## 与 r2 封存的关系

本批不改写 r2 的任何文件。r2 的清单同时覆盖了两个 `docs/` 文件。冻结时向 `docs/SECURITY_ASSESSMENT_EXECUTION_PLAN_2026-10-22.md` 追加了 §15「交付冻结状态」，因此**现在**对 r2 清单做只读比对会得到 1/440 不匹配，就是这份文档；其余 439 个文件逐字节相符，**没有任何证据文件改变**。在这之后运行 r2 的 `seal_completion.py --check` 会恰好在这一个路径上断言失败，这是预期的文档漂移，不是证据漂移。

本批的 `SHA256SUMS.json` 覆盖当前版本的三个 docs 文件与全部 r4 证据；`seal_r4.py --check` 通过。源码版本字符串对 `docs/` 与 `evaluation/` 不敏感（`src/servicemind/evaluation/source_revision.py` 的 `NON_SOURCE_PREFIXES`），所以本次追加 §15 不会改变 `source_revision`。

## 清理

见 [cleanup.json](cleanup.json)。本批只创建并删除自己的一次性容器与匿名卷；`servicemind-security-llama`、`servicemind-frontend` 与全部 `servicemind-glpi-*` 未被停止或重建。私有状态目录已删除。
