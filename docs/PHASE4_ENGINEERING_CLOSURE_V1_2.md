# ServiceMind Phase 4 工程关闭决定 v1.2

> 决定日期：2026-09-08
> 依据范围：`docs/企业IT服务管理(ITSM)智能体平台.md` 的 Phase 4 技术范围
> 状态：**ENGINEERING CLOSED WITH QUALITY EXCEPTION（工程关闭，质量例外保留）**
> Phase 5：**允许启动**
> 企业前沿质量认证：**未签发**

## 1. 决定

Phase 4 的工程建设范围已经完成，可以进入 Phase 5。关闭范围包括 Enterprise Hybrid RAG、结构化摄入、Dense/BM25/RRF/重排、父子块、召回前 ACL、版本发布/撤销、引用身份校验、GraphRAG 旁路，以及 Knowledge Agent 与 Supervisor 的接线。

项目当前无法取得真实生产查询日志和 ITSM 专家签署。该约束不阻塞后续 Memory、Context、Skills 和 Model Gateway 的工程开发，但也不能被解释为“已达到企业前沿质量”。域内检索质量、语义引用支持、容量和完整安全红队转移到 Phase 7 的正式发布门。

## 2. Phase 4 范围验收

| 范围 | 判定 | 证据与边界 |
| --- | --- | --- |
| 文档管道 | 通过 | GLPI/Markdown/PDF/DOCX 路由、解析、结构切块、父子块、内容上限、来源校验、摄入 job 均已实现；Docling 活体报告已落盘 |
| Hybrid RAG | 通过（能力） | Dense、BM25、RRF、Cross-Encoder、parent expansion、dedup、citation packaging 均有实现与测试 |
| Tenant/ACL | 通过 | ACL 在 OpenSearch 召回前过滤，PostgreSQL RLS 作父块权威层；活体跨租户探测为 0 |
| 生命周期 | 通过 | generation、active alias、publish、suspend、deactivate、unpublish、reconcile 均有回归；360 篇 Mendeley 已实际撤销 |
| GraphRAG | 通过（受控旁路） | Neo4j 投影、tenant pin、图路径检索和 runbook/change 路径已接通；默认是否启用仍由配置和适用 query 控制 |
| Citation/拒答 | 有条件通过 | citation identity、来源、tenant、版本 fail-closed；3 条不可答 + 5 条可答的 live 语义 smoke 通过。没有人工 claim→span 全量标注，不能宣称语义 citation completeness 已获企业认证 |
| 数据治理 | 通过 | Mendeley 仅作 reference；生产现为 39 documents / 441 parents / 466 children，PostgreSQL 与 OpenSearch active alias 一致 |
| 可复现外测 | 已建立，质量门失败 | TechQA 400 条代理集、固定 revision/checksum/model snapshot 和 bootstrap CI 已落盘；四个高阈值门禁均未通过，作为后续质量债保留 |

## 3. 子 Agent 职责核验

| Agent | 应承担职责 | 已验证控制 | 决定 |
| --- | --- | --- | --- |
| Data Agent | 读取 GLPI 事实和关系，生成有来源的 Evidence | 行数/内容上限、预算退化、0 模型预算、最小只读计划、tenant context | 可承担 Phase 5 输入职责 |
| Knowledge Agent | 只读检索知识，不持有 GLPI 写工具 | ACL 透传、Hybrid RAG、GraphRAG、引用封装、无 active index 降级 | 可承担只读知识职责 |
| Analysis Agent | 基于 Evidence 诊断并生成建议 | 证据约束、修订失败显式降级、预算控制 | 可承担分析职责 |
| Reviewer Agent | 确定性门 + 语义门，决定通过/重检索/重规划/升级 | citation fail-closed、低置信升级、replan 上限、写动作一致性 | 可承担发布前审查职责 |
| Action Agent | 只生成受约束的 ActionIntent，副作用交给既有 Harness | Reviewer→Action 门、HITL、幂等与回读复用 Phase 2 控制 | 可承担受控动作职责 |

本结论证明代码职责边界和失败路径可执行。EnterpriseOps-Gym 的 103 个 ITSM 任务目前只完成固定数据恢复，尚未建立 ServiceNow→GLPI adapter 和 final-state verifier；因此没有写入虚构的 103-task 成功率。

## 4. Multi-Agent 核验

Supervisor 已具备受验证的 DAG、Data/Knowledge 并行 Evidence Plane、provenance-aware join、Reviewer 门、bounded replan、预算/循环保护、checkpoint/handoff/cancel 路径。此次审计修复了并行预算重复占用、首轮评审前 replan 崩溃、replanner 双失败裸抛、无 ready Action 的错误 handoff 等缺陷，并配套回归。

最终离线回归：

```text
364 passed, 6 skipped, 0 failed
```

该结果支持“Multi-Agent 能按当前合约承担职责”。它不支持生产并发 SLO、混沌恢复成功率或跨进程百万级负载声明；这些是 Phase 6/7 的部署与评测范围。

## 5. 质量例外

固定 TechQA 代理集结果：

| 最终链 | Recall@5 | Recall@10 | Recall@20 | MRR@10 | NDCG@10 |
| --- | ---: | ---: | ---: | ---: | ---: |
| RRF hybrid + BGE reranker | 0.6536 | 0.7000 | 0.7536 | 0.5541 | 0.5896 |

它未达到 v1.2 为租户域内 release qrels 预设的 0.85 / 0.90 / 0.75 / 0.80 门禁。结果保持失败，不降低阈值。NVIDIA 将 TechQA-RAG-Eval定位为技术支持 RAG 外测；原始 TechQA 研究也将它描述为真实用户问题和真实规模的困难域适配任务。公开研究报告的 TechQA 文档检索 R@5 约为 0.56–0.73，说明 0.85/0.90 不是该公开集的官方通用合格线。

因此采用以下处理：

- 把 TechQA 分数冻结为外部鲁棒性基线，不替代租户域内 qrels；
- 不签发“企业前沿质量认证”；
- Phase 5 的每个组件必须继续跑同一代理回归，禁止较 Phase 4 基线退化；
- 正式租户发布前，在 Phase 7 用可取得的 shadow 反馈、用户纠错和人工抽检建立域内集。若届时仍无法取得日志或专家，只能继续维持代理认证等级。

依据：

- [NVIDIA TechQA-RAG-Eval dataset card](https://huggingface.co/datasets/nvidia/TechQA-RAG-Eval)
- [TechQA 原始论文](https://aclanthology.org/2020.acl-main.117/)
- [Technical Question Answering across Tasks and Domains](https://aclanthology.org/2021.naacl-industry.23/)

## 6. Phase 5 进入门

| 条件 | 状态 |
| --- | --- |
| Phase 4 主要架构和合约完整 | 通过 |
| 全量离线回归无失败 | 通过 |
| 生产数据用途与索引一致 | 通过 |
| 固定代理基线和失败项可复现 | 通过 |
| 未获得的人工/日志证据被显式披露 | 通过 |

结论：**Phase 5 可以开始。** Phase 5 的实施顺序和关闭门见 `docs/PHASE5_IMPLEMENTATION_PLAN.md`。
