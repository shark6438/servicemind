# 改写阶段的输入差：裸问题 vs 部署信封

**状态**：`ENVELOPE_QUERY_ARM_MEASUREMENT`（不构成质量认证）
**生成时间**：2026-10-02T05:55:43.474269+00:00
**索引**：`sm-techqa-arms-v1`（**不重建**，沿用 query arm 那轮留下的代际）
**代际**：`a5daf639d749`（199409 children / 46691 parents，为空则拒绝运行）

## 这一轮补的是哪一段

已提交的数字与部署管道之间有三个差：`use_query_model`、`use_rewrites`，以及**改写阶段的输入**。
前两个由 `evaluation/reports/phase4_query_arm_funnel_latest.json` 量化。第三个是这一份：

```python
src/servicemind/orchestration/supervisor_workflow.py:1000
    kwargs["model_query"] = json.dumps(context_envelope.model_payload(), ...)
```

`QueryProcessor.process` 送给模型的是 `model_query or query`，所以只要平台装配了上下文信封，
模型拿到的就是**一个 JSON 数组**，用户的提问只是其中 `items[0].content` 里的 `goal` 字段。
`SERVICEMIND_CONTEXT_ENABLED` 默认 `False`，但部署把它设成 `true (deploy/glpi/.env(.example))`，
所以部署管道走的是这一支。

本探针**用平台自己的代码**复现该信封（`Phase5Governance.build_fast_knowledge_context` +
真实 `ContextBuilder` + `NullContextArtifactSink`），不手搓形状。

## 信封输入把模型的输出推开了多少

比的是**模型自己的字段**（`model_normalized_query` + `rewritten_queries`），也就是词法臂实际拿去搜的文本。
密集锚点两种输入下都是用户原话，信封推不动它；若这一列是 0，说明输入根本没送到模型。

| 项 | 信封 vs 裸提问 | **噪声对照**（同输入重跑） |
|---|---|---|
| 比对条数 | 400 | 400 |
| 模型未作答（回退，已剔除） | 0 | 0 |
| 模型输出改变 | **275（68.75%）** | 78（19.50%） |
| 其中仅改写列表改变 | 121 | 114 |
| 完全不变 | 4 | 208 |

改写阶段是采样的，所以左列必须先超过右列才说明问题；右列是**完全相同的输入**重跑一遍。

the model is asked to rewrite a JSON envelope instead of the question. This counts how often that changes the model's output -- the normalization and the paraphrases the lexical arms search with -- not whether the answer is better. The dense anchor is the user's own question under both inputs, so no envelope can move it, and a sensitivity of zero here would mean the input never reached the model at all.

| 提问 | 裸问题改写 | 信封改写 |
|---|---|---|
| Is there a way set a timeout for shell task?

I  | Is there a way to set a timeout for a shell task in UrbanCod | Is there a way to set a timeout for a shell task in UrbanCod |
| I am getting a random invalid handle exception f | Random invalid handle exception in MQ .NET client applicatio | Random invalid handle exception in MQ .NET client applicatio |
| After applying cumulative fix, why does xmlacces | After applying a cumulative fix to WebSphere Portal 8.5, why | After applying a cumulative fix to WebSphere Portal 8.5, why |

## 各 arm 实测

| arm | 改写输入 | fan-out | R@5 | R@10 | R@20 | 对 `c0_off` | packed max |
|---|---|---|---|---|---|---|---|
| `c0_env` | envelope | off | 65.36% | 68.57% | 68.57% | -0.0429 | 16 |
| `c0_mq_env` | envelope | on | 65.36% | 67.50% | 67.50% | -0.0536 | 17 |
| `c0_mq` | plain | on | 65.71% | 67.50% | 67.50% | -0.0536 | 16 |

## 局限

- the envelope reproduced here is the *fast knowledge* path's; the knowledge-task path assembles memory and ticket items the release set does not have, so it is not reproducible off-line
- the ticket id in the task item is a constant: the release set has no GLPI tickets
- the labels are source-provided silver, not tenant-domain human qrels
- the rewrite capture is one sample of a non-deterministic stage; the vendored sha256 is what makes this sample reproducible, not the stage
- the index is the kept one from the query-arm run, so these rows share its generation by construction; an unreachable or empty generation is refused rather than measured, but a *stale* one -- a kept index whose generation no longer matches the query-arm report's -- is not detected here
- parent expansion reads the index's parent alias rather than PostgreSQL under row-level security, so this run exercises the expansion call, its tenant scoping and its ACL filter, but not the RLS policy itself
