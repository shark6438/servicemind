# ServiceMind 最终交付报告（2026-10-03）

> **这份报告回答什么。** 按用户要求「把完整流程——具体用什么、干了什么、结果怎么样——详细写在报告文档中」，
> 本文按**时间顺序**记录本轮从「RAG 效果不好」这个症状出发，到评测结论可交付为止的**每一步**：
> 用了哪个脚本、跑了什么命令、产出了哪份文件、数字是多少、以及**哪一步失败了**。
>
> **它与另外两份文档的分工（不重复写）：**
>
> | 文档 | 承担什么 |
> |---|---|
> | 本文 | **端到端的完整流程与最终结论**（读这一份就能知道做了什么、结果如何） |
> | `docs/EVALUATION_18_COVERAGE_AUDIT_2026-10-02.md` | **18 项评测分类的逐项判定**与未关闭缺陷 D1–D5 的完整证据 |
> | `docs/PHASE7_REMAINING_EVALUATION_2026-10-02.md` | 七项「剩余评估项」的执行记录（轨迹 / 协同 / 可靠性 / 语义裁判 / 三项未执行） |
> | `docs/PHASE7_ACCEPTANCE_BASELINE.md` | Phase 7 验收基线与 28 个验收案例（P7.6 业务闭环），**不在本轮改动范围** |

---

## 怎么读这份报告（术语纪律）

沿用 `docs/PHASE7_ACCEPTANCE_BASELINE.md` 的定义，本文所有标记只有四种含义：

| 标记 | 含义 | 不是什么 |
|---|---|---|
| **已执行** | 有可复现的观测产物，产物里记着产生它的命令与输入摘要 | — |
| **未评估** | 尚无观测数据 | **不等于「已确认无缺陷」，也不等于「有缺陷」** |
| **已知未关闭缺陷** | 已确认存在、尚未修复，含复现路径 | 不是「未评估」 |
| **结构性做不了** | 受环境 / 人力约束，当前不存在执行路径 | 不是「还没做」 |

**每一个数字都必须指着一份产物。** 凡是本文写了数字的地方，正文里都给出产物文件与字段；
产物与本文不一致时，**以产物为准**。本文**不产生任何新观测**——所有观测都产生于它所引用的批次。

---

## 一、一句话结论

**评测体系本身曾经不可信，这是本轮最大的发现。** 本轮从一个症状（RAG 效果不好）出发，
先量化了这个症状，然后发现**测量它的仪器有三处会静默给出好看结论的缺陷**，把仪器修好、
在各 live 批次**分别绑定单一被测版本**后，得到以下可交付结论；不同批次不宣称来自同一个 source revision：

| 项 | 结论 |
|---|---|
| **RAG 检索质量** | ⚠️ **未认证，不发布 Recall/MRR/NDCG 性能结论**。现有 benchmark 同时受 gold-label 缺陷、代理语料失配及 metric/cutoff 口径问题影响；36 条池外金标诊断中仍有 **4 条真实检索漏失**。部署臂丢弃用户原话的缺陷已修复，候选召回臂（R2）未通过预设准入判据。 |
| **Memory 模块** | ✅ **工程面达到设计门禁，业务面未取样**。14 条硬门禁全 0、泄漏率 0.0、写侧 48 步精确率 1.0；但**生产观测 `NO_DATA`**，因此**不能声称企业前沿级**——能声称的是「契约与治理面通过，真实分布未测」。 |
| **评测可信度** | ✅ **已修复**。三处仪器缺陷（进程-树不一致、变异框架 mtime、裁判契约字段缺口）+ 语义裁判契约缺口，全部**从根源修掉并加锁定测试**，语义层误纳 **0.714 → 0.0**。 |
| **响应质量（冻结案例集）** | ✅ Reviewer 决策口径下，可答案例 **119/120 = 0.9917**（门槛 0.85），三个负向整层 40/40、20/20、20/20 全过，gate **PASS**（退出码 0）；这不是答案事实正确率或生产业务成功率。 |
| **安全与故障** | ✅ **72 场景全 PASS**，252 条证据全部通过、87 条变异证据全部 `RED (good)`，42 场景有变异背书，gate **PASS**（退出码 0）。 |
| **负载** | ❌ **gate FAIL（退出码 1）**，但**根因不是并发**：三档的 6 次未通过**全部是同一条 `Q-002`**，**并发 1 的基线档自己就不过**；延迟无退化（p95 比值 1.00 / 1.04 / 1.06）。 |
| **可靠性** | ✅ **结果稳定、引用不稳定**。并发 1 下 8 例 × 5 次：状态与决定 **100% 稳定**，方差**全在 `citations` 轴**（`flake_rate 0.875`）。 |

**最重要的两句话**（它们比上面任何一行都重要）：

1. **本轮的多数失败不是「平台坏了」，而是「测量平台的东西坏了」。** 见 §三·B。

2. **剩下的失败是小样本上的、被单独定位到一条的。** `Q-002` 一条同时出现在质量批、负载批、可靠性批，
   是**同一条确定性缺陷**，不是三处独立问题。

---

## 二、环境与版本绑定（这些结论跑在什么上面）

### 2.1 运行栈

| 组件 | 形态 | 本轮的角色 |
|---|---|---|
| `servicemind-api` | systemd **--user** unit，`127.0.0.1:18080` | 所有 live 批次的被测对象；`GET /health` 实测 `{"status":"ok"}` |
| OpenSearch | 本地容器 | 知识索引与检索 |
| PostgreSQL | 本地 | `agent_runs` / `run_events` / `model_invocations` / 记忆表 |
| Neo4j | 本地 | GraphRAG |
| TEI 8085 / 8086 | 本地容器（embedding / reranker） | 检索与重排；**本轮未重启、未重建** |
| DeepSeek | 外部模型 | 平台推理；**本轮未打印、未记录任何密钥值** |

### 2.2 被测版本（每个批次各自的绑定）

每个 live 批次的每一条观测都带 `deployed_revision`，且**批次内必须同质**（跨版本即退 3 拒绝）。
实测（按 `evaluation/*/replays/` 逐文件统计）：

| 批次 | 观测文件数 | `deployed_revision` | 同质？ |
|---|---:|---|---|
| 业务质量 | 201（200 例 + `_batch.json`） | `b385df7c…+patch(bb9010dda000)` | ✅ 单一 |
| 安全与故障 | 72 | `b385df7c…+patch(96133a9537b6)` | ✅ 单一 |
| 负载 | 121（120 运行 + `_batch.json`） | `b385df7c…+patch(96133a9537b6)` | ✅ 单一 |
| 可靠性（设计测量） | 41（40 运行 + `_batch.json`） | `b385df7c…+patch(96133a9537b6)` | ✅ 单一 |
| 验收（全量 28 例，本轮新增） | 28 | `b385df7c…+patch(fbdbefc59bbc)` | ✅ 单一，**且当前** |
| 验收（只读 20 例，中间版本） | 20 | `b385df7c…+patch(47de4c33ae95)` | ✅ 单一，**已被取代（不删）** |
| 验收（历史标准 28 例） | 28 | `cf08ac8a…+dirty(26 files)`，`recorded_at 2026-09-30T15:11:03Z` | ✅ 单一，**但不当前** |

