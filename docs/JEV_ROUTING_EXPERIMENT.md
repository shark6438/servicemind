# Jev 入口路由对照实验

本实验只比较现有 `FastPathRouter` 与 TypeSafe Jev 的入口路由分类。不会调用
Supervisor、Planner、RAG、Analysis、Reviewer、GLPI 或业务数据库；不会替换生产路由。
规则侧无模型 API 费用。实验能够测量分类、延迟和路由费用，不能证明端到端业务降本。

## 固定范围

- TypeSafe 直连接口为 `https://api.typesafe.ai/v1/systemone`，模型为 `jev-1.14.0`。
- 本轮直连返回 HTTP 451（服务不支持当前地区）后，经用户指定改用 OpenRouter：
  `https://openrouter.ai/api/alpha/decisions`，模型为 `typesafe/jev-1.14`。
  接受该版本或其日期后缀版本，首个响应绑定实际版本，批次中变化即停止。
- 两个渠道均发送单个 `choice` 问题；渠道切换只改模型标识，问题描述与数据保持一致。
- 原规则保持不变；Jev 直接分类，不用规则答案修正或覆盖它的输出。
- 输入只包含用户问句和 `request_write`。人工/合成标签、理由、样例 ID、规则输出均不发送。
- 路由枚举保持四类。信息不足映射为 `complex_workflow`，不新增产品澄清节点。
- `confidence` 与完整概率分布保留；本轮不调置信度门槛、不训练模型。
- TypeSafe 的概率分布集中度与规则中固定的 confidence 常量不作等价比较。

## 数据与标签

| 数据 | 数量 | 用途与限制 |
| --- | ---: | --- |
| `routing.jsonl` | 107 | 既有规则回归集，与规则共同演进，不作为独立泛化证据 |
| `routing_hard.v1.jsonl` | 41 | 32 条契约样例、9 条边界样例，分开统计 |
| `routing_semantic_probe.v1.jsonl` | 64 | 新增中英文语义探针，每类 16 条；助手编写、未经人工复核，不称为人工金标或盲测集 |

新探针覆盖字段同义表达、文档查找、否定操作、复合需求、信息不足、故障诊断、写入标志、
破坏性操作、凭据披露和试图指定分类答案的输入。它们采用显式的语义预期：仅讨论或否定
删除操作与真正要求删除分开。这与既有困难集部分保守标签不同，因此不能合并成一个
“准确率提升”结论。旧标签不改写。新增语义标签在调用任何路由之前写入。

首次报告后不针对错误修改本轮问题描述或标签。若需要下一轮实验，应保留旧产物、增加
新版本，再提供未用于调参的复核样例。本轮是可行性筛查，没有业务上线门禁结论。

## 预算与失败处理

2026 年 10 月 28 日核对的 TypeSafe 标价为每百万输入 token **0.042 美元**，输出免费。
预算上限 **0.70 美元**，按规划汇率 6.67 折合约 **4.67 元人民币**，不含充值或换汇手续费。
212 次请求若平均总输入为 1500 token，估算约 **0.013356 美元 / 0.089 元人民币**；
TypeSafe 直连按返回的 `usage.input_tokens` 和标价结算；OpenRouter 按返回的
`usage.cost` 结算，同时保存输入与输出 token，不把这份估算当成账单。

- 默认只跑离线基线，必须显式加 `--live` 才调用所选渠道。
- 单并发，无自动重试、无重定向、无生成模型 fallback。
- 发送前将预留写入共享账本并 `fsync`。文件锁拒绝同一实验的并发写入者。
- TypeSafe 每次按 65536 输入 token 预留 0.002752512 美元；OpenRouter 按其公布的
  262144 token 上下文上限预留 0.011010048 美元，成功后按返回费用结算。
  不开启长上下文，只发送一个问题，应用再限制序列化请求不超过 12000 字节。
- 成功返回用量后结算，即使随后分类响应校验失败也计入费用。
- 超时、HTTP 错误或缺少用量时保留整笔预留并停止。未解决的预留阻止同渠道后续调用。
  显式切换渠道时仍沿用共享预算，旧渠道的未知预留继续占用预算，不自动退款或删除账本。
  本轮保留直连失败的 0.002752512 美元未知预留；这不是已确认扣款。
- 同一账本累计最多 300 次 HTTP 尝试；再次运行不会重置预算或次数。
- 价格上限依赖供应商按已核实标价与上下文限制计费；它不是平台账户的消费限额。
  服务返回超出上下文上限的用量会记录并停止。

## 执行

在仓库根目录运行，使用现有环境，不安装新 SDK：

```bash
.venv/bin/python scripts/compare_jev_routing.py
```

真实调用需要在项目 `.env` 中配置 `OPENROUTER_API_KEY`，或在当前进程环境中设置。
密钥不进入命令参数、报告、提示或版本库。配置完成后：

```bash
.venv/bin/python scripts/compare_jev_routing.py --live --provider openrouter
```

直连模式为 `--provider typesafe`（默认），读取 `TYPESAFE_API_KEY`；当前环境直连受到
地区限制，不应反复尝试。两平台密钥和账户余额独立，TypeSafe 余额不能作为 OpenRouter 余额使用。

可用 `--budget-usd 0.10` 降低上限；不允许超过 0.70 美元。降低的预算以首次创建账本时为准，
后续不能通过更大参数提高原始上限。重复执行真实实验会产生新的调用，首轮不应无故重跑。

产物位于 `evaluation/experiments/jev_router_2026-10-28/`：每次运行创建新目录，包含
`manifest.json`、`report.json`、`report.md`、`receipts/`，不覆盖历史结果。
receipts 保存白名单字段中的模型、用量与决策响应，并移除可能回显的当前凭据。
`spend.jsonl` 是实验共享的
持久费用账本。manifest 在付费调用前写入数据、脚本、原路由和问题描述的摘要及 Git HEAD。
报告逐例保存分类、概率、延迟、计费用量和失败状态，每个成功响应后更新一次。

报告分数据集及标签强度展示混淆矩阵、正确数、观察覆盖数、危险快速路径数量、P50/P95
延迟和成对改善/退步数。未调用或失败样例保留在完整分母；单独给出已观察样例的准确率。
没有真实 Jev 结果时，其准确率与延迟为 null，报告明确为 `BASELINE_ONLY` 或 `BLOCKED`。
本地规则与远程 Jev 的延迟均为进程内实测墙钟时间，Jev 包含网络往返。

## 验证与资料

离线测试使用 HTTP 替身，覆盖预算预留、重启后的预算延续、未知费用保留、禁止自动重试、
缺失凭据、异常响应、标签隔离及缺失样例分母，不产生模型 API 费用：

```bash
PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest tests/servicemind/test_jev_routing_experiment.py -q
```

- [TypeSafe 模型和价格](https://docs.typesafe.ai/models)
- [System One API](https://docs.typesafe.ai/api/system-one)
- [Choice 请求与响应](https://docs.typesafe.ai/primitives/choice)
- [置信度含义](https://docs.typesafe.ai/confidence)
- [OpenRouter Jev 接入说明](https://openrouter.ai/docs/guides/community/jev)
- [OpenRouter Jev 请求与返回示例](https://openrouter.ai/docs/guides/community/jev-tutorial)
- [OpenRouter Jev 1.14 模型和价格](https://openrouter.ai/typesafe/jev-1.14)
