# ServiceMind Phase 4 数据与测评基线 v1.2

> 制定日期：2026-09-08
> 适用项目：`/home/shihongye/data1/servicemind`
> 目的：判断现有数据是否足以证明 RAG、子 Agent 与 Multi-Agent 达到企业发布标准，并给出成本最低、可复现的补齐方案。

## 1. 结论

现有数据**足以关闭 Phase 4 工程范围并建立公开代理回归，不足以签发企业前沿质量认证**。项目方已明确无法取得真实生产查询日志和 ITSM 专家签署，因此域内质量门保留为 Phase 7 发布证据，不阻塞 Phase 5 工程启动。

| 测试目的 | 现状 | 判定 |
| --- | --- | --- |
| 摄入、切块、索引、ACL、引用链路回归 | 治理修正后生产为 39 文档、441 parent、466 child；PostgreSQL 与 OpenSearch active alias 数量一致 | 足够做链路回归 |
| 检索算法优劣比较 | 8 篇合成 SOP、15 条 query，其中 12 条可答、3 条不可答 | 不足 |
| 域内 RAG 企业发布定级 | 缺少来自 GLPI KB、同租户已解决 Ticket 和真实查询分布的冻结 qrels | 不足 |
| 通用技术 RAG 外部泛化 | 固定 TechQA revision 已恢复：910 rows、28,481 corpus files；400 条代理评测已完成 | 基线已建立；高阈值门禁未通过 |
| 子 Agent/Multi-Agent 长链任务 | EnterpriseOps-Gym ITSM 固定 revision 已恢复，103 个任务、4 个 tool-set 模式 | 可作 Phase 7 适配起点；尚无 GLPI final-state benchmark |
| 越权与提示注入 | AgentDojo 固定 revision 和 SHA-256 已恢复 | 可作 Phase 7 适配起点；当前仅有 ServiceMind 控制与回归证据 |
| 企业规模容量 | 当前生产索引为 466 child | 不足以外推到几十万或百万 chunk 的延迟、吞吐和故障 SLO |

当前 15 条检索集已经出现明显的“天花板效应”：dense、BM25、hybrid、hybrid+rerank 的 Recall@5 都为 1.0，MRR@10 都为 1.0，无法区分简单检索和复杂检索。3 条不可答 query 只要搜索返回任意 top-k 就被当前 retrieval harness 计为“未拒答”，因此这个字段只能描述“是否返回候选”，不能代替端到端拒答测评。

## 2. 数据基线修正结果

1. `sources.v1.2.json` 已成为当前清单；TechQA、EnterpriseOps-Gym ITSM 和 AgentDojo 已按固定 revision、形状和 SHA-256 恢复，并可由 `scripts/fetch_phase4_eval.py --verify-only` 复核。
2. Mendeley V3 已改为 `production_allowed: false`，360 个 case 已从 PostgreSQL 和 OpenSearch active alias 撤销，文件迁移至 `raw/reference`。当前生产只含 3 篇 internal runbook 和 36 篇 PagerDuty 文档。
3. TechQA 400 条公开代理评测已经执行，最终链 Recall@5=0.6536、Recall@10=0.7000、MRR@10=0.5541、NDCG@10=0.5896；四个预设高阈值门均失败。它被保留为跨域鲁棒性基线，不冒充租户人工金标。

恢复数据时只恢复有明确用途的集合：

- **域内主测**：GLPI KB、同租户已解决 Ticket/Followup/Solution、关联 Problem/Change/CI、内部 runbook、PagerDuty 最佳实践。
- **RAG 二级外测**：固定 revision 的 NVIDIA TechQA-RAG-Eval。
- **Agent 外测**：固定 revision 的 EnterpriseOps-Gym ITSM 子集，经 GLPI adapter 转换。
- **安全外测**：固定 revision 的 AgentDojo，再补 ServiceMind 自有 ACL、数据投毒和工具越权用例。
- **不进入 RAG 主分数**：UCI 498、Zenodo 分类/相似度、Mendeley 全量结构数据、IBM synthetic generator。

## 3. 最方便且权威的测评方案

### 3.1 两档 query 集

| 集合 | 数量 | 用途 | 运行频率 |
| --- | ---: | --- | --- |
| `servicemind-itsm-smoke-v1.2` | 120 | PR/CI 快速回归，及时发现指标和安全退化 | 每次合并 |
| `servicemind-itsm-release-v1.2` | 400 | Phase 4 关闭门、模型或检索策略发布定级 | 冻结候选版本后运行 |

400 条是工程推荐值，不是通用行业硬规定。对比例型指标，最保守条件下 95% 置信区间半宽约 5% 所需样本量为：

```text
n = 1.96² × 0.5 × 0.5 / 0.05² ≈ 385
```

因此取整为 400。排序指标仍需报告逐 query 的 bootstrap 置信区间，不能只看均值。120 条只用于快速发现回归，不能单独支撑“企业级”声明。

