# ServiceMind RAG 质量提升执行方案 v2.0

> 冻结日期：2026-09-16  
> 适用范围：Phase 4 质量债的后续改进；不改变 Phase 5 工程验收结论  
> 当前状态：`DOMAIN_QUALITY_NOT_CERTIFIED / QUALITY_EXCEPTION_ACCEPTED`  
> 目标：让检索指标发生可复现的真实提升，而不是继续修正测量口径或重跑已失败候选

## 1. 决策

状态口径先冻结：§4.1 的六个租户发布门禁是 `NOT_EVALUATED`；TechQA 四个检索点估计
低于租户参考阈值，只能标记 `BELOW_TARGET_DIAGNOSTIC`；0.275 是检索 top-score 阈值代理，
不是端到端 Reviewer 作答率；8 文档/15 query gold 是 `SMOKE_ONLY_SATURATED`。机器可执行的
唯一状态源为 `scripts/audit_rag_quality_state.py --check` 与
`evaluation/reports/rag_quality_status_latest.{json,md}`。

现役 RAG 暂不改生产配置。下一轮只接受一个**实质不同的联合候选**：生产一致的
multi-query 候选生成、一个新的 late-interaction 召回信号，以及用公开技术支持数据的
保守 hard negative 做领域适配的重排器。三部分必须先分臂消融，再联合验证。

以下路径不再投入：

- 不继续扫描 `candidate_k`、`rerank_weight` 或 30/50/100 深度；现有实验已经证明
  `candidate_k=100`、`rerank_weight=0.85` 位于当前最优区，继续扫描只会在已分析数据上过拟合。
- 不重新包装或重新抽样验证 Qwen3 第三稠密臂。它的预注册留出集判定是
  `CONFIRMATORY_FAIL / RESEARCH_ARCHIVED`；单独重跑会形成重复抽样。
- 不把 512→8192、top-30→top-100 或报告口径对齐记为质量收益。这些都是测量一致性修正。
- 不用 Mendeley V3 训练或签发主分数；它继续是 `production_allowed=false` 的 reference 数据。

## 2. 已冻结的根因

当前 TechQA 外部 silver 生产形态基线为：

| 指标 | 当前点估计 |
| --- | ---: |
| Recall@5 | 0.6857 |
| Recall@10 | 0.7643 |
| Recall@20 | 0.8071 |
| MRR@10 | 0.5848 |
| NDCG@10 | 0.6279 |

Recall@10 可以拆成：

```text
0.7643 = 候选池覆盖率 0.8714 × 池内进入 Top-10 的条件精度约 0.877
```

因此只改一头不够：扩大池子会把更多难负例交给现役重排器；只换通用重排器又受 0.8714
的池覆盖率上限约束。第三稠密臂的留出集结果也验证了这一点：Recall@10、MRR、NDCG
改善主要落在第 6–10 位，Recall@5 没有提升。

另有一个尚未闭合的测量缺口：生产启用了 `SERVICEMIND_RAG_MULTI_QUERY=true`，而当前
TechQA 代理脚本只测单 query 的 BM25+dense。下一轮第一步必须先测清现役生产形态，不能
继续用缺少改写臂的结果代表完整生产路径。

## 3. 候选系统

### 3.1 C0：生产一致锚点

C0 必须走与生产相同的组件和公式：

1. 固定 QueryProcessor 提示、模型、revision、temperature 与 structured schema；
2. 对每条 query 保存经过脱敏的 rewrite cache，并将 cache SHA-256 写入报告；
3. 一路 BGE-M3 dense，加 normalized query 与至多 3 条 rewrite 的 BM25；
4. OpenSearch 同层 RRF，`rank_constant=60`，各臂深度 100，输出 100 个重排候选；
5. bge-reranker-v2-m3，模型窗口 8192；
6. `0.85 × rerank + 0.15 × minmax(RRF)`；
7. parent 去重、来源多样性、token budget、ACL 与版本过滤全部开启。

同时保留 C0-SQ（关闭 rewrite）作为消融。若 C0-MQ 相对 C0-SQ 在新的开发集上退化，先修
rewrite/fusion，不把 multi-query 既成事实当作收益。

### 3.2 R1：late-interaction 召回挑战者