> `head_sha` 相同、`patch(...)` 不同，是因为**每个批次冻结在各自的源码指纹上**：
> 批次之间隔着重跑与修复。**同一批次内绝不跨界**，这是 `src/servicemind/evaluation/revisions.py`
> 的同质性门禁在守的那条线。
>
> **验收三行是同一问的三个答案，不是三次尝试。** 历史标准 28 例记的是**旧格式** `+dirty(26 files)`
> ——`§三·B1` 修掉的那个口径；它**可归属**（28/28 同一个修订）但**不当前**。后来那版只读 20 例是新格式、
> **经 `--expect-revision` 正向断言过**，但只覆盖 28 例里的 20 例。**本轮按裁定补跑了写入的 8 例**，
> 28 例在单一部署上整批跑完：**28 PASS / 0 FAIL / 0 BLOCKED、107/107 断言、`--expect-revision` 通过、
> gate 退 0**。验收的版本绑定因此从「20/28 当前 + 8/28 停在 `cf08ac8a…`」变成 **28/28 当前**；
> 下面那段「退 1 与 8 条 `NO EXECUTION RECORDED`」描述的是**中间那版只读语料**，不再是当前状态
> （该语料不删、标记为被取代，取代理由是覆盖面与批次级证据，不是结果有误）。
>
> **补跑这批第一次用上了 §2.3 那道批次身份闸的豁免分支。** 两个案例（ACC-10a、ACC-22）会按剧本把
> 核验器指向死端口并**重启 unit**，于是批次中途 unit 共重启 **4 次**（两个案例各两次）、批次身份因此变了 **2 次**。
> 新闸逐例核对身份、两次打出 `REBASELINE`
> 并继续——而在它只有严格版时，这批会在 ACC-10b 处中止。两处的**源码树那一半都没动**，变的是进程，
> 这正是该闸要区分「谁动的」的原因。

### 2.3 本节最硬的一条：被测进程必须与源码树一致

**这是本轮所有 `REFUSED`（退出码 3）的根因。** 见 §三·B1——一句话版本：
**一个比工作树旧的进程会称职地回答每一个请求**，于是批次跑完、每条记录都带着一份平台
从未运行过的代码的修订号，而**没有任何东西会报错**。

**本轮没有动的东西**（用户硬约束：「组里的卡不是我私有的」）：不装 / 不升 / 不卸任何包；
不改 `.venv` 依赖锁；不改共享 miniconda 环境；不重启 / 不停止 / 不重建任何在跑的容器
（含用户自己的 `servicemind-frontend` 与 TEI 8085/8086）；不 kill 用户的 qBittorrent；
不 `source .env`（bash 会剥掉 JSON 值里的双引号，使 `policy.py:77` 在每次受治理的模型调用上抛
`JSONDecodeError`）；所有文件写入走 bash；不擅自 commit / push。

---

## 三、完整流程

按时间顺序分四段：**A 诊断 RAG → B 修仪器 → C 五批实测 → D 文档交付**。
每个小节的格式固定为 **用什么 / 干了什么 / 结果怎么样**。

---

## A. RAG 主线：从「效果不好」到「知道差在哪、并修掉能修的那一条」

### A0. 先把症状拆成两半（漏斗与标签诊断）

**用什么**：`scripts/audit_rag_quality_state.py`、`scripts/diagnose_phase4_packing_ceiling.py`、
`scripts/diagnose_phase4_label_diagnostic.py`。

**干了什么**：把「RAG 效果不好」分解成三个可分别测量的量——**候选池里有没有金标**、
**装进上下文的是不是整段**、**标签本身对不对**。

**结果怎么样**（`evaluation/reports/rag_quality_status_latest.json` 与三份诊断报告）：

| 诊断 | 关键数字 | 读数 |
|---|---|---|
| 候选池（depth-100，即生产 `SERVICEMIND_RAG_CANDIDATE_K`） | 含金标 **244/280 = 0.8714**；top-5 含金标 **0.6857** | 天花板是 **0.8714**，不是 1.0——**36 条（12.86%）的金标根本不在池内** |
| 装包上限（packing ceiling） | 每臂最多装 **19 篇**，中位父文档 1107 token，再放一篇的余量 **p50 −13033 token** | `Recall@10 == Recall@20` 是**装包造成的假象**，不是排序深度不够 |
| 标签质量 | 一个落地页 `swg21592093.txt` 是 **8 条互不相关问题**的金标（其中 6 条在池外）；**0 个标注员、`kappa = None`** | 标签是 **external silver**，**已自证损坏** |

**这一步的结论（写进 `release_blockers`）**：**首要约束是标签，不是检索器。**
这条结论直接决定了后面两步的走向——**不要把力气花在调检索上**。

---

### A1. R0：生产真正在跑的那条查询臂（本轮最实质的一条修复）

**用什么**：`scripts/measure_phase4_production_query_arms.py`（七臂 × 两样本，TechQA）。

**干了什么**：把**生产真正在跑**的查询臂在真实语料上量化。过程中查出一条**根因缺陷**：

> **部署臂在搜索之前把用户原话丢了。**
> `QueryProcessor.process` 在模型改写成功时返回
> `KnowledgeQuery(normalized_query=proposal.normalized_query, …)`——用户原话只留在 `raw_query` 里，
> 而**全仓 grep：`raw_query` 没有任何检索读取者**。检索读的是 `normalized_query` 与
> `rewritten_queries`。所以在部署配置下，「模型改写成功」与「用户原话被丢弃」**是同一件事**。

**修复**（落 `src/servicemind/rag/query.py`、`domain/knowledge.py`、`rag/opensearch.py` 三处）。
不变量一句话：**模型只能加一条臂，不能换掉那条臂**——`model_normalized_query` 把模型的归一化
**存在提问旁边**而不是顶替它，锚点恒为用户原话。

**结果怎么样**（R@10，280 条可答，两个独立样本）：

| 臂 | 是什么 | 样本 A | 样本 B |
|---|---|---:|---:|
| `c0_off` | 确定性处理、无 fan-out | **72.86%** | **72.86%** |
| `c0_mq` | **部署臂**（修复前） | 67.86% | 67.50% |
| `c0_anchor` | **修复后的形态** | **70.00%** | **68.93%** |

- **修复前，部署臂比确定性锚点少 14–15 条**（两样本方向一致）——让模型改写检索文本，
  在这份语料上是**净负面**。

- **修复把其中 4–6 条还回来**（A +6、B +4），两样本都为正，**但都尚未恢复到 `c0_off`**。

- 因此这一项的定性是：**一处方向确定的、可测量的改进**，加上**一条被证伪的常见做法**；
  **不是「RAG 效果修好了」**。

- **噪声下界由控制臂自己量出来**：同一进程重跑控制臂，280 条查询里 **R@10 移动 0 条**、R@5 移动 1 条。
  所以**小于一条的差不是发现**，这条下界被后续所有读数沿用。

固定该结论的资产：`evaluation/reports/phase4_query_arm_funnel_latest.json`、
`docs/PHASE7_ACCEPTANCE_BASELINE.md` §5.9。

---

### A2. 漏斗深度这条杠杆已用尽（measured, closed）

**用什么**：同上实验的 `candidate_k` 扫描。

**干了什么**：把候选池 100 → 200 → 400，看召回是否上升。

**结果怎么样**：

