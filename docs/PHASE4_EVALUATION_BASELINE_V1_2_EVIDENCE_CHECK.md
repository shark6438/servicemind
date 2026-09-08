# Phase 4 数据与评测基线 v1.2 最终证据核验

> 核验日期：2026-09-08
> 范围：数据治理、固定外部评测集、生产索引、代理检索评测和代码回归
> 结论：**方案中的可自动执行部分已经完成；Phase 4 可按“工程关闭、质量例外显式保留”进入 Phase 5。本文不替代 ITSM 专家签署，也不声称存在真实生产查询日志。**

## 1. 已落地事实

| 证据 | 最终状态 | 可复核依据 |
| --- | --- | --- |
| 数据清单 | 通过 | `data/phase4/manifests/sources.v1.2.json` 可解析，记录用途、许可、固定 revision、SHA-256 和关闭门角色 |
| Mendeley 治理 | 通过 | 360 篇公开历史案例已从 PostgreSQL 与 OpenSearch active alias 撤销；原始文件移至 `data/phase4/raw/reference/mendeley-helpdesk-v3`，`production_allowed=false` |
| 当前生产权威库 | 通过 | PostgreSQL：39 documents / 441 parents / 466 children；来源为 3 internal runbooks + 36 PagerDuty。OpenSearch active aliases：441 parents / 466 children，与 PostgreSQL 一致 |
| TechQA | 通过 | 固定 revision `0b5bbc84…`；910 rows（610 answerable / 300 impossible）、28,481 corpus files；README、train、corpus SHA-256 通过 |
| EnterpriseOps-Gym ITSM | 通过（数据恢复） | 固定 revision `c8e538ea…`；oracle、+5、+10、+15 四种模式各 103 条，四个 parquet SHA-256 通过 |
| AgentDojo | 通过（数据恢复） | 固定 revision `089ed468…`；archive SHA-256 `b1cbd209…`，安全解压后 36,860 files |
| 复现脚本 | 通过 | `python scripts/fetch_phase4_eval.py --verify-only` 全部通过 |
| 代码回归 | 通过 | `.venv/bin/pytest -q`：364 passed / 6 skipped；pytest 已限制发现范围为本仓库 `tests/`，不误收集外部 eval 源码 |
| RAG 活体 smoke | 通过 | `PYTHONPATH=src .venv/bin/python scripts/verify_phase4_rag.py`：真实 OpenSearch、零跨租户 parent 泄漏、3 条 evidence |

OpenSearch 当前是单节点部署。计数一致和容器健康只能证明本机链路可用，不能据此推导高可用或大规模容量。

## 2. 400 条公开代理评测

由于项目方明确无法取得真实生产查询日志和 ITSM 专家签署，本次采用固定公开数据作工程代理证据：从 TechQA 确定性选择 280 条可回答 query 和 120 条官方 impossible query，检索语料为完整 28,481 篇文档。标签层级明确写为 `external_silver_no_tenant_human_signoff`。

可回答 query 的检索结果如下；所有均值同时在 JSON 报告中保存 10,000 次 bootstrap 95% CI。

| 配置 | Recall@5 | Recall@10 | Recall@20 | MRR@10 | NDCG@10 |
| --- | ---: | ---: | ---: | ---: | ---: |
| BM25 | 0.5429 | 0.5821 | 0.6464 | 0.4527 | 0.4839 |
| BGE-M3 dense | 0.6607 | 0.6964 | 0.7429 | 0.5485 | 0.5848 |
| RRF hybrid | 0.6357 | 0.6929 | 0.7536 | 0.5369 | 0.5742 |
| RRF hybrid + BGE reranker | 0.6536 | 0.7000 | 0.7536 | 0.5541 | 0.5896 |

固定门禁为 Recall@5 ≥ 0.85、Recall@10 ≥ 0.90、MRR@10 ≥ 0.75、NDCG@10 ≥ 0.80，四项均未通过。报告状态因此保持 `failed`，没有降低阈值或删改样本。稠密排名采用本地固定 BGE-M3 ONNX 快照和 GPU 精确余弦，排除了 HNSW 近似召回造成主要差距的可能；重排使用固定 `bge-reranker-v2-m3` 快照。

该结果不能被解释为“Phase 4 RAG 已达到企业前沿质量”。同时，这些阈值原先是为租户域内人工 qrels 制定的发布目标，并非 TechQA 官方合格线。TechQA 是跨产品技术支持外测，其公开研究也显示该任务的文档召回具有较高难度。因此本报告把它作为外部鲁棒性基线和后续回归起点，不把跨域失败伪装成租户域内失败或成功。

机器报告：

- `evaluation/gold/phase4_proxy_release_v1.2.json`
- `evaluation/reports/phase4_proxy_release_latest.json`
- `evaluation/reports/phase4_proxy_release_latest.md`

## 3. 无法自动替代的证据

以下证据没有取得，也不会由 LLM 伪造：

- 来自真实租户分布的脱敏查询日志；
- ITSM 域专家对 parent/chunk 的 0–4 级 qrels、nugget 和 citation span 标注；
- 双专家复标、Cohen's kappa 和正式业务负责人签署。

因此保留两种不同结论：

1. **Phase 4 工程范围关闭**：架构、数据管道、检索链、GraphRAG、ACL、版本生命周期、引用身份校验、拒答路径、子 Agent 与编排控制均已实现并有自动化或活体证据，允许开始 Phase 5。
2. **企业前沿质量认证未签发**：域内相关性、语义引用完整性、生产分布性能和大规模容量仍缺权威证据。它们进入 Phase 7 的 Evaluation/Red Team/CI Gate；将来能取得日志或专家时再替换代理集，不反向阻塞 Phase 5 的工程开发。

## 4. 对旧核验结论的修订

本文件取代此前“eval/reference 缺失、Mendeley 尚在生产、399/1,247/2,705”的状态描述。旧数值是治理修正前的历史快照，不再代表当前生产状态。

最终 Phase 4 决策见 `docs/PHASE4_ENGINEERING_CLOSURE_V1_2.md`。复现公开数据与代理评测分别使用：

```bash
python scripts/fetch_phase4_eval.py --verify-only

LIBS=$(find /data/shihongye/data/miniconda3/lib/python3.13/site-packages/nvidia \
  -type d -name lib | paste -sd:)
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
LD_LIBRARY_PATH="$LIBS:${LD_LIBRARY_PATH:-}" \
python scripts/evaluate_phase4_proxy_release.py --device cuda:0
```
