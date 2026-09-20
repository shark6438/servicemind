# Phase 4 第三检索臂实验（2026-09-15）

> 本目录是**变更审计归档**，不是发布路径。发布报告在 `evaluation/reports/`。
> 实验结论与判据见 [`docs/PHASE4_RAG_QUALITY_ROOT_CAUSE_2026-09-15.md`](../../../docs/PHASE4_RAG_QUALITY_ROOT_CAUSE_2026-09-15.md)
> §3.3 与 [`docs/PHASE4_EVALUATION_BASELINE_V1_2.md`](../../../docs/PHASE4_EVALUATION_BASELINE_V1_2.md) §4.1。

## 状态

第三检索臂（Qwen3-Embedding-0.6B 稠密臂，按生产等权 RRF 口径融合）：

**`CONFIRMATORY_FAIL` / `PRODUCTION_NOT_APPROVED` / `SHADOW_NOT_AUTHORIZED` /
`RESEARCH_ARCHIVED`**（2026-09-16 项目方裁定）

> `RESEARCH_ACCEPTED` **已不适用**：预注册协议规定只有收益门禁与非劣门禁**同时**通过
> 才可进入该状态，而留出集上收益门禁未通过。

- **开发集**（280 可答，已反复分析）：本轮三条候选路径里唯一在 Recall@5 上显著为正的改动
  （全体 +0.0286、CI95 `[+0.0071, +0.0536]`；两路池覆盖率 0.8714 → 0.9143）。
- **留出集**（330 可答 + 180 不可能，一次性消费）：按预注册协议判定 **`FAIL`**。
  门禁一 1a 全体 Recall@5 **+0.0000**（LCB −0.0091）、1b 困难集 `rescue_rate@5`
  **+0.0000**（**0/71** 挽救）；门禁二三项**全部通过且 LCB > 0**（R@10 +0.0152 /
  MRR +0.0135 / NDCG +0.0136），**该改善主要发生在第 6–10 位**。
  **开发集的困难集 +5.67pt 没有复现。**
- **不批准上生产、也不启动 shadow**：收益门禁未通过，协议 §5.5 的 shadow 准入前提不成立。
  现役两路配置保持不变。
- **失败的两条独立理由**：① 门禁一 1b 是**结构性零基线的 `rescue_rate@5` 端点**
  （困难集 = C0 top-10 未命中 ⇒ C0 的 R@5 恒为 0），本轮**实际挽救数为 0**；
  在 n = 71 上需 **≥ 4 次**挽救其 LCB 才 > 0（1/2/3 次的 LCB 均为 0），
  故它在任何合理统计方法下都给不出收益证据——**不是**被刀刃判据误伤。
  ② 门禁一 1a 独立于该端点，**独立地不通过**。
- **本候选的确认性验证已于 2026-09-16 正式关闭**：不新建协议版本、也不采集新数据重试
  （避免可选停止与重复抽样）。§9.5 的「不得就地修正」仍然成立——
  **冻结协议与其 sha256 保持不变**，勘误只追加在其外部。重启条件见留出集归档。
- 产物：`evaluation/reports/phase4_holdout_prereg_2026-09-15.{json,md}`；
  执行脚本 `scripts/evaluate_phase4_holdout.py`。

> **留出集确认已单独归档**：预注册协议的一次性运行及其证据链见
> [`../phase4_holdout_prereg_2026-09-15/`](../phase4_holdout_prereg_2026-09-15/)，
> 判定为 `FAIL`。本目录只承载**开发集**的探索性分析。

## 为什么归档在这里

这些脚本与产物原先留在 `/tmp/exp_layer1`（临时目录），不满足企业变更审计与可复现要求。
现按以下规则入库（`manifests/artifacts.sha256.json` 登记每个文件的 sha256）：

| 类别 | 处理 | 理由 |
| --- | --- | --- |
| `scripts/` | 原样入库（13 个） | 判定逻辑必须可读、可复算 |
| `reports/` | 原样入库（13 个，均 ≤100KB） | 结论直接引用的指标与配对自助结果 |
| `matrices/` | 原样入库（9 个，每个 ≤200KB） | 分数矩阵：审计者无需 GPU 即可复算每一项指标 |
| `pools/` | **gzip 入库**（10 个） | 池定义是所有矩阵的索引，缺它矩阵不可解读；gzip 后约 1MB |
| `qwen3_passages.npy` | **仅登记 sha256**，不入库 | 385 MB float32 嵌入矩阵，可由固定 revision 重新生成 |

**这些脚本按「运行时原样」冻结，不做格式化。** 它们是当时实际执行的字节，`pyproject.toml`
因此把 `evaluation/experiments` 从 ruff 排除（它们也不在 `[tool.pyrefly] project-includes`
里）。理由是可审计性优先：若为了过 lint 而改写，清单里的 sha256 就会指向一份**事后被改过的
文件**，归档就不再证明「这些数字出自这些代码」。后续分析代码请放 `scripts/` 或 `src/`。