| 臂 | depth | Recall@10 | p95 延迟 |
|---|---:|---:|---:|
| `c0_sq` | 100 | 0.6893 | 927.9 ms |
| `c0_sq_ck200` | 200 | 0.6857 | 1482.1 ms |
| `c0_sq_ck400` | 400 | 0.6821 | 2772.1 ms |

**候选池翻四倍，Recall@10 不动（0.6893 → 0.6821），延迟翻三倍。** 差异落在
「同进程重跑 R@10 移动 0 条」这条噪声下界之内。**加宽漏斗是已测量并关闭的选项，不是待调参数。**

---

### A3. R1 → R2：候选召回臂按它自己的准入判据淘汰

**背景（这是唯一动过 GPU 的一步）。** 唯一的硬件动作是一次**有界**的尺寸 / 延迟 spike，
跑在**独立本地 GPU 进程**上（miniconda 解释器，`torch 2.5.1+cu121`，RTX 3090，
`torch.cuda.set_per_process_memory_fraction` 显式封顶），**仓内只做客户端**，
`.venv` 依赖锁与共享环境**一字未动**。冻结方案把这一步定为 R2。它的准入判据是**方案自己写的**：

> 「只有池覆盖率@100 的配对提升达到 **2 个百分点**，且最终 **Recall@5 不退化**，才进入联合臂。」

**用什么**：`scripts/build_late_interaction_index.py --dry-run`（R2.0 spike，**在全量构建之前**）。

**干了什么**：在真实语料的 1,276,222 篇文档上抽样 5,000 篇，测 token 直方图、编码吞吐与
MaxSim 扫描成本，然后**外推**到全量，与预先冻结的成本门（`c0_mq` p95 = **1020.7 ms** 的 +20%
= **204 ms**）比较。

**结果怎么样**（`evaluation/reports/phase4_r2_decision_latest.json`）：

| 量 | 实测 / 外推 | 门 |
|---|---:|---:|
| 外推 token 数 | 138,219,181 | — |
| 外推 fp16 字节 | **283,072,884,233 B ≈ 283 GB** | — |
| 外推构建时长 | **10.42 小时** | — |
| 外推单查询扫描 | **2,358,305 ms ≈ 39 分钟** | **≤ 204 ms** |
| 物理下界（完美 GPU kernel：PCIe 一遍 + 计算） | 13.3 s | 0.204 s |
| 比值 | **超出预算 65.2 倍** | — |

**判定：`RETIRE the late-interaction arm in its online corpus-scan form`。**

**为什么"只做 rerank"救不了它**（这一步的推理值得记下，因为它解释了这个方案为什么在数学上不成立）：
覆盖率@100 只有在**给现有各臂 top-100 之外的金标打分**时才会上升，而找到那种文档的唯一办法
就是**对全语料打分**。「用 MaxSim 重排现有候选」是 rerank——它重排一个**成员固定**的池，
**按构造不可能提高覆盖率@100**。也就是说：**这条臂自己的准入判据，要求的正是那个被 spike 否掉的
全语料扫描。** 这是一次**按方案自己的止损条款执行**的淘汰：**淘汰这条臂，不把它并入 RRF 去调权重。**

**它顺带留下的两条产物**：① 「漏斗宽度」这条控制实验（A2）已经在盘上，不需要新跑；
② 对 36 条池外金标的逐条人工复核，进一步支持 A0 的「首要约束是标签」结论。

---

### A4. RAG 侧的收束：为什么本轮到此为止（用户裁定）

**不是「修不动了」，是「再往下修就是改金标」。** 剩下 36 条池外金标里，
**一个落地页被标成 8 条互不相关问题的金标**这类问题**不是检索器能修的**。
按用户 2026-10-03 的裁定：**如实记录，不改冻结案例集，不再做更多 RAG 工作**
（改案例集会改 `cases_digest`，200 例需整批重跑）。

**§四** 的 `Q-002` 就是这条线上的一个具体例子。

---

## B. 修仪器：三处会「静默给出好看结论」的缺陷

> **为什么单独成段。** 本轮的多数 `REFUSED`（退出码 3）与一次语义层误判，**根因都不在被测平台里，
> 在测量它的工具里**。这类缺陷的共同特征是**不报错**：它们产出一份看起来正常的报告，
> 而报告描述的东西从未发生过。**这是本轮最值得留下的经验。**

### B1. 缺陷一：被测进程与源码树不一致（所有 `REFUSED` 的根因）

**缺陷。** 每个 live 批次把**从源码树算出来的** `source_revision` 写进观测记录——
而**没有任何一步验证过正在响应请求的那个进程跑的就是这棵树**。

**实测（本轮开跑前）**：

| 量 | 值 |
|---|---|
| `servicemind-api.service` MainPID | `2095085` |
| 该进程启动于 | `2026-10-01 00:08:15 CST` |
| 工作树最新源文件 mtime | `2026-10-02` |
| **不一致量** | **145,881.6 秒 ≈ 40.5 小时** |

**为什么一直没被发现**：`revision_problems()` 只在传 `--expect-revision` 时才问「是不是当前」，
不传时**只检查同质性**——所以「批次内都是同一个修订」永远成立，而「这个修订是不是正被服务的那个」
**从没被问过**。更说明问题的是：**六个 live 批次各自实现了一遍新鲜度检查，只有一个真的做了**
（这就是为什么同一轮里 acceptance 是 PASS 而 security / quality / load 全部 REFUSED——
不是三个批次各自出错，是**五个批次缺同一道闸**）。

**修法（单一实现 + 接线 + 锁定测试）。**

- 新增 `src/servicemind/evaluation/deployment.py` 作为**唯一实现**：
  `stale_deployment()` 比较进程启动时间（`/proc/<pid>/stat` + `CLOCK_BOOTTIME`）与
  最新源文件 mtime；**拒绝是默认**，放行必须显式传 `--allow-stale-deployment`。

- 六个 live 批次全部改为在**观察任何东西之前**调用 `refuse_stale_deployment(...)`。

- `tests/servicemind/test_phase7_deployment_freshness.py` 对**六个批次逐一**断言源码里存在该调用
  ——**将来新增批次若漏接这道闸，测试变红**，而不是等下一次审计再发现。

- **处置**：先量化重启风险（acme 316 条探针残留 pending、globex 1 条可恢复），
  记录原 PID 与启动时间，重启两个 **user unit**（不碰容器）：
  API `2095085 → 1623492`（`2026-10-02 16:46:29 CST`），outbox `937420 → 1624495`。
  重启后 `GET /health` 200，`run.recovered` 无增长。**当前 `stale_deployment()` 返回 `None`（新鲜）。**

---

### B2. 缺陷二：变异框架还原时不还原 mtime

**它怎么被发现的。** 安全批重跑之前，闸突然报「部署过期」：最新源文件是
`src/servicemind/agents/supervisor.py`，mtime 比进程启动**晚 3339 秒**——
但 `git diff` 与 `git status` 都显示它**与 HEAD 无差异**。**一个内容没变的文件，凭什么被判成「新写的」？**

**根因（不在闸里，在变异框架里）。** 安全批每次运行跑 6 个变异实验：临时改一处源码 → 跑测试 → 还原。
而 `scripts/mutation_harness.py` 的还原**只写回内容，不写回 mtime**。于是一个**内容与行为都完全正确
的部署**被判成过期，受影响批次一律退 3。**所以这不是「变异没还原干净」——`git status` 干净本身就是证据。
是「还原」的定义错了：撤掉一个写操作，必须连它留下的时间戳一起撤。**

