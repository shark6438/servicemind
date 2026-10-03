# 生产 query arm 与漏斗深度实测（TechQA release set）

**状态**：`QUERY_ARM_MEASUREMENT`（不构成质量认证）
**生成时间**：2026-10-02T05:36:48.601721+00:00
**索引**：`sm-techqa-arms-v1`，28481 篇文档，ingest Nones
**改写模型**：`deepseek-v4-flash`
**改写缓存**：`evaluation/gold/techqa_rewrites.v1.json` sha256 `314cdb58d5f2b05b…`（0 条未产出改写）

## 为什么需要这份报告

已提交的每一个 TechQA 数字都是在 `use_query_model=False` 下测出来的，且全部关闭
`use_rewrites`。生产不是这样跑的：`agents/knowledge.py` 用
`use_query_model=True`、`use_rewrites=SERVICEMIND_RAG_MULTI_QUERY`（该配置固定为
`True`，注释写明“改写阶段始终运行”）。测量口径与部署口径不一致，而此前没有任何
数字说明这个差异把检索推向哪一边。

## 忠实度锚点

| 项 | 值 |
|---|---|
| 已提交 `hybrid_rerank` R@10 | 0.7214285714285714 |
| 本次 `c0_off` 复现 R@10 | 0.7286 |
| 差值 | 0.0072 |
| 已提交 packed max | 16 |
| 本次 `c0_off` packed max | 16 |

c0_off replays the published run's query arm on this run's index. A delta of 0.0000 means the two builds agree and the published recall is this run's baseline. A non-zero delta is a difference between two *index builds*, not between two arms: every arm in this table shares one index and one capture. What the control repeats is measured, not assumed -- see 控制臂自身的可复现度 below -- and a delta at or below the movement reported there is not a difference this table can resolve.

## 控制臂自身的可复现度

同一条控制臂（`c0_off`）在同一索引、同一捕获、同一主体上跑第二遍；两列之间唯一的
差别就是「又跑了一遍」。这张表的每一行都是**相对它**读的，所以它能移动多少，就是
这张表能分辨的最小差。

| 项 | 第一遍 | 第二遍 |
|---|---|---|
| R@5 | 66.43% | 66.79% |
| R@10 | 72.86% | 72.86% |
| R@20 | 72.86% | 72.86% |
| MRR@10 | 0.5797 | 0.5798 |
| packed max | 16 | 16 |

| 逐 query 对比 | 条数 |
|---|---|
| 参与比较 | 280 |
| R@5 变化 | 1 |
| R@10 变化 | 0 |
| 排名（MRR@10）变化 | 1 |
| 只出现在其中一遍 | 0 |

The same arm, the same index, the same capture and the same principal; the only difference between the two columns is the pass. A delta at or below what moved here is not a difference this table can resolve, and where the cutoff columns moved while the rank column did not, what this stack does not repeat is the order of near-tied documents rather than which documents it retrieved.


## 各 arm 实测

| arm | query 处理 | 改写 fan-out | R@5 | R@10 | R@20 | MRR@10 | packed max | 耗时 |
|---|---|---|---|---|---|---|---|---|
| `c0_off` | deterministic | off | 66.43% | 72.86% | 72.86% | 0.5797 | 16 | 311s |
| `c0_sq` | model | off | 66.07% | 68.93% | 68.93% | 0.5748 | 16 | 288s |
| `c0_mq` | model | on | 65.71% | 67.50% | 67.50% | 0.5650 | 16 | 328s |
| `c0_sq_ck200` | model | off | 65.36% | 68.57% | 68.57% | 0.5707 | 18 | 486s |
| `c0_sq_ck400` | model | off | 65.71% | 68.21% | 68.21% | 0.5711 | 18 | 877s |
| `c0_mq_ck200` | model | on | 65.71% | 68.93% | 68.93% | 0.5696 | 16 | 534s |
| `c0_anchor` | model | on | 66.79% | 68.93% | 68.93% | 0.5600 | 16 | 374s |

## 相对 `c0_off` 与相对生产 arm 的位移（R@10）

| arm | R@10 | 对 `c0_off` | 对 `c0_mq` |
|---|---|---|---|
| `c0_sq` | 68.93% | -3.93pp | +1.43pp |
| `c0_mq` | 67.50% | -5.36pp | +0.00pp |
| `c0_sq_ck200` | 68.57% | -4.29pp | +1.07pp |
| `c0_sq_ck400` | 68.21% | -4.65pp | +0.71pp |
| `c0_mq_ck200` | 68.93% | -3.93pp | +1.43pp |
| `c0_anchor` | 68.93% | -3.93pp | +1.43pp |

## 截止点坍缩

包内文档数不足以区分这些截止点，它们报告的同一个数字是包长而非检索能力：

- `c0_off`：10==20
- `c0_sq`：10==20
- `c0_mq`：10==20
- `c0_sq_ck200`：10==20
- `c0_sq_ck400`：10==20
- `c0_mq_ck200`：10==20
- `c0_anchor`：10==20

## 局限

- the labels are source-provided silver, not tenant-domain human qrels, so no row here is a release threshold
- each answerable query carries a single relevant filename, so a rank-2 hit scores the same as a miss under Recall@k
- the harness packs to final_k = max(top_ks) + 4 = 24 while production's first round packs to 8; every packed count here is the harness's, and the rows whose cutoffs collapse are reporting the pack length rather than the retriever
- parents are read from the index's parent alias rather than under PostgreSQL row-level security, so this run exercises the expansion call, its tenant scoping and its ACL filter, but not the RLS policy itself
- the rewrite capture is one sample of a non-deterministic stage: a second capture would differ in wording. The vendored sha256 is what makes this particular sample reproducible, not the stage
- the control arm's own reproducibility was measured in this run (see 控制臂自身的可复现度): a delta at or below the movement reported there is not a difference this table resolves
- the index was not rebuilt in this run. It was reused under `sm-techqa-arms-v1-tenant-11111111-1111-4111-8111-111111111111-*` because re-ingesting 28,481 documents costs ~53 minutes and the corpus digest has not moved. What the report records is the active generation and its parent/child counts, read back from the cluster; that proves the generation is reachable and populated, not that it holds *this* corpus. The run's own check is the fidelity anchor -- if `c0_off` did not reproduce the published `hybrid_rerank` recall, none of the rows beside it may be read