新增一条 token 级 late-interaction 检索臂，只作为独立影子索引，不接现役 alias。理由是
现役 BM25 和单向量 dense 已经给出互补增益，但仍有 12.86% 可答 query 在 depth-100 两路池外；
late interaction 保留 token 级匹配，可提供与现有两路不同的候选集合。

R1 的准入顺序：

1. 先在 LoTTE technology `dev` 比较 BGE-M3、BM25、现役混合与 late interaction；
2. 只有池覆盖率@100 的配对提升达到 2 个百分点，且最终 Recall@5 不退化，才进入联合臂；
3. 独立记录索引体积、构建时间、GPU/CPU 内存与 p95 查询延迟；
4. 任何模型都必须固定 revision、许可证、文件哈希，并禁止 `trust_remote_code=True`；
5. 未达准入线即淘汰，不把它并入 RRF 继续调权重。

R1 是新的检索信号，不是已归档 Qwen3 第三臂的重试。

### 3.3 K1：领域适配的重排器

以当前 bge-reranker-v2-m3 checkpoint 为起点，训练数据只来自已经消费、因此只能用于
研发的 TechQA 集和 LoTTE technology `dev`。不得接触新的确认集。

训练样本按 query 建组：

- 正例：数据集明确给出的相关文档/answer passage；
- hard negative：C0 与 R1 top-100 中排名靠前、但未命中官方相关文档的候选；
- 近邻冲突：同产品、同错误码或标题相近但答案不同的候选；
- 禁止把所有“未标注文档”直接当强负例。TechQA 是单金标 silver，未标注不等于不相关；
  只把高置信的保守负例用于强监督，其余使用低权重或跳过。

训练目标必须直接覆盖 Top-5 排序，报告 pairwise/listwise loss、按 query 分组的数据切分、
checkpoint 哈希和全部超参数。模型选择只看开发集，不看确认集。

### 3.4 联合消融

开发阶段固定比较以下五臂：

| 臂 | 候选池 | 重排器 | 回答的问题 |
| --- | --- | --- | --- |
| C0-SQ | 单 query BM25 + BGE-M3 | 现役 | 历史锚点 |
| C0-MQ | 生产 multi-query + BGE-M3 | 现役 | 当前生产改写是否有净收益 |
| R1 | C0-MQ + late interaction | 现役 | 新信号是否提高池覆盖与 Top-5 |
| K1 | C0-MQ | 领域适配 | 排序改进能否独立成立 |
| **J1** | C0-MQ + late interaction | 领域适配 | 唯一允许进入确认阶段的联合候选 |

J1 必须先在开发集同时满足：Recall@5 提升至少 0.02、池覆盖率@100 提升至少 0.02，且
Recall@10/MRR@10/NDCG@10 任一项不下降超过 0.01。这个开发门只决定“是否值得消费确认集”，
不构成发布结论。

## 4. 无专家、无生产日志条件下的数据路径

TechQA 的 910 条 query 已全部参与过研发或确认，从现在起整个集合降级为训练/开发与历史
回归数据，不再承担任何新候选的确认判决。

下一轮采用两层公开外测：

1. **LoTTE technology**：使用官方 `dev` 做选择，官方 `test` 冻结为一次性确认集；search
   与 forum 两种 query 分开报告，并合并给出主分数。它有独立 collection、自然查询和
   `answer_pids`，可直接做有标签检索评测。
2. **BRIGHT Stack Overflow**：完整 117 query 只做未调参的推理密集型压力测试，报告
   nDCG@10 与 Recall@k。它全部属于 test，样本量也较小，因此不能单独签发采纳结论。

下载前先把 source URI、revision、license、文件哈希、query/corpus 数量写入
`data/phase4/manifests/sources.v1.3.json`；原始文件只进 `raw/eval`，不得进入租户生产索引。

这条路径能给出权威、可复现的**公开代理证据**，但仍不能替代中文 ITSM、租户 ACL、版本时效、
不可回答与真实 Reviewer 的域内验收。没有专家和生产日志时，最终声明上限仍是
`EXTERNAL_PROXY_IMPROVED`，不能写成“企业业务质量已认证”。

## 5. 预注册确认门禁

在读取 LoTTE technology `test` 的任何候选结果前，冻结协议、候选 checkpoint、rewrite
cache 生成规则、索引代、统计代码和 SHA-256。确认集只运行一次。