**为什么值得单独记一条。** 这类缺陷的危害不是让某个批次失败，而是**它会训练人去绕过闸**：
碰到「明明没改却报过期」，最省事的做法就是加 `--allow-stale-deployment`——而那道闸保护的恰是
B1 那种**不会自我暴露**的失败。**一个经常误报的闸，等于没有闸。**

**修法**：抽出模块级 `snapshot(path) -> (text, (atime_ns, mtime_ns))` 与 `restore(path, text, stamp)`；
运行时四条写盘路径全部改走它（快照、逐例还原、`atexit`/信号处理器整体还原、崩溃恢复）；
**`SIGKILL` 路径单独处理**——原快照已不存在，因此 journal 多记 `"stamp"` 字段。
**锁定测试 2 条**：一条行为测试（新鲜 → 变异 → 报过期 → 还原 → 回到 `None`），
一条读源码钉住崩溃路径的 journal 字段与 `os.utime`（那段代码在闭包里，不读源码测不到）。

**验证（修后整批重跑）**：见 §C2。

---

### B3. 缺陷三：语义裁判的 claim 索引契约缺口（D4）

**症状。** 语义层（Reviewer Semantic）**误纳率 0.714**：5/5 的 evasion 类缺陷被放行。

**根因（`docs/PHASE7_REMAINING_EVALUATION_2026-10-02.md` §4.3）。** 裁判的判据是**按 claim 索引**的，
而**缺陷面比 claim 宽**——不落在 claim 上的缺陷，裁判结构性地看不见。

**修法**：契约加宽 3 个判据（`action_target_grounded` / `citation_integrity_ok` / `unbacked_assertions`）

- 判据收紧（豁免须同段写明）+ `_citation_finding` 根因修复；`REVIEW_POLICY_VERSION` **v5 → v6**。

**结果怎么样**：**误纳 0.714 → 0.0，误拒保持 0.0**（18 次模型调用，契约填充率 1.0）。
裁定逻辑的**零调用重放探针**作为常驻锁定测试留在仓库。

**这条支线必须一起记下**：**探针自己在那段时间里出过一次静默缺陷**——它按加宽前的 8 个字段
重建判决，把已拦下的案例重放成 `PASSED`。**零调用探针同样是代码，同样会静默地给出好看的结论**，
所以它也需要锁定测试。修前的渲染**原样冻结**为
`evaluation/reports/phase7_reviewer_semantic_replay_prefix_2026-10-02.{json,md}`，不覆盖、不删除。

---

## C. 五批实测（修好仪器之后，在单一版本上重跑）

### C1. 业务质量：200 例端到端

**用什么**：`scripts/verify_phase7_quality_live.py` → `scripts/gate_phase7_quality.py`。
**干了什么**：200 条案例（120 可答 + 40 证据不足 + 20 版本冲突 + 20 必须拒绝访问）
在**单一被测版本**上跑完；可答率对照**运行之前就登记好**的门槛 0.85。

**结果怎么样**（`evaluation/reports/phase7_quality_latest.{json,md}`）：

| 类别 | 条数 | PASS | FAIL | 判定方式 |
|---|---:|---:|---:|---|
| answerable | 120 | **119** | 1 | 速率，对照预登记门槛 0.85 |
| insufficient-evidence | 40 | 40 | 0 | 绝对：不得有证据不支持的断言或凭空提出的动作 |
| version-conflict | 20 | 20 | 0 | 绝对：必须不引用已废止版本 |
| must-refuse-access | 20 | 20 | 0 | 绝对：必须不出现受组限制的文档 |

- 端到端 Reviewer 可答率 **119/120 = 0.9917**，95% Wilson 区间 **[0.9543, 0.9985]**。

- 它**取代**的是 `0.275`——那是**检索 top-score 阈值代理**，里面没有模型、没有复核器、没有答案
  （`answerability_signal.is_end_to_end_reviewer_measurement = false`）。
  两者**不是同一个测量**，代理值也**不是**本值的下界。

- **gate `verdict: PASS`，退出码 0**（本报告写作时重跑复核过）。

- 唯一未通过的是 `Q-002`，见 §四。

**这份报告自己声明了它不证明什么**（原文在 `phase7_quality_latest.md`）：语料是自建 44 篇而非租户真实知识库；
40 条「证据不足」由**本清单作者**判定，**无第二方复核**；该类别**无法独立判定是否作答**，
只能读平台自己发布的编造信号；**只测读取路径**（全部 `request_write=false`）；
只在组轴上做了隔离，不覆盖实体轴与角色轴。

---

### C2. 安全与故障：72 场景

**用什么**：`scripts/verify_phase7_security.py` → `scripts/gate_phase7_security.py`。
**干了什么**：72 个场景跨 15 个类别跑完，每个场景的证据分三类：普通测试、**变异实验**、验收案例。
**结果怎么样**（`evaluation/reports/phase7_security_latest.{json,md}` + `evaluation/security/replays/` 逐条统计）：

| 读数 | 值 |
|---|---:|
| 场景 | **72**（PASS 72 / FAIL 0 / BLOCKED 0） |
| 证据条目 | **252，全部 `passed`** |
| ↳ 测试类 | 153 |
| ↳ **变异类** | **87，全部 `RED (good)`** |
| ↳ 验收案例类 | 12 |
| **有变异背书的场景（`teeth`）** | **42 / 72** |
| 被测版本 | `b385df7c…+patch(96133a9537b6)`，**单一** |
| gate | **`verdict: PASS`，退出码 0** |

**87 条变异证据分布在 6 个脚本上**：`mutate_phase7_permissions.py` 43、
`mutate_rate_limit_degradation.py` 13、`mutate_glpi_write_gateway.py` 12、
`mutate_outbox_retention.py` 12、`mutate_revision_crash.py` 4、`mutate_supervisor_feedback.py` 3。

**报告自己把两种强度分列，不合并**：42 个场景**有变异背书**（删掉这段行为测试会变红），
其余 30 个**只由通过的测试背书**。「测试是绿的」与「删掉这段行为测试会变红」不是同一个强度的结论。

**报告同样自己列出了 8 项 `not_covered`**，其中最关键的一条逐字是：
> 「没有任何一条场景向平台投喂过词表之外的新注入变体」

即：**只验证了已列入词表的标记被处理，从未有人真正攻击过这套系统**。其余缺口：
服务重启后的撤权、`CredentialCipher` 的 `InvalidToken` 路径与轮换后旧密文、直接投递其他 realm 的
有效令牌、`memory_review` 端点的角色与租户约束、供应链与镜像漏洞（由 trivy 承担）、拒绝服务与容量、
已写审计行的防篡改检测。

---

### C3. 负载：三档一次测完 → gate FAIL，但根因不是并发

**用什么**：`scripts/verify_phase7_load_live.py` → `scripts/gate_phase7_load.py`，
计划冻结在 `evaluation/load/plan.v1.json`（tier-1 并发 1 × 1 遍、tier-5 并发 5 × 2 遍、
tier-10 并发 10 × 3 遍，共 **120 次运行**，单次结算预算 240 s）。
**干了什么**：在**单一部署**上把三档**一次跑完**，批次**独占主机、不与任何其它批次并发**。
**结果怎么样**（`evaluation/reports/phase7_load_latest.{json,md}`）：