### 3.2 400 条发布集的构成

| 类别 | 数量 | 例子 |
| --- | ---: | --- |
| 单证据、日常可答 | 180 | SOP、状态解释、故障排查、知识问答 |
| 多证据、关系和时序 | 80 | Ticket+CI+Change、当前版与旧版 SOP、跨文档约束 |
| hard negative/近邻冲突 | 80 | 相似 Incident 但不同根因、旧版本、错误租户、低权威公开案例 |
| 不可答或应拒绝 | 60 | 语料不存在、未来预测、凭据/PII、无权限实体、要求绕过审批 |

上表仍是未来租户域内 release set 的目标结构。本次无日志/无专家条件下实际冻结的 TechQA 代理集为 280 条 answerable + 120 条 impossible；公开源不提供 Incident/Change/CI、租户 ACL 和中文比例等域内分层，因此不能声称满足上表的业务覆盖。

query 来源优先级：脱敏后的真实搜索/工单表达 > GLPI 已解决工单反向编写 > 域专家编写 > 模板扰动。语言比例应跟随生产日志；日志暂不可用时，首版采用中文 50%、英文 30%、中英混合及缩写 20%。每类都覆盖 Incident、Request、Problem、Change、Knowledge、CI 和权限/时效边界。

发布集先冻结 120 条 development 子集用于调参，其余 280 条作为隐藏 test。最终候选配置冻结后可一次性对全 400 条出具报告；之后不能再用隐藏 test 调参。每季度从 shadow 日志追加困难样本并发布新版本，旧版本继续保留用于回归。

### 3.3 按 TREC 方法建立 qrels，而不是让 LLM 自己出答案再自己打分

1. 对每条 query 运行 BM25、dense、hybrid RRF、hybrid+rerank、multi-query 和 GraphRAG（适用时）。
2. 合并各路 top-10，按 `parent_chunk_id` 去重，形成候选池；额外加入旧版本、低权威源和相似错误案例。
3. 域专家对候选 parent/chunk 做 0–4 级相关性判断：0 无关，1 背景相关，2 可支持部分答案，3 支持主要答案，4 完整且权威。
4. 为每条可答 query 记录必须覆盖的事实要点 `nuggets`，并绑定支持这些要点的 span、版本、authority、tenant/ACL 条件。
5. 一名域专家全量标注；第二名域专家复标分层抽样的 20% 和全部模糊项。报告 Cohen's kappa；低于 0.80 时修订标注指南并重标争议类别。
6. LLM 可以预提取候选 span 和提示遗漏，最终 qrels 由人工确认。生成模型、自动 judge 与人工抽检必须分离。

这套方法直接对应 NIST TREC 的 query + corpus + qrels 测试集合和 pooling 方法。TREC 2025 RAG 进一步把评测拆为相关性、答案要点覆盖、逐句引用支持和人工/自动判断一致性。其自动相关性判断最佳提交与人工判断的一致比例也只有 0.30–0.34，因此不能把全自动 LLM-as-judge 当作发布真值。

### 3.4 复用现有 OpenSearch，减少新工具建设

当前 OpenSearch 容器已经安装 `opensearch-search-relevance` 和 `opensearch-ubi`。直接使用 Search Relevance Workbench 管理 query set、search configuration、judgment list、质量实验和 hybrid 参数优化；现有 ServiceMind evaluation harness 继续作为最终权威执行器，因为它还要执行 tenant/ACL、parent expansion、PostgreSQL RLS 和 Reviewer 语义。

建议落盘结构：

```text
evaluation/gold/servicemind_itsm_v1_2/
├── manifest.json
├── queries.jsonl
├── qrels.tsv
├── nuggets.jsonl
├── annotation_guideline.md
└── splits.json

evaluation/reports/releases/<release-id>/
├── retrieval.json
├── generation.json
├── agents.json
├── security.json
└── summary.md
```

需要对现有 harness 做的最小扩展：

- 从 document-level binary `relevant` 升级到 `parent_chunk_id` 的 0–4 graded qrels。
- 增加 query 级指标明细、95% paired bootstrap、配置/模型/index generation 指纹。
- 增加 claim/sentence → citation span 支持率和 nugget completeness。
- 将 no-answer 改为完整 responder + Reviewer 的选择性回答指标；检索返回非空不能直接判错。
- 增加 Search Relevance Workbench judgment list 导入/导出，避免另造标注平台。

## 4. 发布时必须同时测四层

### 4.1 RAG 检索层

固定对比 BM25、dense、hybrid、hybrid+rerank；multi-query 和 GraphRAG 只在适用子集报告。

| 指标 | Phase 4 关闭门 |
| --- | ---: |
| Recall@5 | ≥ 0.85 |
| Recall@10 | ≥ 0.90 |
| MRR@10 | ≥ 0.75 |
| NDCG@10 | ≥ 0.80 |
| 错误 tenant/ACL/失效版本进入可见 evidence | 0 |