## 复现

前置：GPU（重排/嵌入推理）、pinned 模型快照（`data/phase4/models/`）、
TechQA-RAG-Eval 固定 revision（见 `TECHQA_REVISION`）。
本目录脚本的输入输出目录默认是 `/tmp/exp_layer1`，先做一次引导：

```bash
mkdir -p /tmp/exp_layer1
cp pools/*.json.gz /tmp/exp_layer1/ && gunzip -f /tmp/exp_layer1/*.gz
cp matrices/*.npy /tmp/exp_layer1/
cp reports/*.json /tmp/exp_layer1/
cp scripts/*.py /tmp/exp_layer1/
# 385MB 嵌入矩阵需按 run_layer2a.py 中的固定 revision 重新生成（不入库）
```

随后按依赖顺序执行；**每个脚本都会先断言两路锚点**
（纯重排 R@10 = 0.7357 / NDCG@10 = 0.6036，融合 R@10 = 0.7643 / NDCG@10 = 0.6279），
断言不过即中止——这样任何一次在错位分数矩阵上得出的 A/B 都不会被误读为结论。

| 顺序 | 脚本 | 回答的问题 |
| ---: | --- | --- |
| 1 | `fix_blend100.py` | 修正层一融合归一化口径（曾用重排分做基准，结论已作废） |
| 2 | `run_layer2a.py` | 第三稠密臂的池覆盖率 |
| 3 | `run_layer2c.py` | 嵌套三路池的端到端指标（**构造已作废**，生产不这样融合） |
| 4 | `run_layer2d.py` | **真实等权三路融合**——§3.3 结论的唯一依据 |
| 5 | `run_layer2b.py` / `reranker_ab.py` | 重排器 A/B：gte-modernbert、mxbai 两个挑战者 |
| 6 | `reconcile_v14.py` | 用框架自身扁平缓存重建逐 doc 分数，验证发布报告五项指标可复现 |
| 7 | `compare_paths_at_production_window.py` | 同池同窗口下进程内 vs TEI 打分一致性（§2.7） |
| 8 | `diagnose_scoring_paths.py` / `probe_truncation.py` | 512 窗口错配的诊断与受控探测（§2.6） |
| 9 | `measure_passage_lengths.py` | 子块长度的字符 / token / **拼接对**三口径实测（§2.6 的事实更正） |
| 10 | `prereg_power_analysis.py` | 第三臂预注册的**逐指标功效 + 门禁二联合功效**（§5.4）。只在**已分析集合**上运行，是规划计算，**不触碰留出集** |
| — | `scripts/evaluate_phase4_holdout.py` | **不在本目录**（属发布路径脚本）：预注册协议 §8 的一次性留出集裁定。冻结后只跑一次，判定 `FAIL` |

## 口径边界（引用本目录数字前必读）

1. 本目录的分数矩阵来自 **TEI `/rerank` 服务**（窗口 8192）。§2.7 已证明它与框架在
   **生产窗口下的进程内打分**等价：生产形态臂十个数字（五项指标 + 五个 CI 边界）逐位相同。
   即便如此，**引用绝对值仍以 `evaluation/reports/` 的框架产物为准**。
2. `layer2_grid.json` 是首轮嵌套构造的产物，其 `pure_rerank` 与 `BLEND` 两项数值相同
   （融合退化为纯重排），**不作为任何结论的依据**。
3. 困难子集（141/280）由「现役系统 top-10 未命中」**事后定义**，且与本目录的全体指标
   出自同一批 400 条 query——它是**目标子集**指标，不是独立证据。
4. 覆盖率 0.9143 是**候选池的 oracle 覆盖率**：生产 Reviewer 只看到最终 evidence，
   因此它只说明弃答门禁存在理论空间，**不能证明真实弃答率能达到 0.90**。
5. **子块长度有三个不同口径，引用时必须写明是哪一个**（`reports/passage_lengths.json`）：
   字符 p50 1313 / p95 1738；**token**（BGE 与重排器一致）p50 417 / max 427，**0/96,293 超 512**；
   而重排器真正吃的是 **query+子块拼接对**，p50 480 / p95 639，**27.5%（824/3,000）超 512**。
   根因文档初稿把第一个数当成了第二个数，据此写「512 砍掉每段 61%」，该表述已更正（§2.6）。
6. `pools/` 里有两组**解压后哈希相同**的文件，这是 A/B 设计使然、**不是拷贝事故**：
   `gte_modernbert_three_way` ↔ `rrf3_top100`、`gte_modernbert_two_way` ↔ `mxbai_rerank_docs`。
   两组同属「同一候选池、换重排器打分」的对照，池必须逐字相同才有可比性。
   全部 49 个入库文件的 sha256 可用 `manifests/artifacts.sha256.json` 一次性校验
   （池按**解压后**字节哈希，其余按文件字节哈希；第 50 条 `qwen3_passages.npy` 仅登记哈希、
   不入库）。