| 档位 | 并发 | 声明 | 观测 | 通过 | 未通过 | p50 | p95 | p95/基线 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| tier-1 | 1 | 20 | 20 | 19 | **1** | 24.3 s | 33.7 s | 1.00× |
| tier-5 | 5 | 40 | 40 | 38 | **2** | 19.7 s | 35.0 s | 1.04× |
| tier-10 | 10 | 60 | 60 | 57 | **3** | 19.9 s | 35.7 s | 1.06× |

- 120 条观测全部落盘，**驱动层错误 0**，未观测 0，`deployed_revision` **单一**。

- **gate `verdict: FAIL`，退出码 1**（本报告写作时重跑复核过）。

- **FAIL 的根因是单独一条，不是并发**：三档的 **6 次未通过全部是 `Q-002`**
  （tier-1 的 1 次记为 **blocker**，其余 5 次记为 finding），文案逐字
  *"the run succeeded without citing ['KB-Q-VPN-CONN']"*。

- 关键是**并发 1 的基线档自己就不过**，于是 gate 按第 3 条判据写下 blocker：
  *"the unloaded tier is not a usable baseline (1 run(s) did not behave), so nothing observed under
  load can be attributed to load"*。**这句话是对的，而且正是本批次存在的意义。**

- **延迟本身就是反证**：p95 相对基线 1.00 → 1.04 → 1.06，**三档之间没有可辨退化**。

- **没有为让它变绿去放宽判据，也没有改冻结案例集。**

**这一步同时解除了历史遗留的一条判断。** 旧报告曾记 tier-10 在并发 10 下「60 中失败 41」。
今日推演：那 41 条的证据**在磁盘上已不存在**（重放被后续重跑覆盖），失败原因全部是**提供方侧**
（限流 / HTTP 状态错误 / 越过重试后仍失败），**没有一条平台内部错误**；而全表
`MODEL_RUNTIMEERROR` 出现 **0 次**——即**熔断器从未因误判打开**。改判为
**「本地 10 路并发下触发的提供方侧降级」**，而非平台缺陷。**本轮实测进一步支持这一点**：
在冻结版本上三档跑完，未通过**不是**提供方侧（驱动层错误 0），而是 `Q-002` 一条。

**仍保留的残余风险（不是 bug，是设计约束）**：`SERVICEMIND_MODEL_MAX_RETRIES = 1`
（限流下总尝试 2 次），而网关**已经提供了**长时钟挂钩 `throttle_wait_seconds()`
（`gateway.py:226`，base 1.0 s / ceiling 8.0 s，尊重运行截止时间），却**只有一条路径**在用
（`agents/analysis.py:244`）。**本轮没机会验证它，因为本轮没触发限流。**

---

### C4. 可靠性：两个来源，一个结论

**这一项有两个来源，性质不同，分开写。**

#### (a) 设计测量：8 例 × 5 次 @ 并发 1

**用什么**：`scripts/verify_phase7_reliability_live.py`（10-02 记录，产物
`evaluation/reliability/replays/_batch.json` 与 40 个 `Q-*__rep*.json`）。
**干了什么**：按四类各取 2 例（可答 `Q-001`/`Q-002`、版本冲突 `Q-121`/`Q-122`、
必须拒绝 `Q-141`/`Q-142`、证据不足 `Q-161`/`Q-162`），在**并发 1** 下各重复 **5 次**，
每次单独落盘——所以这里测到的任何方差**都是平台自己的，不含争用**。

**结果怎么样**：

| 读数 | 值 |
|---|---:|
| 例数 / 运行数 | 8 / 40 |
| `terminal_status` 有分歧的例 | **0** |
| `reviewer_decision` 有分歧的例 | **0** |
| `citations` 有分歧的例 | **7 / 8**（`flake_rate = 0.875`） |
| 40 次运行里 `errors` 非空的 | **0** |

**逐条复核 40 个重放（不只读汇总）**：8 例全部 `succeeded` / `passed`；
分歧幅度小而确定——`Q-001` 5 次里 4 次引 6 篇、1 次换掉其中 1 篇；`Q-142` 出现 4 种不同引用集合。
**结论：结果稳定，引用集合不稳定。**

#### (b) 负载批幸存重复给出的下限

**用什么**：`scripts/report_phase7_reliability_recorded.py`，**纯文件分析**，
读 `evaluation/load/replays/`（120 条观测），**不连数据库、零模型调用**。

| 层 | 并发 | 观测 | 每例重复 | 全轴一致率 |
|---|---:|---:|---:|---:|
| tier-1 | 1 | 20 | 1 | —（无重复） |
| tier-5 | 5 | 40 | 2 | **50%**（10/20） |
| tier-10 | 10 | 60 | 3 | **30%**（6/20） |

- **40 个有重复的用例里 24 个不一致（60%）**，且**每一个分歧都只落在 `citations` 一个轴上**；
  `terminal_status`、`reviewer_decision`、`plan_digest` **从未变化**。

- 三档现在**同处一个修订**（10-02 重跑之后），所以档间差异**不再被修订变化混淆**——
  但仍不能只归因于并发：**重复次数与并发是绑在一起的**。

**两个来源差得很远，而那正是它们的用途。** (a) 在并发 1 下测出 **87.5%** 的例会漂；
(b) 的 tier-1 在 20 例上测出 **0 分歧**——因为**它每例只跑 1 次，没有重复就测不出方差**。
**这一对照本身就是一条告警**：(b) 里任何一行的「一致」都**不能**当作「稳定」读，
只有 (a) 回答得了「并发 1 稳不稳」。

**本轮修掉的一处自相矛盾（工具层）。** (b) 报告的 limitations 原是**写死的常量**；
重跑之后其中两条（「语料横跨两个修订」、「旧的那次不稳定不在语料里」）**与自己上方的表格互相打架**。
已改成**由语料推出**——凡关于语料的陈述都从 `revisions(observations)` 与 `floor` 算出来，
只有与语料无关的三条仍是常量。**一条与表格矛盾的 limitation 比没有更坏。**
配套加锁定测试 `tests/servicemind/test_phase7_reliability_limitations.py`（5 条），
并用**三个变异**验证它有牙：把单修订分支改成永不执行 → 红；把 floor 写死成 60.0% → 红；
把「不在语料里」那句加回来 → 红。

**同一类缺陷在协同报告里又抓到一次。** 它的最后一条 limitation 写死「**121 observations**」——
那是 10-03 重渲染**之前**的语料，而它自己上方的表格在同一页数着 **200**。
这次改成 `limitations(rows)` 由 `len(rows)` 推出，配锁定测试
`tests/servicemind/test_phase7_coordination_limitations.py`（3 条：1/121/200/2000 四种规模下
末条必须报出**本语料**的行数、且不与其它规模混淆；四条与语料无关的常量里不得再出现 "observations"），
两个变异（把 200 写死回 121 → 红；只改数字但不带 caveat → 红）均 `RED (good)`。

**为什么记在交付报告里**：这两份报告都是**机器生成**的，而机器生成的报告最容易犯的错不是算错，
是**把只有人写才会写死的那句话写死**。语料每次重录都会变，写死的那句就从「描述」变成「撒谎」。

---

### C5. 轨迹 / 协同 / 路由（零模型调用）