每项都报告总体值、各类别值和 95% CI。新增检索臂只有在困难子集 NDCG@10 绝对提升至少 2 个百分点，且 paired-bootstrap 95% CI 下界大于 0 时才保留；否则选择更简单、延迟更低的配置。

### 4.2 RAG 生成层

| 指标 | Phase 4 关闭门 |
| --- | ---: |
| Citation identity/ACL/版本有效性 | 100% |
| 逐句 citation support precision | ≥ 0.95 |
| 必要事实 nugget completeness | ≥ 0.90 |
| 不可答样本错误作答率 | ≤ 0.05 |
| 可答样本过度拒答率 | ≤ 0.05 |

同时报告人工结果与自动 judge 结果、两者一致性、延迟和 token 成本。安全和 ACL 属于零容忍确定性控制，不用平均 RAG 分数抵消失败。

### 4.3 子 Agent 与 Multi-Agent 层

公开 EnterpriseOps-Gym 用于外部可比性；ServiceMind 的发布结论以 GLPI adapter 后的最终状态 verifier 为准。沿用 ITSM 103 个任务作为第一版，每个任务从固定数据库快照重置，验证数据库最终状态，不给动作轨迹打主观分。

| 指标 | Phase 4/5 进入门 |
| --- | ---: |
| Router macro-F1 | ≥ 0.85 |
| 合法工具选择率 | ≥ 0.90 |
| 103 个 ITSM 任务 final-state success | ≥ 0.70 |
| Reviewer 未通过却到达 Action | 0 |
| 并行总预算越界 | 0 |
| checkpoint 重放产生重复副作用 | 0 |

### 4.4 安全与容量层

- 对 tenant × entity × group × profile × document state 生成至少 3,000 个确定性 ACL 矩阵用例。若 3,000 次零失败，按“rule of three”只能表述为 95% 置信下失败率上界约 0.1%，不能表述为绝对零风险。
- 建立至少 200 个提示注入、恶意文档、工具参数越权、secret/PII、审批绕过和资源耗尽攻击用例；生产安全控制出现一次绕过即阻断发布。
- 容量测试至少设 10 万、50 万、100 万 child 三档，报告 ingestion throughput、index size、p50/p95/p99、并发吞吐、错误率及 OpenSearch/TEI/Neo4j 单点故障下的降级结果。

## 5. 执行状态与未来域内认证路径

可自动完成的工程替代路径已经执行：

| 状态 | 交付 |
| --- | --- |
| 已完成 | 修订 manifest v1.2；恢复并校验 TechQA、EnterpriseOps-Gym、AgentDojo；生产索引排除 Mendeley |
| 已完成 | 固定 TechQA 400 条代理集；运行 BM25、dense、RRF、reranker 和 10,000 次 bootstrap CI |
| 已完成 | 复跑代码回归、RAG 活体 smoke、生产 PostgreSQL/OpenSearch 一致性核验 |
| Phase 7 | 有真实反馈时建立域内 query/qrels/nugget/span；若仍无专家，维持代理认证等级并明确限制 |
| Phase 7 | EnterpriseOps GLPI final-state adapter、AgentDojo/自有红队和 10万/50万/100万 child 容量测试 |

专家或真实日志将来可用时，仍按 §3 的 pooling、分层抽样、人工 qrels 和一致性流程执行。现阶段不等待不可获得的输入，也不把公开 silver labels 升格为人工 gold。

## 6. Phase 5 启动条件

Phase 5 的工程开发允许启动，依据如下：

1. manifest v1.2 与磁盘、许可、checksum 和用途一致，已满足。
2. 400 条 TechQA 代理集、模型 revision、样本 seed 和报告已冻结；它只作为二级外测。
3. Phase 4 架构与 Agent 合约回归通过；未完成的 final-state、安全、容量和域内人工证据转移到 Phase 7。
4. TechQA 高阈值失败作为显式质量例外保留；Phase 5 不得让同配置代理指标进一步退化。

工程关闭决定见 `docs/PHASE4_ENGINEERING_CLOSURE_V1_2.md`。该决定允许进入 Phase 5，但不签发企业前沿质量认证。

## 7. 权威依据

- NIST TREC 对 test collection、qrels 和 pooling 的说明：<https://trec.nist.gov/howto.html>
- NIST TREC relevance judgments：<https://trec.nist.gov/data/reljudge_eng.html>
- NIST TREC 2025 RAG Track，多层相关性、完整性、引用支持与一致性评测：<https://trec.nist.gov/pubs/trec34/papers/Overview_rag.pdf>
- OpenSearch Search Relevance Workbench：<https://docs.opensearch.org/latest/search-plugins/search-relevance/using-search-relevance-workbench/>
- NVIDIA TechQA-RAG-Eval：<https://huggingface.co/datasets/nvidia/TechQA-RAG-Eval>
- ServiceNow EnterpriseOps-Gym：<https://github.com/ServiceNow/EnterpriseOps-Gym>
