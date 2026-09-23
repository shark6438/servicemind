# 记忆质量评测报告

- 语料：`servicemind-phase5-memory-v2`
- 打分模式：`lexical`
- 打分模型：`lexical-jaccard`
- 时间锚点：`2026-09-22T12:43:29.864419+00:00`

> 本语料由本项目编写，是对设计所声称保证的**回归契约**，不是任何租户真实记忆
> 分布的度量，也不能替代生产记忆流量回放。时间以锚点偏移表达。

## 1. 写侧治理

| 指标 | 值 |
| --- | --- |
| 步骤数 | 48 |
| 动作精确匹配率 | 1.0 |
| 落库状态匹配率 | 1.0 |
| 原因码覆盖率 | 1.0 |
| **越权激活** | 无 |
| 误拒 | 无 |

| 动作 | 支持数 | 精确率 | 召回率 |
| --- | --- | --- | --- |
| reject | 2 | 1.0 | 1.0 |
| quarantine | 10 | 1.0 | 1.0 |
| activate | 36 | 1.0 | 1.0 |

## 2. 读侧安全（与打分模式无关）

- 探针数：45
- **泄漏率：0.0**（0 条记录）
- 泄漏探针：无

| 类别 | 探针数 | 泄漏率 | 泄漏记录数 |
| --- | --- | --- | --- |
| as_of | 9 | 0.0 | 0 |
| injection | 7 | 0.0 | 0 |
| isolation | 9 | 0.0 | 0 |
| retrieval | 11 | 0.0 | 0 |
| staleness | 3 | 0.0 | 0 |
| taint | 6 | 0.0 | 0 |

## 3. 读侧排序

打分模式 `lexical`（模型 `lexical-jaccard`）；仅统计声明了 `must_surface` 的探针。

| k | 探针数 | Recall@k | MRR@k | NDCG@k |
| --- | --- | --- | --- | --- |
| 1 | 30 | 0.9444 | 1.0 | 1.0 |
| 3 | 30 | 1.0 | 1.0 | 1.0 |
| 5 | 30 | 1.0 | 1.0 | 1.0 |
| 8 | 30 | 1.0 | 1.0 | 1.0 |

### 3.1 排名门禁（声明式）

只统计显式声明 `max_rank` 的探针；未声明排名要求的探针不计入，
把它们算作通过会虚增门禁覆盖面。

- 受门禁探针数：29
- **判定：通过**

### 3.2 投递门禁（声明式）

前两节都止步于检索器。**检索不等于投递**：`ContextBuilder` 按 authority 排序后
丢弃超出 token 预算的可选项，而记忆（authority 0.7）排在 evidence（1.0）之后，
因此一条记忆可以被检索到、排在第 1 位，然后在进入提示前被裁掉——而上面每一行仍然全绿。

本节按真实 `ContextBuilder` 的**选择清单**判定：声明了 `must_deliver` 的探针，
其必需记录必须处于 `selected` 状态，而不只是被检索器返回。

- 受门禁探针数：4
- 声明的载荷：预算 12000 token、evidence 3 条（每条约 7000 字符）
- 载荷依据：按已配置的生产上限推导，不是对观测的拟合：知识包器预算 SERVICEMIND_RAG_CONTEXT_TOKENS=8000 token、单条父块上限 parent_max_chars=7000 字符，故一次填满自身预算的知识任务至少投递 2 条满长父块；joined 信封还叠加 data_task 的工单证据，故声明 3 条 x 7000 字符。分析信封预算取生产实际值 SERVICEMIND_CONTEXT_MAX_INPUT_TOKENS=12000；ANALYSIS 的 SERVICEMIND_CONTEXT_EVIDENCE_TOKEN_CAP=5000，REVIEWER 不应用该上限。此处声明的是要求：记忆必须能在该形态下存活。
- 记忆通道可支配余量：最紧探针 **5738** token
- 记忆通道实际占用：占用最多的探针 **1003** token
- **判定：通过**

> 载荷是**声明的运行形态假设，不是生产观测**：生产库中没有任何 `evidence.joined`
> 事件被记录过，真实 evidence 体积未知。此处的绿色不得读作生产可达性结论。

| 探针 | 记忆通道余量 | 实际占用 | 判定 |
| --- | --- | --- | --- |
| p-preference | 5749 | 502 | 送达 |
| p-procedure | 5738 | 765 | 送达 |
| p-fleet-vpn-causes | 5742 | 1003 | 送达 |
| p-post-run-tenant-episode | 5749 | 905 | 送达 |

## 4. 直接播种的记录（绕过写入策略）

以下记录由语料直接写入仓库，**未经过治理策略**，因此不构成任何写侧结论；
它们只用于陈述读侧前提（例如「迁移前已存在、带污点标签且仍为 ACTIVE」）。

`s-taint-settlement`, `s-taint-access-review`, `s-taint-capacity`, `s-taint-user-scope`, `s-taint-group-scope`, `s-inj-ignore-previous`, `s-inj-bypass-approval`