| 项 | 用什么 | 结果 |
|---|---|---|
| 轨迹打分 | `scripts/report_phase7_trajectory.py` | **200 运行** / **0 异常 / 0 契约失败** / 计划一致性 100%；轨迹长度 24 / 36.96 / 63；模型调用 3316 / 工具调用 889 |
| 多智能体协同 | `scripts/report_phase7_coordination_recorded.py` | **200 运行** / 0 异常；再入 **66.0%**；**争用 0.0%**；策略拒绝 2.0%；supervisor 置信度 p50 0.90 / 最低 0.70；引用来源少于交付数量的运行 20.5% |
| 路由判别力 | `scripts/evaluate_phase7_routing_hard.py` | 契约集 107/107 饱和；**强化集 36/41 = 0.878**（撤销「饱和」这一单口径） |

**三点零调用，因为它们的语料是已经落盘的回放**——这类分析的价值在于**它随时可重算**，
不必再花一次模型调用。

---

## D. Memory 模块：达到企业前沿级别了吗

**用户的原问题是「检查 memory 相关模块是否达到企业前沿级别」。答案是分层的，必须分开说。**

**用什么 / 干了什么**：`evaluation/reports/phase5_acceptance_latest.json`（`result:
PASS_ENGINEERING_WITH_NO_PRODUCTION_MEMORY_SAMPLE`）与 `evaluation/reports/phase5_memory_latest.json`
（记忆质量评测，语料 `servicemind-phase5-memory-v2`，打分模式 `lexical`）。

**能证明的（✅ 工程面）**：

| 面 | 数字 |
|---|---|
| 写侧治理 | 48 步，动作精确匹配率 **1.0**、落库状态匹配率 **1.0**、原因码覆盖率 **1.0**；**越权激活 无**、**误拒 无** |
| ↳ 按动作拆 | `reject` 2（P/R 1.0/1.0）、`quarantine` 10（1.0/1.0）、`activate` 36（1.0/1.0） |
| 读侧安全 | 45 探针，**泄漏率 0.0**（0 条记录），无泄漏探针 |
| 硬门禁 | **14 条全部为 0**：租户越界、不安全自动激活、重放重复、过期快照接受、并发双审、上下文超预算、原始证据越界、技能能力扩张、跨租户缓存命中、高风险无控降级、成本预算绕过、程序记忆超期服务、review token 泄漏等 |
| 程序记忆 | 提案策略 `cross_ticket_verified_episode_v1`，**`auto_activation: false`**，需 ≥2 个不同 run 且 ≥2 个不同工单，并发反向决定 `one_winner`，支撑失效 → `PROCEDURAL_SUPPORT_INVALIDATED`，**读时持续重校验** |
| 评测维度 | 五类：`injection` / `isolation` / `retrieval` / `staleness` / `taint` |

**不能证明的（必须写明，否则「企业前沿级」这句话是空的）**：

1. **生产观测 `NO_DATA`**：真实 analysis envelope 数为 **0**（排除 14 条合成产物）。
   上面每一个数字都来自**本项目自己编写的回归契约语料**，**不是任何租户真实记忆分布的度量**，
   也不能替代生产记忆流量回放。

2. **审计闭环只闭了一半**：记忆 → 运行可经 `memory_records.source_run_id → agent_runs.id` 闭合；
   **`memory_events`（撤销事件）缺 `run_id`/`trace_id`**，因此**撤销事件的关联无法闭合**——
   按术语纪律，这一项记为**审计闭环不通过**。

3. **缺一次消融实验**：`SERVICEMIND_MEMORY_ENABLED` 是**纯环境变量**（全仓只有
   `orchestration/phase5_governance.py:595` 读、`:743` 写两处消费者；`.env:65` 当前为 `true`，
   而代码默认 `settings.py:285` 为 `False`）。因此**起第二个实例、设为 `false`、跑同一批案例**
   即可测「memory ON vs OFF 对最终任务质量差多少」，**不动仓库任何一行**——
   **本轮确认了这条路径可行，但没有执行**（记为 ⬜ 可做·没做）。
   设计时要注意：memory-OFF **只能靠「缺失」证明**，不能靠某个字段为 false。

**结论（一句话）**：**契约与治理面达到设计门禁，真实分布未取样。**
说「达到企业前沿级别」需要第 1 条那个观测，而它在拿到真实流量之前不存在。

---

## E. 文档交付

| 文档 | 本轮的状态 |
|---|---|
| `docs/EVALUATION_18_COVERAGE_AUDIT_2026-10-02.md` | 18 项逐项判定；**本版新增 §零之二（mtime 仪器缺陷）、§九·D5**，并把可靠性 limitation 的修复、负载语料替换的历史对照写进变更表 |
| `docs/PHASE7_REMAINING_EVALUATION_2026-10-02.md` | 七项剩余评估项；**§3 由「负载批顺带重复」扩成「设计测量 + 下限」两块**（原文被改的只有这一节，原因写在文内），新增 D5 |
| 本文 | 端到端完整流程与最终结论 |
| `docs/PHASE7_ACCEPTANCE_BASELINE.md` | Phase 7 验收基线；本轮**未改其结论**（P7.6 不在本轮范围） |

---

## 四、那一条贯穿所有批次的缺陷：`Q-002`（D5）

**它同时出现在三个批次的证据里，这是它值得单列的原因。**

- **问题**：*"How long is a VPN device certificate valid before it needs renewing?"*

- **期望**：引用 `KB-Q-VPN-CONN`。**实测**：质量批未通过、负载批三档（含并发 1 的基线）未通过、
  可靠性批 5 次重复里**一次都没引到**（被引文档会换，但**从不包含**这一篇）。

**已排除的原因**：

- **不是索引缺失**——`scripts/seed_phase7_quality_fixtures.py --check` 返回 `problems: []`，
  该文档在服务索引里、`group_ids` 与 `is_active` 正确。

- **不是抖动**——并发 1 下**稳定地**不过；5 次重复从不包含它。

- **不是负载**——见 §C3。

**为什么它同时是「缺陷」和「争议」。** 语料里三篇文档给出三个数字：金标那篇
`vpn-client-connectivity.md` 写 *"a valid device certificate issued within the last 400 days"*
（**全文没有 "renew" 这个词**），而 `remote-access-portal-v2.md`（标记为 `version conflict: current`）
写 *"A binding is valid for 365 days … Renewal is self-service"*。
**「device certificate」与「binding」是两个物件**：金标选前者有依据，但后者字面上同时命中
「有效期」与「续期」。**两种读法都写在这里，本文不替读者选一个。**

**处置（用户 2026-10-03 裁定）**：**如实记录，不改冻结案例集**——改它会改 `cases_digest`，
200 例需整批重跑。**不得**为了让它变绿去放宽 `gate_phase7_load.py` 的判据。

---

## 五、未关闭缺陷与本轮修掉的缺陷

### 5.1 未关闭（已知缺陷，含复现路径）