### 5.1 质量门禁

以 query 为配对单位，10,000 次 bootstrap，固定 seed；判定只有
`PASS / FAIL / INCONCLUSIVE` 三态。

1. 主端点：J1 − C0-MQ 的 Recall@5（LoTTE 对应 Success@5）95% CI 下界 > 0；
2. 非劣端点：Recall@10、MRR@10、NDCG@10 的单侧 95% 下界均 > −0.02；
3. search、forum、query 长度四分位、是否含代码/标识符均预先冻结分层；不得再用 C0 的
   失败结果事后定义“困难集”；
4. TechQA 历史基线容差维持 0.005，防止为了新数据而破坏既有技术支持分布；
5. tenant/ACL/失效版本泄漏保持 0。

LoTTE/BRIGHT 的指标不套用为租户域 gold set 定义的 0.85/0.90/0.75/0.80 绝对门槛；跨数据集
硬套阈值没有统计意义。原绝对门槛继续保留，只有将来取得租户域 release qrels 才裁定。

### 5.2 成本与运行门禁

- p95 检索延迟相对 C0-MQ 增幅 ≤ 20%；
- 单租户索引体积、重建耗时、峰值显存和并发吞吐全部报告并设机器可执行上限；
- rewrite 失败、late-interaction 服务失败或 K1 失败时回退 C0，且回退原因进入审计；
- J1 只读 shadow 使用独立索引代与 feature flag；确认通过前不得写现役 alias；
- 对完全相同 query 的重复运行必须得到相同候选集合和最终顺序，所有破并列键显式冻结。

### 5.3 生成与弃答边界

公开检索集没有生产 Reviewer 分布，也没有可信的 ITSM 不可回答标签。下一轮只裁定检索质量，
不把 pool oracle、top score 或人为跨语料负例冒充真实弃答率。生成层继续要求真实 Reviewer
输出、citation support 与 no-answer 标签；数据不可得时状态保持 `NO_DATA`。

## 6. 实施顺序与交付物

| 阶段 | 必须交付 | 退出条件 |
| --- | --- | --- |
| R0 生产口径闭合 | rewrite cache、C0-SQ/C0-MQ 报告、模型/提示/cache 指纹 | 当前 multi-query 的净影响被量化 |
| R1 数据冻结 | LoTTE/BRIGHT manifest、license、hash、dev/test 隔离测试 | test 无法被调参脚本读取 |
| R2 候选召回 | late-interaction 独立索引、池覆盖/成本报告 | 覆盖率准入门通过 |
| R3 重排训练 | hard-negative 构建、训练配置、checkpoint、开发报告 | K1 或 J1 达开发门 |
| R4 预注册 | 协议、代码、候选和数据清单 SHA-256 | 评测前冻结且 sidecar MATCH |
| R5 一次性确认 | 逐 query 排名、bootstrap、成本、安全、三态结论 | PASS 才允许只读 shadow |
| R6 部署准备 | feature flag、独立 alias、回退演练、运行手册 | 人工批准后才改生产流量 |

每一阶段都保存逐 query 排名、候选池、模型分数、配置与 SHA-256；报告从原始 artifact 自动生成，
不得人工改数字。旧 C0 与已归档第三臂证据保持只读。

## 7. 最终验收口径

本方案完成后的最好合法结论是：

```text
EXTERNAL_PROXY_IMPROVED
ENGINEERING_GATES_PASSED
PRODUCTION_SHADOW_ELIGIBLE
DOMAIN_QUALITY_NOT_CERTIFIED
```

只有 LoTTE 一次性确认通过、TechQA 历史回归不退化、成本与安全门全过，才可得到前三项。
租户域最终认证仍需自然产生的查询与 qrels、Reviewer 不可答结果或专家签署；这些证据不存在时，
第四项必须保留。

## 8. 外部基准来源

- [Stanford ColBERT：LoTTE 官方数据说明](https://github.com/stanford-futuredata/ColBERT/blob/main/LoTTE.md)
- [BRIGHT 官方数据说明](https://github.com/xlang-ai/BRIGHT/blob/main/Dataset_documentation.md)
- [BRIGHT 官方项目页](https://brightbenchmark.github.io/)