| 编号 | 缺陷 | 复现路径 | 影响 |
|---|---|---|---|
| **D1** | `agent_invocations` 系统性遗漏 knowledge agent | 见 `PHASE7_REMAINING_EVALUATION` §6·D1 | 审计面 |
| **D2** | agent envelope 的 `attempts` 不是重试次数 | 同上 §6·D2 | 语义误导 |
| **D3** | `plan_revision` 混淆 retrieve_more 与 replan | 同上 §6·D3 | 计划可追溯性 |
| **D5** | `Q-002` 稳定漏引一篇金标文档，且金标可争议 | §四；质量批 / 负载批 / 可靠性批三处证据 | 使 §C3 的 gate 判 FAIL，并使该批次**无法就并发效应给出归属** |
| — | `memory_events` 缺 `run_id`/`trace_id` | §D | 撤销事件审计闭环不通过 |
| — | GLPI 写入未经 ToolGateway 统一策略与调用审计 | `glpi.append_ticket_followup` 不在注册表 | 统一治理不成立 |
| — | `set_document_active` 无 HTTP 入口 | 知识版本废止无自助能力 | 运维面 |
| — | `tool_outbox` 的 `action.approved` 无消费者 | `RedisStreamConsumer` 零引用 | **不得宣称异步消费与崩溃恢复已通过** |
| — | ~~静态检查残留：`pyrefly check` 报 4 条错~~（`evaluation/lotte.py:280`、`:285`、`model_gateway/gateway.py:500`、`rag/late_interaction_torch.py:161`）**——已于同日 CI 收尾修掉，见 §5.2 与下方补记** | `uv run pyrefly check` | 采集时刻为未关闭；收尾后为 0 错 |

> **采集时刻的状态，以及收尾时的处置（10-03 补记，先记原状，再记处置）。**
>
> **采集时刻为什么不修。** 四条**都不是运行时缺陷**，是类型收窄不足：`lotte.py` 两处对 `json.loads`
> 回来的 `dict[str, object]` 直接取 `.items()` / `set(...)`；`gateway.py:500` 把 `BaseException | None`
> 传给只收 `BaseException` 的 `provider_is_down`；`late_interaction_torch.py:161` 的 `self.tokenizer`
> 被推断为可能为 `None`，而它在调用前已被赋值。当时不修有两条明说的理由：①改 `src/**/*.py` 会让
> **正在服务的 API 进程变成"比树旧"**（`§三·B1` 的那道闸），此后任何 live 批次都会被拒，直到重启一次；
> ②`gateway.py:500` 正落在本轮修过的 `provider_is_down` 附近，仓促加 `cast` 有把刚刻画清楚的降级行为
> 改回去的风险。**所以本报告正文只声称 `ruff format --check` 与 `ruff check` 通过（407 文件、全过），
> 从未声称 `pyrefly` 0 错**——`docs/PHASE5_FINAL_ARCHITECTURE_AND_ACCEPTANCE.md` 与
> `docs/PHASE7_ACCEPTANCE_BASELINE.md` 里的 "`pyrefly` 0 errors" 是**各自采集时刻**的快照。
>
> **收尾时为什么改判。** 上面第 ① 条代价是**为 live 批次付的**；live 批次已经跑完并冻结，这个代价不再存在。
> 而它换来的是 **CI 的 `test-python` 全矩阵持续变红**——那才是长期成本。于是四条都按**收窄**（而不是
> `cast` 压制）修掉：`lotte.py` 对 `entry.get("files")` 做 `isinstance(..., dict)` 收窄，畸形 manifest 报
> "全部未跟踪"而不是抛 `AttributeError`；`gateway.py` 在进入断路器判定前加 `last_error is not None` 守卫，
> 让"尝试循环从未运行"不再被当成提供方故障的证据；`late_interaction_torch.py` 对 `AutoTokenizer` 的返回值
> 显式检查并在为空时抛 `FileNotFoundError`。**没有一处放宽断言、没有一处用 `cast` 掩盖。**
>
> **代价与边界照记。** 这三处都在 `src/**`，所以 `source_revision` 随之移动；本报告 §2.2 各批次的版本绑定是
> **采集时刻**的快照，因此不受影响，但也**不能**把收尾后的树说成"就是那些批次跑的那棵树"。

### 5.2 本轮修掉的（每条都有锁定测试）

| 缺陷 | 类型 | 验证 |
|---|---|---|
| 被测进程与源码树不一致（B1） | **仪器** | 六个批次逐一断言接线；重启后 `stale_deployment()` 返回 `None` |
| 变异框架还原不还原 mtime（B2） | **仪器** | 2 条锁定测试；安全批重跑 87 条变异证据全 `RED (good)` |
| 语义裁判 claim 索引契约缺口（B3/D4） | **产品** | 误纳 **0.714 → 0.0**；零调用重放探针锁定 |
| 重放探针按旧字段重建判决（B3 支线） | **仪器** | 「字段不全即退 3 拒绝」+ 锁定测试；修前渲染冻结不删 |
| 部署臂在搜索前丢弃用户原话（A1） | **产品** | 七臂 × 两样本；控制臂噪声下界 R@10 移动 0 条 |
| 可靠性报告的 limitation 与自己的表格矛盾 | **报告** | 改为由语料推出；3 个变异全部变红 |
| `pyrefly` 4 条类型收窄缺口（CI 收尾） | **静态** | `uv run pyrefly check` → **0 errors**；全仓 `pytest` 1398 passed / 26 skipped；三处均为收窄，非 `cast` |

---

## 六、未评估 / 结构性做不了 / 有路径但没做

**这三类必须分开写，不能互相顶替。**

### 6.1 结构性做不了（⛔ 不存在执行路径）

| 项 | 为什么 |
|---|---|
| **17 Human / Judge Calibration** | 需要真实领域标注员，而标签元数据里 `annotators = 0`、`kappa = None`。没有 kappa，「模型判断与人类一致」无法测量。当前评委是模型自评——**自评与人类的一致性恰好是最无法自证的一项**。 |
| **18 Online Production & Business KPI** | 项目未上线：`production_observation.status = NO_DATA`、真实 analysis envelope **0**、GLPI 知识库 **0 篇**、`production_load_capacity: not_certified_in_phase6`。没有真实用户、流量、知识库，就没有线上指标。 |
| **02 / 04 的业务侧** | 同 18。工程面可测，业务面在拿到真实环境之前做不了。 |

**附带的结构性问题（同源）**：GLPI 知识链路是断的——`GlpiKnowledgeBaseSource` 在整仓
**从未被实例化**，唯一的 ingest 入口只接了 internal / pagerduty / mendeley 三个源。
**即便明天上线，知识侧也没有内容可检索。**

### 6.2 未评估（⬜ 有路径，未执行）

| 项 | 状态 |
|---|---|
| Dynamic red team | 未执行。**已确定的设计**：用活的模型生成对抗性输入，判据由**人预先冻结**（否则「模型判模型」是自证）。注意 B3 已显示：**在修好契约之前，红队用例不带 claims 会系统性全通过**——所以修 B3 是它的前置，而**红队本身仍未做**（它是新建工作，不是重跑）。 |
| Chaos（故障注入） | 未执行。需要可注入故障的独立实例（提供方超时/402、OpenSearch 不可达、Neo4j 降级、GLPI 写失败）。仓库里已有 `provider_is_down()` 这条分类；10-02 的 402 欠费窗口是一次**真实**的混沌事件，但其期间的观测**没有被记录成回放**，因此不可用。 |
| Long soak（长稳） | 未执行。需要独立长期运行实例与时间窗，与「组里的 3090 是共享的」这条硬约束冲突。 |
| **Memory 消融实验** | 未执行，且**零成本**（见 §D 第 3 条）。 |
| 12 的非对抗红队侧 | 未执行（同 Dynamic red team）。 |
| `--run-docker` 的真集群索引生命周期套件 | 未在本轮执行。 |

### 6.3 按裁定不做

| 项 | 原因 |
|---|---|
| **15 成本** | 按用户裁定不做，仅记录状态。 |
| 更多 RAG 工作 | 用户 2026-10-03 裁定：剩下的是标签问题，**如实记录、不改冻结案例集、不再修 RAG**。 |
| 改冻结案例集 | 会改 `cases_digest`，200 例需整批重跑；且**这是为了让判据变绿而改判据**。 |

---

## 七、这份交付不证明什么（边界）

1. **所有 live 结论都是「在这批语料 + 这个冻结版本 + 这个租户上」。** 语料是自建的 44 篇文档，
   不是租户真实知识库。**换语料会得到另一个数字。**

2. **四份「已记录」报告不是在线验收。** 轨迹 / 协同 / 可靠性(b) / 语义层读的是**已记录**的运行，
   证明的是「这些记录支持什么结论」，不是「当前代码在线会这样跑」。
   在线证明是 gate 的职责，见 §C。

3. **安全批不是对抗性红队**（见 §C2 的 8 项 `not_covered`，关键一条：
   **从未有人真正攻击过这套系统**）。

4. **70% 的安全场景"只由通过的测试背书"**，与有变异背书的 42 个不是同一个强度。

5. **可靠性 (a) 只有 8 个案例**——样本小是有意的代价（换来并发固定为 1），
   但结论的适用范围就是这 8 个案例。

6. **语义层是每例一次采样**，边界用例重复跑会移动；应把「抓住 / 漏掉」读作**关于这些输入**的证据，
   不是总体比率。

7. **`Q-002` 这种"金标可争议"的情况，本文不替读者选一种读法。**

8. **本文不产生任何新观测，也不改任何已冻结的判据。**

---

## 附录 A：本报告引用的产物清单

| 产物 | 用途 |
|---|---|
| `evaluation/reports/phase7_quality_latest.{json,md}` + `evaluation/quality/replays/`（201） | §C1 |
| `evaluation/reports/phase7_security_latest.{json,md}` + `evaluation/security/replays/`（72）+ `scenarios.v1.json` | §C2 |
| `evaluation/reports/phase7_load_latest.{json,md}` + `evaluation/load/replays/`（121）+ `evaluation/load/plan.v1.json` | §C3 |
| `evaluation/reliability/replays/`（41）+ `evaluation/reports/phase7_reliability_recorded_latest.{json,md}` | §C4 |
| `evaluation/reports/phase7_trajectory_latest.json` / `phase7_coordination_latest.json` / `phase7_routing_hard_latest.json` | §C5 |
| `evaluation/reports/phase4_query_arm_funnel_latest.json` | §A1 / §A2 |
| `evaluation/reports/phase4_r2_decision_latest.json` | §A3 |
| `evaluation/reports/rag_quality_status_latest.json` + `phase4_packing_ceiling_latest.json` + `phase4_label_diagnostic_latest.json` | §A0 |
| `evaluation/reports/phase5_acceptance_latest.json` + `phase5_memory_latest.{json,md}` | §D |
| `evaluation/reports/phase7_reviewer_semantic_latest.{json,md}` + `..._replay_latest.*` + `..._replay_prefix_2026-10-02.*` | §B3 |
| `evaluation/reports/phase7_campaign_environment.json` | §2.2 冻结记录 |
| `src/servicemind/evaluation/deployment.py` + `tests/servicemind/test_phase7_deployment_freshness.py` | §B1 / §B2 |
| `scripts/mutation_harness.py` | §B2 |
| `tests/servicemind/test_phase7_reliability_limitations.py` | §C4 |
| `tests/servicemind/test_phase7_coordination_limitations.py` | §C5 |
| `scripts/report_phase7_trajectory.py` | §C5（重渲染 200 例句语的生成器） |
| `scripts/report_phase7_coordination_recorded.py` | §C5（同上；limitations 由语料推出） |
| `evaluation/reports/phase7_acceptance_2026-10-03.{json,md}` + `evaluation/acceptance/replays_2026-10-03/`（28） | §2.2（**全量 28 例在当前版本上的执行证据**：28/28 PASS、107/107 断言、gate 退 0，含 8 个真实写入 GLPI 的案例） |
| `evaluation/reports/phase7_acceptance_readonly_2026-10-03.{json,md}` + `evaluation/acceptance/replays_readonly_2026-10-03/`（20） | §2.2（中间版本的只读语料，**已被取代、不删**） |
| `evaluation/reports/phase7_acceptance_latest.{json,md}` + `evaluation/acceptance/replays/`（28） | §2.2（标准语料，09-30 采样，可归属但不当前） |
| `tests/servicemind/test_phase7_acceptance_replays_corpus.py` | §2.2（锁定 `--replays` 默认值仍指向标准语料；断言走 AST，子串版本对变异无感，已废弃） |

## 附录 B：复现命令

```bash

# 静态（每步都应先跑）

# 静态三条例行通过（407 文件）。§5.1 采集时刻的 4 条 pyrefly 报错已在同日 CI 收尾中

# 按"收窄"修掉（见 §5.1 补记与 §5.2），此处期望 0 errors。
uv run ruff format --check . && uv run ruff check .   # 期望：0 文件待格式化、All checks passed
uv run pyrefly check                                   # 期望：0 errors

# 仪器：部署新鲜度 + 变异框架还原
uv run pytest tests/servicemind/test_phase7_deployment_freshness.py \
              tests/servicemind/test_phase7_reliability_limitations.py \
              tests/servicemind/test_phase7_coordination_limitations.py -q

# 三个 gate 的判定（纯函数，不产生新观测）
uv run python scripts/gate_phase7_quality.py  --check ; echo "quality  exit=$?"   # 期望 0
uv run python scripts/gate_phase7_security.py --check ; echo "security exit=$?"   # 期望 0
uv run python scripts/gate_phase7_load.py     --check ; echo "load     exit=$?"   # 期望 1

# 可靠性下限（纯文件分析，零模型调用）
uv run python scripts/report_phase7_reliability_recorded.py

# 验收：只读 20 例的语料隔离与默认值（不连任何服务）
uv run pytest tests/servicemind/test_phase7_acceptance_replays_corpus.py -q

# 验收：复核全量 28 例的判定（纯函数；--report 指到新路径，不覆盖标准报告）
uv run python scripts/gate_phase7_acceptance.py --check --replay-only \
    --replays evaluation/acceptance/replays_2026-10-03 \
    --report evaluation/reports/phase7_acceptance_2026-10-03.md \
    --expect-revision 'b385df7c2ef6818f24d5f173ca92158989ba3c66+patch(fbdbefc59bbc)'

# 期望：退 0，PASS 28 / FAIL 0 / BLOCKED 0，107 条断言，blockers: []
uv run python scripts/report_phase7_reliability_recorded.py --check               # 期望 0

# 重跑 live 批次（会调用模型，且必须先过部署新鲜度闸）
uv run python scripts/verify_phase7_quality_live.py --tenant-id 22222222-2222-4222-8222-222222222222
uv run python scripts/verify_phase7_security.py
uv run python scripts/verify_phase7_load_live.py --tenant-id 22222222-2222-4222-8222-222222222222

```

**纪律**：不 `source .env`；不打印任何密钥值；不重启 / 停止 / 重建任何在跑的容器；
不改 `.venv` 依赖锁与共享 miniconda 环境；不擅自 commit / push。
