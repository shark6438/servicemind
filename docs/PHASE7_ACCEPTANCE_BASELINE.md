# ServiceMind Phase 7 验收基线（P7.6 核心业务闭环验收）

> 版本：v3.4（**执行后**）
> 判定规则冻结日期：2026-09-22；v3.0/v3.1/v3.2 执行完成 2026-09-23；v3.3 执行完成 2026-09-30；**本次（v3.4）执行完成 2026-10-01**
> 范围：P7.6 核心业务闭环验收——**28 个案例**（ACC-01…ACC-13 共 18 条业务闭环 + ACC-14…ACC-17 共 4 个独立探针 + ACC-18…ACC-23 共 6 条核验器配置后的撤权与核验真实验证）
> 结论：**28 条案例的判定与 v3.3 逐字相同**——总判定 `PASS`，验收 gate 退出码 `0`，计数 **PASS 28 / FAIL 0 / BLOCKED 0**，**107 条断言全部 PASS**（质量/负载/安全三门为退出 3，见下）。**v3.4 不改案例、不改判定规则、不放宽任何断言**：本轮改的是两条新确认的缺陷（R4 规划器形状、R5 版本号）、用**四组廉价探针**去替代「先花 ¥40 重录再发现问题」，并**把整张缺陷清单（D4–D13 共 10 条）拿回代码里逐条回读**——结果是 8 条早已修复、2 条的原始描述本身不成立，**本轮为此一行源码都没有改**。**「缺陷清单 10 → 0」与「本轮修复 0 条」是同一件事的两面，必须一起读。**

**v3.4 是一次「先探针、后付费」的轮次**，因此它的主要产物不是新的通过数字，而是**对既有结论的处置**（两条修复、一条更正、一条未评估项关闭、以及**一次对整张缺陷清单的回读**）：

| 编号 | 性质 | 本轮结论 | 证据 |
| --- | --- | --- | --- |
| **R4** | **产品缺陷**：语料未覆盖的问句会让规划器产出**残缺计划**（有证据任务、没有分析或评审），两次重试后整个运行以 `critical_error` 结束，用户**什么也拿不到** | **已修**：证据流水线的补全改为**确定性策略**（`_complete_evidence_pipeline`），提示词同步改写。实测 Q-200 由 `failed` 变为 `succeeded/passed/7 引用/33.9s` | 「v3.4 的根因修复」R4，含 5 条新测试、一次变异验证与一次活体复跑 |
| **R5** | **判定器缺陷**：`deployed_revision` 用 `git status --porcelain` 的**行数**当版本号，而驱动的证据就写在同一棵树里，于是**记录这个动作本身改变了被记录的版本号** | **已修**：改为对**源码树内容**取指纹（`<sha>` 或 `<sha>+patch(<12 hex>)`），四端共用一份实现。实测四端现在得到**同一个**字符串 | 「v3.4 的根因修复」R5，含 13 条新测试与 3/3 变异验证 |
| **A3** | **既有结论被证伪**：v3.3 把负载 `tier-10` 的 FAIL 归因为「服务方并发上限是 5，该档位不可运行」 | **归因错误，已更正**：上限是**由余额推导**的（6 秒内从 7 掉到 5），该批次真正的死因是**账户余额耗尽**（402）。在**账户已充值**的当前构建上重录：**tier-10 60/60 succeeded，168.2 秒** | 「验收证据」八（该节已整段重写：**表格保留、结论句替换**——v3.3 记录的失败行仍然真实，变的只是对它的解释） |
| **A4** | **未评估项被关闭**：安全语料中 2 条证据此前记为 `unexercised` | **已关闭**：`SEC-OUTBOX-02` 与 `SEC-TENANT-06` 的两条证据是**真实 PostgreSQL 上的 RLS 删除隔离**测试，只是缺 `--run-docker`。补录后该 2 条场景共 8 条证据**全部 passed**，全语料**0 条 unexercised** | 「验收证据」九 |
| **D5** | **一条被记了两轮的「未关闭缺陷」，其实早已关闭**：本文件的 D5 行自 v3.0 起写着「修复 7 只改重试的**时机**，不改「重试仍失败后如何降级」——因此它不是 D5 的修复」，并在「关闭本阶段的前置」里列为「仍未做」 | **该行自 2026-09-24 起即已过时**：D5 的两半在提交 `688dd90` 中都已实现——(a) 限流不再按运输故障重试，而是用**运行自己的截止时钟**等待（`throttle_wait_seconds` / `_model_analysis_resilient`）；(b) 降级后的裁决由**首次即升级**改为**先重规划一次**（评审器 `_deterministic_gate`，受 `max_replans` 约束）。本轮**重新核验并做变异验证**：改回「永远升级」→ 1 红；令限流永不等待 → **10 红**；还原转绿（28 passed）。**D5 关闭。** 本文件的错误**不是判断错，是没重读**——见「历史口径修正」第八条 | 「本阶段已修复并锁定」D5 |
| **D4–D13** | **整张「已知未关闭缺陷」表的十条，自写下起就一直被抄写，从未被回读** | **顺着 D5 把剩下九条一并读完，才发现 D5 不是个例而是通例**：十条中 **8 条早已修复**（D4、D6、D9、D10、D11、D12、D13 各有代码落点与锁定测试，最久的自 2026-09-23 起即已修好）、**2 条的原始描述本身不成立**（D7 的「顺序耦合」被 14 条相对增量断言证伪；D8 的投递半已部署、`servicemind-outbox.service` 长期 active、实测 98 行全部 `published`、零 `pending`/`failed`）。**本轮为此一行源码都没有改。** 于是「缺陷清单 10 → 0」与「本轮修复 0 条」是同一件事的两面，必须一起写 | 「其他已知未关闭缺陷（v3.4 逐条回读后重写）」——逐条列出**代码落点、锁定测试、以及每条「不被证明的部分」** |

> **G1 的后果必须与本文件的结论一起读**：它使**已提交的质量语料**（200 条观测横跨 4 个源码版本）从「PASS」变成「退出 3 拒判」。这是**修复生效**，不是回归——但它同时意味着**离线 CI 半（`phase7-offline-gates`）在四份语料各自按单一版本重录之前无法恢复**。该重录（业务质量 200 条 + 负载 120 次 + 安全 72 场 + 验收 28 例，实测总成本约 $5.54 ≈ ¥40）**不在本轮执行范围内**，列在「未评估」与「边界与下一阶段」中。

> **R5 的后果同样必须一起读，而且它改变了「重录一次就能恢复」这个判断。** R5 之前，那条恢复路径**根本不成立**：版本号数的是 `git status` 的行数，而每次重录都会把证据文件写回工作树，于是**重录出来的版本号必定与上一次不同**——四条 gate 的报错文案都在说「re-run the batch against a single deployment」，而**那句话在当时是做不到的**。R5 把版本号改成源码内容的指纹之后，这句话第一次成为一条**可执行**的指令：四份语料只要在同一次记录活动中产出、期间不改源码，就会得到同一个字符串。实测：四个驱动（acceptance / security / quality / load）现在对同一棵工作树返回**逐字相同**的 `cf08ac8a7b731054e492ed81ba5f3164dc381863+patch(0ce86f5d19bc)`。
>
> **本轮四门 gate 的退出码，与它们各自的成因（必须分开读，不能合并成「四门绿了没」）**：

| gate | v3.3 退出码 | **v3.4 退出码** | 成因 |
| --- | --- | --- | --- |
| 验收 | 0 | **0（PASS）** | 28 份回放**本身同源**（单一版本），案例与断言均未改动 |
| 质量 | 3 | **3** | 语料是**修复前**录的：200 条横跨 6 个 `dirty(N)` 版本。这是 R5 要消灭的现象，由阶段 B 收敛 |
| 负载 | 1 | **3** | 同上，语料横跨 2 个 `dirty(N)` 版本。**注意：v3.3 的 1（有 FAIL 判定）在 v3.4 已不存在**——本轮把 tier-10 在已充值账户上重录为 60/60 succeeded，见「验收证据」八 |
| 安全 | 0 | **3** | 72 场中有 2 场是本轮用 `--run-docker` 补录的（新格式），与另外 70 场（旧格式）不同源。**这正是 R5 描述的机制在新旧格式交界处的一次真实显形** |

> **必须点明的一件事**：v3.4 下有三门取 3，**不是本轮的代码改动让它们变红的**，而是**旧证据自带异质性**——它们本来就该取 3（质量门在 v3.3 就已经是 3）。安全门 v3.3 取 0 反而是那个**不该有的绿**：那份语料当时同源但陈旧，且其中 2 条证据是 `unexercised`。本轮把它补录完整之后，新旧格式的差异才把它显形为 3。

本文件的判定规则与缺陷口径在执行前冻结；执行结果由 `scripts/gate_phase7_acceptance.py` 依据案例清单与回放**机器生成**，见
[`evaluation/reports/phase7_acceptance_latest.md`](../evaluation/reports/phase7_acceptance_latest.md)（人读）与
[`evaluation/reports/phase7_acceptance_latest.json`](../evaluation/reports/phase7_acceptance_latest.json)（机器源）。
本文件引用并摘录该机器报告，**不另写一套叙述**；两者不一致时以机器报告为准。

## 版本沿革（v1.0 → v2.0 → v3.0 → v3.1 → v3.2 → v3.3 → v3.4）

| | v1.0 | v2.0 | v3.0 | v3.1 | v3.2 | v3.3 | **v3.4** |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 案例数 | 22 | 28 | 28（不变） | 28（不变） | 28（不变） | 28（不变） | 28（不变） |
| 核验身份 | **未配置** | 已配置且可达 | 已配置且可达（不变） | 已配置且可达（不变） | 已配置且可达（不变） | 已配置且可达（不变） | 已配置且可达（不变） |
| 计数 | PASS 16 / FAIL 1 / BLOCKED 5 | PASS 26 / FAIL 1 / BLOCKED 1 | PASS 27 / FAIL 0 / BLOCKED 1 | PASS 26 / FAIL 2 / BLOCKED 0 | PASS 28 / FAIL 0 / BLOCKED 0 | PASS 28 / FAIL 0 / BLOCKED 0 | **PASS 28 / FAIL 0 / BLOCKED 0（不变）** |
| 断言计数 | — | — | — | 105 条：103 PASS / 2 FAIL | 106 条：106 PASS / 0 FAIL | 107 条：107 PASS / 0 FAIL | **107 条：107 PASS / 0 FAIL（不变）** |
| FAIL | ACC-03 | ACC-03 | 无 | ACC-03、ACC-06 | 无 | 无 | **无** |
| 阻塞缺陷 | D1 + 5 条 BLOCKED | D1 + D14（ACC-03）、D15（ACC-12b） | 仅 D15（ACC-12b） | D16（ACC-03）、D17（ACC-06） | 无（D3 / D16 / D17 均已关闭） | 无（R1 / R3 / G1 三条本轮新确认的缺陷已修复） | 无（**另有 R4 / R5 两条本轮新确认的缺陷已修复；未评估项比 v3.3 少一项**） |
| gate 退出码 | 1 | 1 | 2（观测不足：必需案例 BLOCKED） | 1（有 blocking FAIL） | 0（PASS） | 0（PASS） | **0（PASS）** |
| 被测源码版本 | — | — | — | `7a6e675` | `7a6e675` | `cf08ac8`（v3.2 之后仓库新增的一次提交） | **`cf08ac8+patch(0ce86f5d19bc)`**（同为 `cf08ac8`，v3.4 起版本号按**源码内容指纹**记录） |
| 判定 | 未通过 | 未通过 | 仍未通过 | 仍未通过 | 通过 | 通过 | **通过** |

**v3.4 的计数与 v3.3 逐字相同，但这一列的读法与前几轮不同。** v3.3 的「计数不变」说明的是「收紧判定器没有改变这 28 条的结论」；**v3.4 的「计数不变」说明的是「本轮根本没有去动那 28 条」**——R4 与 R5 一个改在规划器（不在 28 条案例的判定路径上决定成败），一个改在**版本号的产生方式**（28 条案例的回放本身同源，故不受影响）。**因此本列不构成一次「28 条重新验证过」的声明**：v3.4 没有重跑 28 条案例，它复核的是四门 gate 的取码与四份语料的成因。

**v3.2 → v3.3 的计数没有变化，但这不是「什么都没做」。** 本轮的全部改动都落在**判定器与错误记录**上（R1 路由、R3 错误持久化、G1 证据同源性），没有一个字是为了让 28 条断言更容易通过；计数不变恰好说明**收紧判定器没有改变这 28 条的结论**——如果 G1 让某个 gate 从 PASS 变 FAIL、或 R1 让某条案例从 PASS 变 FAIL，那才是需要解释的信号。**唯一被 G1 改变结论的是质量 gate（不是本文件的 28 条案例）**：它从 PASS 变成退出 3，成因见下。

v3.0 不是「重跑一次」，而是**五处产品根因修复（上下文回收 / 分析信封 / 续跑与形状规则 / 错误文本 / 限流退避）+ 一处验收套件自缺陷的裁定 + 一次夹具扩张**之后的重测。**v1.0 与 v2.0 的计数一律不得沿用**；其中 v2.0 关于 D1 的两条推演已被实测推翻（见「历史口径修正」）。

**v3.1 是 D15 被用户裁定后的实施与重测**：修的是模式身份与佐证可见性这一对自锁（含生产侧门禁、查询侧谓词、**Postgres 侧 SQL 预过滤**三处），并把 ACC-12b 的激活主体缺陷一并更正。**v3.1 的计数不得与 v3.0 直接相减后归因于本次代码改动**——`ACC-12b` 由 BLOCKED 转 PASS 是本次修复的目标；`ACC-03` 与 `ACC-06` 由 PASS 转 FAIL 经实测**不是**本次代码改动造成的（两条成因见「验收证据」二、三），把它们记成「本次修复的代价」会是一个错误的归因。

**v3.2 是 v3.1 三条遗留缺陷的根因修复与重测**，三条缺陷**性质各不相同，修法也各不相同**，逐条对应关系必须先说清，否则「PASS 26 → PASS 28」会被误读成「把断言调松了」：

| 缺陷 | 性质 | 本轮处置 | 修的是产品还是案例 |
| --- | --- | --- | --- |
| **D3** | **产品缺陷**：评审器拿到的证据集是**分析代理见过的那一份的超集**（分析信封裁剪掉的证据，评审器仍看得见） | 评审器的证据集改为**分析代理实际交付的那一份**（`analysis_evidence_ids`），并保留 `∪ cited` 兜底 | 产品（`phase5_governance.py` + `state.py` + `supervisor_workflow.py`） |
| **D16** | **产品缺陷（提示词侧）**：模型把「用来排除某候选的文档」一并挂进根因 claim 的 `evidence_refs`，而该文档说的正是相反的结论 | 分析提示词新增排除性依据条款；**同时**判定器新增 `must_cite` 正向断言，堵住「什么都不引也算合规」的空满足 | **两侧都修**：提示词约束是产品侧，`must_cite` 是判定器侧 |
| **D17** | **验收套件自身的输入前提不成立**：ACC-06 的载荷**实测到不了预算上限**，裁剪与否取决于评审器是否恰好吃紧 | 把裁剪断言改接**确定性探针**——直接对 `ContextBuilder` 求值一次固定超预算载荷 | 案例（`cases.v1.json` + 驱动新增探针） |

**因此 v3.2 的 PASS 28 里，只有 D17 一项涉及案例改动**；D3 与 D16 的主体是产品代码与提示词。D17 的改法也**不是放宽断言**：原先那条断言在 5 次运行里 5 次没有产生任何信号（见 v3.1「验收证据」三），新断言在**每一次**运行里都给出确定结果（实测：`control 23 selected / 799×2 selected / 799×4 pruned`）。

## 最终裁定

本文件冻结的是 **P7.6 核心业务闭环验收**，不等同于 Phase 7 全部完成。

**关于 Phase 7 的现状，必须如实记录**：在本阶段之前，仓库内不存在任何 Phase 7 产物——无 `docs/PHASE7*`、无 `scripts/*phase7*`、无 Phase 7 报告、无 Phase 7 CI job。Phase 7 此前仅以**散文形式**存在于 `docs/企业IT服务管理(ITSM)智能体平台.md`（Phase 7：Observability、Evaluation、红队与 CI Gate）与 `docs/PHASE6_ENTERPRISE_ACCEPTANCE.md:70` 的执行顺序建议。因此：

- P7.6 **没有已实现的父产物可挂**；它是 Phase 7 的第一个落地产物，编号表示**范围**而非执行顺序。
- P7.4 的 CI/release gate 是**新建**，不是复用。
- P7.0–P7.5 本身列为「未评估」。

**本阶段的实际结论**：核心业务闭环**通过**。28 个案例全部 PASS、0 条 FAIL、0 条 BLOCKED，107 条断言全部 PASS；按冻结判定规则，无 blocking 断言 FAIL、无必需案例 BLOCKED，gate 以退出码 **0** 结束。

**「通过」的射程必须先说清，它比结论本身更重要**：

- 它说的是：在下方「冻结范围」与「被测版本绑定」所限定的租户、身份、语料、工单、部署版本与配置下，**28 条案例的断言在本次运行中全部成立**。
- 它**不**说：平台没有缺陷、容量已认证、或 Phase 7 已完成。P7.0–P7.5 与 P7.6.5/6/7 仍在「未评估」中。（**「缺陷清单未关闭项为 0」这句话本身也要读准**：v3.4 清空的是**那一张表**，靠的是**回读**而不是修复；本文档另有一份**从未被同样回读过**的已关闭清单，见「未评估」。）
- 它**尤其不**说：v3.1 记下的两条「单次 PASS 不等于稳定 PASS」的口径可以作废。该口径在 v3.2 仍然生效，并且**本轮用它自己的方式又兑现了一次**——见下方「历史口径修正」的 v3.2 一条。

v3.2 与 v3.1 的实质差别有三点，逐条写清：

1. **D3 关闭：评审器不再被拿「分析代理没见过的证据」来判定。** 评审器的证据集原为**全集**（分析信封裁剪掉的证据一并交给评审器），本轮改为**分析代理实际交付的那一份**（`analysis_evidence_ids`），并保留「**∪ 分析实际引用过的 id**」作为兜底——引用必须可解析，否则报错的是「解析」而不是「根因」。**这不是把评审放宽**：它把判定对象从「一个分析从未获得过的材料」改回「这个分析本身」，是**收紧**了评审与分析的对应关系。改动前后各有一条锁定测试守着两个方向（子集语义 + 引用兜底），并做过三次变异验证（见「v3.2 的锁定测试与变异验证」）。
2. **D16 关闭：产品提示词与判定器两侧同时堵上。** 产品侧：分析提示词明文规定「用来排除某候选的文档**不构成**所命名根因的证据，该文档不得出现在根因 claim 的 `evidence_refs` 里，排除理由写进 `statement` 或 `assumptions`」。判定器侧：`RequiredFact` 新增 `must_cite`，根因 claim **必须**引用正确手册，堵住「把 refs 清空就合规」这条空满足路径。**这一条尤其不是放宽**——ACC-03 在 v3.1 是「引用集合里不得有诱饵」，本轮**追加**了「必须引用正确手册」，约束是变多了。实测证据：本次共 **6 次**观测（5 次独立重复 + 最终全量批次 1 次），根因 claim 的 `evidence_refs` **6/6 逐字为同一对 id**（不再波动），且诱饵仍每次都被检索、被 `incident_fact` 引用（判别性引用照旧保留）。
3. **D17 关闭：裁剪断言改接确定性探针。** 原断言依赖实跑运行恰好超预算，v3.1 实测 5/5 零裁剪——**该断言在多数运行下不产生信号**。本轮把它改为直接对 `ContextBuilder` 求值一次固定超预算载荷（2000 可用 token 对 6 条各 799 token 的证据行），断言「至少一条证据行被选中、且至少一条被丢弃并写明非空理由」。实跑一侧**保留**为两条更弱但恒真的断言（选择清单非空 + 裁剪之后仍到达成功终态），因此该案例对活链路的覆盖面**只增不减**。

### v3.3 与 v3.2 的实质差别（2026-09-30）

v3.3 的改动**不在 28 条案例的判定规则里**，而在**判定器本身**与**失败记录**上。四条逐条写清，因为其中一条（R2）的结论恰恰是「**什么都没改**」：

1. **R1 已修：确定路由不再把故障诊断问句当成知识直答。** v3.2 之前，`incident_fact`/故障诊断类问句在路由器的快路径上被判为 `simple_knowledge_query`，于是「一个 VPN 认证失败的用户问题」会被路由到**只做一次知识检索并直接作答**的路径，而不是派发分析代理去读工单、比对知识、给根因。**这是一条真实的产品缺陷**：它不报错、不掉断言，只是安静地给出一个更浅的答案。修法是新增 `incident` 诊断路径，并在金标语料上验证：**107 例 `accuracy = 1.0`，`fast_path_recall = 1.0`（该走快路径的一次不漏），`unnecessary_agent_rate = 0`（不该派代理的一次不派），混淆矩阵为对角阵**。活体对照见「验收证据」：ACC-01 落在 `complex_workflow`（`analysis_or_action_required`，confidence 0.98），ACC-02 落在 `simple_knowledge_query`（`knowledge_lookup_only`，0.97）——**同一个批次里两条路径都被走到**，这比只看一个总数更能说明路由在按内容分叉。

2. **R2 未复现，未改语义——这一条是本轮唯一「修了但没有代码 diff」的处置。** 疑点是「评审器判定不稳定 + 旁支 claim 拦掉整个动作」。三次**活体**探针（每次都是真实模型、真实工具、真实 GLPI 读）全部走到 `waiting_approval`，并给出结构正确的 `append_ticket_followup`；**没有一次复现出「动作被凭空拦掉」**。因此本轮**没有改动评审器的安全语义**。这样做是有代价的——报告上少一条「已修复」，但**为了让报告好看而去改一条复现不出来的安全路径，会用一个真实的缺陷换一个虚假的通过**。该疑点因此记为**未复现的观察**，不下「已修复」的结论，也不删记录。

3. **R3 已修：失败运行的 `agent_runs.error` 从「恒为 NULL」改为「写进原因」。** 修复前，一个 `failed` 的 run 在 `agent_runs` 行上只有 `status='failed'`，`error` 为 `NULL`；**原因只存在于 `run_events` 流里**，而事件流不是「一列失败运行」这个界面会读的东西。修复后 `<termination_code>: <reason>` 落进 run 行，`api.py` 的三条续跑路径（webhook、审批、复核续跑）改用 `bounded_error_text`，并顺带修掉了一个**潜伏的类型 bug**：那条分支把 `type(failure).__name__` 里的 `failure` 当成异常，而它实际是字符串，于是记录出的 `error_type` 是 `"str"`——一个什么也没说的类型名。**回溯佐证**：本轮在排查负载 gate 时读到的旧 `tier-10` 失败行（v3.2 之前的代码产出）正是 `status='failed' / error=NULL`，而 `run_events` 里原因写得清清楚楚（`402 Insufficient Balance`、`429 concurrency limit is 5`）——**这正是 R3 关闭的那个缺口在真实数据上的样子**。

4. **G1 已修：四个 gate 现在拒判「证据横跨多个版本」的批次。** 这是本轮最有普遍性的一条。四个 gate（质量 / 验收 / 安全 / 负载）**每一个都读 `deployed_revision`**，安全与负载的 grader 甚至把它算成一个集合、在报告里渲染成 `被测版本`——**但四个 gate 的退出码都不取决于这个集合有几个成员**。实测：`evaluation/quality/replays` 里 200 条观测横跨 **4 个源码版本**（`7a6e675+dirty(120)` ×38、`7a6e675+dirty(121)` ×158、`cf08ac8+dirty(8)` ×2、`688dd90+dirty(131)` ×2），批头还写着第 5 种组合，而 gate 报 **PASS，退出 0**。**一个横跨四个平台的判定是关于其中任何一个的判定吗？** 修法分两半，分别对应两种不同的问题：**同源性**（观测之间是否一致）不需要任何外部输入，因此**总是检查**；**时新性**（这批观测是否就是当前这次部署）离线 checkout 无从得知，由 `--expect-revision` 提供。修复后这四项的实测（**这是 v3.3 的取码，v3.4 的三项已被旧证据的异质性推到 3**，见「十一」）：质量 **退出 3**（并逐字列出四个版本与各自条数）、验收 **退出 0** 且 `deployed_revisions` 为单一版本、安全 **退出 0**（单一版本，但相对 HEAD 已陈旧）、负载 **退出 1**（单一版本，且是**预先存在**的 FAIL，成因见下）。**注意 v3.3 的「安全退出 0」是一个不该存在的绿**：那份语料当时同源但陈旧，且其中 2 条证据是 `unexercised`——v3.4 把它补录完整后，新旧格式的差异才把它显形为 3。

## 术语纪律

| 标记 | 含义 | 纪律 |
| --- | --- | --- |
| **已知未关闭缺陷** | 已确认存在、尚未修复的缺陷 | 必须进入缺陷清单，含复现路径与影响面；**不得因未验证而消失** |
| **未评估** | 尚未执行的验证（无观测数据） | 只描述验证缺口；**不等于无缺陷，也不等于有缺陷** |

两者在覆盖表中是**独立两列**，任何情况下不得互相替代。凡结论性陈述，必须能追溯到一条实际观测；无观测即标「未评估」。

**BLOCKED 的语义**：保留用于分诊（区分「卡住」与「答错」），**不与 FAIL 混同，也不计入通过**；必需案例 BLOCKED 同样阻断验收关闭。核验器未配置/不可达时，「暂停续跑、无外部副作用」这类**负向案例**是**真实通过**，只有**需要正常写入**的案例记 BLOCKED。两者不得互相顶替。

**v3.2 与 v3.3 的退出码都是 0，含义同样必须读准**：gate 的取码顺序是「有 blocking FAIL → 1；否则有必需案例 BLOCKED → 2；否则退出 0」。两轮的 FAIL 都是 0 条、BLOCKED 都是 0 条，故取 0。**退出码 0 只表示「本次冻结范围内没有断言失败、没有必需案例卡住」**——它不表示平台已无缺陷（**v3.4 清空了缺陷表，但那是回读的结果而非修复的结果**，且已关闭清单从未被同样回读）、不表示未评估项已被验证，也不表示通过是永久的：**任何一次案例改动、回放重跑或配置漂移都会重新取码**（案例改动而未重跑 → 退出 3，见「篡改反证」）。

**一条必须分清的事**：**验收 gate 在本轮取 0，而同一次工作树上的质量 gate 取 3、负载 gate 取 1**（成因见「八」与「G1 带来的一个必须交代的后果」）。**这不矛盾**——四个 gate 判的是四份不同的证据，结论只对各自那批证据成立。「验收 PASS」说的是「28 条业务案例在 `cf08ac8` 上全部成立」，**不是**「四条 gate 一起绿了」。

**沿革上的取码含义，一并留档**：v3.1 取 1（有 blocking FAIL），v3.0 取 2（必需案例 BLOCKED 导致观测不足），**v3.2 与 v3.3 取 0**。**1 与 2 都表示未通过，只是分诊方向不同**；0 自 v3.2 起连续两轮出现。三个码之间**不存在「谁的结论更强」的排序**——它们描述的是失败的不同成因，而本轮只是恰好没有那两种成因。

**离线与在线，两句话分开写**：

1. **离线回放只证明判定器可重放**——`--replay-only --check` 在无任何基础设施的机器上重算全部断言，证明「记录的观测 + 冻结的案例 = 报告的判定」这条链是可复算的。
2. **它不证明当前代码在线通过**——判定的对象是**回放文件里记录的观测**，那些观测由某一次真实运行产生，绑定在下方「被测版本绑定」所列的版本上。代码改动之后未重跑，离线半仍会给出旧判定。

## 冻结范围

### 租户

| 角色 | 租户 | GLPI entity | 说明 |
| --- | --- | --- | --- |
| 验收租户 | globex `22222222-2222-4222-8222-222222222222` | 2 | 承载验收知识语料与工单 |
| 隔离对照 | acme `11111111-1111-4111-8111-111111111111` | 1 | **只读**，用于证明跨租户隔离 |

知识索引名天生带租户（`{prefix}-tenant-{tid}-children-{gen}`），无需另设前缀。

### 身份（4 个业务主体 + 1 个核验主体，缺一不可）

| 主体 | 角色 | entity | 群组 | 用途 |
| --- | --- | --- | --- | --- |
| `globex-analyst-g3` | analyst | [2] | **[3]** | 仅组 3 |
| `globex-analyst-g4` | analyst | [2] | **[4]** | 仅组 4 |
| `globex-analyst-nogroup` | analyst | [2] | **[]** | 无组权限，仅公共文档 |
| `globex-approver` | viewer/analyst/operator/approver | [2] | [3,4] | 审批员；**其组权限不得污染运行** |
| `servicemind-entitlement-reader` | **仅 realm 只读管理查询** | — | — | **续跑边界的核验身份**；不是业务主体，只回答「该主体现在持有什么」 |

GLPI entity 2 的组 id 实测为 `3 Network Team` / `4 Service Desk`（entity 1 才是 1/2）。硬编码 1/2 会让组过滤静默失效。此外 `globex-analyst-g4` 兼作 ACC-13 的图正对照见证主体。

核验主体经 `SERVICEMIND_KEYCLOAK_ADMIN_URL` / `_USERNAME` / `_PASSWORD` / `_REALM` 四个设置项装配（**值一律不打印**）；`security/auth.py:install_entitlement_verifier()` 在导入时安装。它**不是** realm-admin：只读查询一个主体、单次调用足以完成核验，realm-admin 超过所需。

### 知识夹具（v3.0 由 5 篇增至 6 篇）

| 文档 | `source_record_id` | 可见性 | 用途 |
| --- | --- | --- | --- |
| 当前有效手册 | `KB-GLOBEX-VPN-MFA-REBIND` | 公共（无组限制） | 正确处置依据 |
| 组 3 限制文档 | `KB-GLOBEX-VPN-MFA-G3` | 仅组 3 | 授权/未授权可见性对照 |
| 组 4 限制文档 | `KB-GLOBEX-VPN-MFA-G4` | 仅组 4 | 授权/未授权可见性对照 |
| 已废止手册 | `KB-GLOBEX-VPN-MFA-LEGACY` | 公共（`is_active=false`） | 版本冲突 |
| 症状相似不同根因 | `KB-GLOBEX-VPN-APP-REG` | 公共 | 判别力负例 |
| **VPN MFA 登记审计清单**（v3.0 新增） | `KB-GLOBEX-VPN-MFA-AUDIT` | 公共 | **体量**：使 ACC-06 的载荷真正超预算 |

**组限制文档必须真实存在**——不得以空 ACL 完成验收。ACL 声明见 `evaluation/acceptance/fixtures/globex/manifest.json`。

**v3.0 夹具扩张的理由与射程**（`vpn-mfa-enrolment-audit.md`，8277 字节，11 个父分块）。ACC-06 断言的是「某条被丢弃且写明了理由」，这只有在信封**确实装不下**时才成立。扩张前实测过一条更弱的路径并**否决**：只增加文档**篇数**无用——检索端的 `final_k=8` 把进入信封的候选数钉死，多出来的文档只会把既有行挤掉、不会让总量超预算（实测：新增约 2900 字节的文档后，分析信封只从 9771 涨到 9850）。因此扩张的是**体量**而非**篇数**。该文档写作时刻意**只陈述程序、不陈述因果也不给补救措施**，因此它永远不可能成为 ACC-03 禁止引用的机理，也不会成为 ACC-08 禁止给出的建议——语料扩张不得改变其他案例的判别力。扩张后实测：该文档的父分块进入分析信封，并且**挤出 2 条带非空理由的 `pruned` 行**。

### 工单夹具

globex entity 2 两张**结构同构**工单，事实固定为「密码认证成功 / 多因素认证失败 / 更换过手机」，仅报障人与设备型号不同。逐字正文见 `deploy/glpi/bootstrap_phase7_tickets.php` 中 `'ref' => 'globex-vpn-mfa-a'`（`:41`，正文 `:43-55`）与 `'ref' => 'globex-vpn-mfa-b'`（`:62`，正文 `:64-76`）——该正文是 ACC-03 三项 `required_facts` 断言的判定基准，**引用时以该文件为准，不引用本文件的转述**。

## 冻结判定规则

1. **预期决定预先规定**：每条案例在冻结时声明其输入**应当通过 / 弃答 / 补证据**中的哪一个。不接受「宽泛允许集合让所有结果都合格」。每条案例须含正例与负例，判别力来自**能被推翻**。
2. **覆盖表两列**：区分「**涉及**该模块」（链路经过）与「**验证了该模块的具体行为**」（存在对该模块可观察行为的断言）。不得输出「N/N 已覆盖」这类预设结论。
3. **四个独立探针**不得由核心闭环代为证明：浏览器前端（ACC-14）、MCP（ACC-15）、outbox 与恢复（ACC-16）、索引生命周期（ACC-17）。
4. **BLOCKED 语义**：见「术语纪律」。健康环境下超过冻结时限 → 判**超时失败**，不得无限归为数据不足。
5. **gate 退出码**：`0` PASS｜`1` 有 blocking 断言 FAIL｜`2` 观测不足｜`3` 配置/覆盖错误（案例校验失败、报告与案例 checksum 不一致、**或证据横跨多个源码版本**——见第 9 条）。
6. **离线与在线分离**：见「术语纪律」。
7. **可自动断言的项不得列为人工**：正文一致性、`pattern_key` 相等性、manifest 裁剪原因、引用集合、回读条数与内容、终态、时延。人只做抽样复核与失败分诊，不承担判定。
8. **判据不得靠枚举措辞**：`RequiredFact` 用概念组（`all_of`）判定**命题**而非字符串；模型换一种说法不得使断言失效，断言也不得因措辞巧合而通过。
9. **证据同源性（v3.3 新增，四个 gate 共同的前置）**：一批观测若横跨一个以上的源码版本，其结论「只在下述意义上是关于平台的——每一条观测各自是关于一个平台」。因此**同源性检查先于任何判定**，且 **`--force` 不能越过它**（`--force` 的语义是「替换一份摘要过期的报告」，不是「让不可用的证据变得可用」）。同源性检查**恒定执行**；「是否就是当前版本」这一半需要 `--expect-revision` 提供，离线裸检出无法自行推断。**空批次不在此列**：它仍走「观测不足」（退出 2），因为「没跑」与「跑出来的东西不可用」要把操作者引向不同的地方。

## 被测版本绑定

本次全部证据绑定于以下版本。逐案例的原始证据（run_id、timeline、审批记录原文、GLPI 回读正文、citation 列表、`selection_manifest` 的 selected/pruned 条目与 reason）见机器报告「六、逐案例记录」。

| 维度 | 实际值 |
| --- | --- |
| 源码版本 | `cf08ac8a7b731054e492ed81ba5f3164dc381863`（**v3.3 的全部改动都在该提交之上、未提交**；**2026-10-01 冻结时这些改动提交为 `5fea7ca60b4ca71df5495c80ce38751e01f4647f`，内容未改**）。v3.2 记录的 `7a6e675` 在其之前两个提交；仓库作者此后提交了 `688dd90` 与 `cf08ac8`（后者即「暂时移除 phase7-offline-gates 任务」） |
| 未提交改动（v3.4 实测） | **206 个**（202 个 `M` + 4 个 `??`，`git status --porcelain` 实时计数）。**这个数字本身是本文件最好的例证**：v3.3 定稿时是 56，v3.4 结束时是 206，而两次之间**代码侧的净变化只有 17 个文件**（4 个新增 + 13 个改动，即 `src/`+`scripts/`+`tests/` 三栏，见下）——**其余约 150 个的增量全部是本轮录制的证据**（安全 72、负载 61、验收 28、质量 14、报告 12）。逐目录：`evaluation/security/` **72**、`evaluation/load/` **61**、`evaluation/acceptance/` **28**、`evaluation/quality/` **14**、`evaluation/reports/` **12**、`src/servicemind/` **6**（4 改 + 2 新增未跟踪）、`scripts/` **6**、`tests/servicemind/` **5**（3 改 + 2 新增未跟踪）、`evaluation/routing/` **1**（金标语料）、`docs/` **1**（本文件）。**所以「未提交改动 206 个文件」这句话几乎不包含关于平台的信息**——它主要在数本轮录了多少证据。**这是 R5 之前版本号所依赖的那个量**，也是它必然失效的原因 |
| 未提交改动（v3.3 留档） | **批次运行时刻 55 个文件；本文件定稿后 56 个**（`git status --porcelain` 实时计数，两次都数过）。**+1 就是本文件自身**——驱动批次跑完之后才写 v3.3 各节，此前它在工作树里与 HEAD 逐字相同（HEAD 上仍是 v3.2）。逐目录（定稿后）：`evaluation/acceptance/replays/` **28**、`evaluation/reports/` **12**、`scripts/` **4**（四个 gate）、`tests/servicemind/` **3**、`evaluation/quality/replays/` **3**、`src/servicemind/orchestration/` **2**、`src/servicemind/evaluation/` **1**（新增未跟踪 `revisions.py`）、`src/servicemind/` **1**（`api.py`）、`evaluation/routing/` **1**（金标语料 +7 例）、`docs/` **1**（本文件）。**其中 2 个为新增未跟踪文件**（`src/servicemind/evaluation/revisions.py`、`tests/servicemind/test_phase7_gate_revisions.py`）。工作树是本阶段全部工作的**累积**，未回退任何既有改动 |
| 驱动程序记录的部署版本（v3.3 观测，**原样留档**） | `cf08ac8a7b731054e492ed81ba5f3164dc381863+dirty(26 files)`（`verify_phase7_acceptance_live.py` 横幅逐字）。**本行在 v3.3 曾把 26 解释成「驱动自己的计数口径，与 `git status` 的 55 不是同一个量」——那个解释是错的，v3.4 更正如下。** 驱动从来没有第二个口径：它数的就是 `git status --porcelain` 的行数。26 与 55 的差别只是**两次取数的时刻不同**——横幅在批次开始时打印（此时只有 26 个文件被改动），本文件在批次结束后定稿（此时 28 份新回放与 12 份新报告都已写回工作树，于是变成 55）。**这正是 R5 的病灶**：版本号是「此刻工作树里有多少文件被动过」的函数，而记录这个动作本身就在改这个数。当时无法解释这个差额，于是把它写成了另一个口径——**一个解释不通的现象被记成了一条不存在的规则**。v3.4 起版本号改为源码内容指纹，不再有这个差额 |
| 驱动程序记录的部署版本（v3.4 起） | `cf08ac8a7b731054e492ed81ba5f3164dc381863+patch(0ce86f5d19bc)`——**四个驱动对本轮工作树的返回值逐字相同**（实测：acceptance / security / quality / load 四份实现各算一次，结果一样）。`patch(0ce86f5d19bc)` 是对工作树中**17 个改动的源码/脚本/测试文件**的路径与内容取的摘要，**与 `evaluation/`、`docs/` 下 188 个证据与文档产物无关**：写一个新回放进去，这个字符串不动 |
| **提交后：同一棵树的第二个名字**（2026-10-01 冻结点） | **`5fea7ca60b4ca71df5495c80ce38751e01f4647f`**——本轮全部改动已提交，工作树清空，故指纹消失、版本号退化为纯 commit。**这不是版本变更，是同一棵源码树换了一个可解析的名字**：`git diff --name-only cf08ac8 5fea7ca` 共 206 个文件，其中**恰好 17 个**落在 `src/`+`scripts/`+`tests/`——与上一条 `patch(0ce86f5d19bc)` 所摘要的路径集合**一致**，其余 189 个全是本轮录制的证据与本文件。所以**上一条与本条指的是同一份代码**。**这件事本身就是 R5 最好的端到端证明**：在 R5 之前，「提交」这个动作会把它变成**第三个** `dirty(N)`；现在它只是让一个匿名指纹变成了一个别人 `git checkout` 得到的 commit |
| **四份语料各自实际记录的版本**（2026-10-01 逐条读出，**与上一条的「驱动返回值」不是一回事**） | 上一条说的是**驱动现在会写什么**，本条说的是**语料里已经躺着什么**——两者不同，这正是三门拒判的原因。逐条实测：**验收 28 份全部是 `cf08ac8…+dirty(26 files)`**（**单一版本，故该门取 0**）；**安全 72 份 = 70×`cf08ac8…+dirty(130 files)` + 2×`cf08ac8…+patch(0ce86f5d19bc)`**（70 旧格式 + 2 新格式，即本轮 `--run-docker` 补录的那两条）；**负载 120 份 = 60×`cf08ac8…+dirty(69 files)` + 60×`688dd90…+dirty(155 files)`**；**质量 200 份横跨 6 个版本**（148×`7a6e675…+dirty(121)`、37×`+dirty(120)`、11×`cf08ac8…+dirty(56)`、2×`688dd90…+dirty(131)`、1×`+dirty(8)`、1×`+dirty(69)`）。**因此「旧证据」的准确含义是按语料分级的，不是一句「都是旧版本」**：验收是**同源但陈旧**，其余三门是**不同源**。**读法上的后果（实测）**：带 `--expect-revision 5fea7ca60b4ca71df5495c80ce38751e01f4647f` 跑验收 `--replay-only` → **退出 3**，报文逐字为「the observations were taken against `cf08ac8…+dirty(26 files)` but this run expects `5fea7ca…`」；**不带该参数时只查同源性，仍取 0**（两条都已实跑确认）。拒绝是设计行为，不是回归 |
| 受测端点 | `http://127.0.0.1:18080` |
| API 运行方式 | systemd user unit `servicemind-api`；`ExecStart=/home/shihongye/data1/servicemind/.venv/bin/servicemind-api`，`Restart=on-failure`，`ActiveState=active` |
| **服务本批次起始进程** | **CST `23:08:37`** 启动（`ActiveEnterTimestamp=Wed 2026-09-30 23:08:37 CST`，`INFO: Started server process [1656207]`）。这次重启是**本轮改动的部署动作**：`src/` 有改动必须先重启才能让新代码生效。**ACC-01…ACC-10a 跑在这个进程上** |
| 批次内的重启（4 次，2 个场景） | CST `23:15:20`（PID 1703482）／`23:15:35`（1705241）／`23:22:48`（1755443）／`23:23:04`（1757345）。**四次都发生在案例自身的场景里**：ACC-10a 与 ACC-22 要验证「身份服务不可达时暂停、恢复后同一个决定仍可应用」，驱动程序据此改写 `SERVICEMIND_KEYCLOAK_ADMIN_URL` 并重启 unit，每个场景各重启两次（改为不可达、再改回）。回放里逐字可查：两条案例各有 `verifier-outage-on` / `verifier-outage-off` 两个步骤，detail 为 `outage-on: SERVICEMIND_KEYCLOAK_ADMIN_URL=http://127.0.0.1:1 on servicemind-api, then restarted` 与 `outage-off: … unset on servicemind-api, then restarted`。**它们不是杂散重启**——但必须如实说明：**ACC-11 之后的部分案例跑在重启后的进程上**。四次重启前后加载的是**同一棵工作树**（重启不改代码），故版本绑定不受影响；受影响的只是进程实例标识 |
| unit 当前 `ActiveEnterTimestamp` | `Wed 2026-09-30 23:23:04 CST`（PID 1757345）——**这是 ACC-22 自己恢复身份服务时的重启**，不代表本批次的起始进程；以「服务本批次起始进程」一行为准 |
| 健康检查 | `GET /health` → `{"status":"ok"}`（`200`） |
| 租户 | `22222222-2222-4222-8222-222222222222` |
| 模型 | DeepSeek（经平台 `model_gateway` 路由；模型调用逐条记录在 `model_invocations`） |
| 核验器（entitlement verifier） | **已配置且可达**（`True`）。装配读的是**本进程自己的注册表**，且在 `import servicemind.security.auth` **之后**读取——只导入注册表模块会读到一个永远为空的槽位，从而把「已配置的部署」误报成未配置 |
| 容器清单 | `servicemind-frontend`、`servicemind-glpi-glpi-1`、`-database-1`(mariadb:11.8)、`-keycloak-1`(26.7.3)、`-keycloak-database-1`、`-neo4j-1`(5.26)、`-opa-1`(1.20.2)、`-opensearch-1`(3.8.0)、`-rag-embedding-1`、`-rag-reranker-1`、`-redis-1`(8.2)、`-servicemind-postgres-1`(16)，全部 `Up`/`healthy`（`environment` 阶段逐行原文见 `phase7_pipeline_evidence.json`） |
| 配置摘要 | 仅记录**变量名**，值一律脱敏（共 **23** 个密钥变量名被 `collect_phase7_pipeline_evidence.py` 的值级擦除器登记，见 `phase7_pipeline_evidence.json` 的 `secret_keys_scrubbed`）；本文件与机器报告均不含任何密钥值。**与本轮判定直接相关的三项非密钥配置**：`SERVICEMIND_CONTEXT_MAX_INPUT_TOKENS=12000`（usable = 12000 − 256 − 1024 = **10720**）、`SERVICEMIND_CONTEXT_EVIDENCE_TOKEN_CAP=5000`（仅对分析代理生效）、检索侧 `final_k = 8`（首轮）/ `10`（`retrieval_round > 0`，`agents/knowledge.py:90`） |
| 观测时刻 | 回放写入区间 CST `23:11:35` – `23:24:17`（ACC-01…ACC-23，**28 份回放出自同一次驱动运行**，总耗时约 12 分 42 秒） |
| 案例清单摘要 `cases_digest` | `63a3a71fd0a6caedb9041dffa0094cabe80a84afe91bdbf11f13c6334d67356c` |
| 观测摘要 `observation_digest` | `ba2829a496bbb3ee402a5caecec2012f1bfce6984bc94ad76eda735f05888ee6`（v3.2 的 `619f01b3…` 已被本次取代；这是**新一次驱动运行的观测**，不是重新渲染旧观测） |
| 报告生成时间 | `2026-09-30T15:30:43.466133Z`（`gate --check --replay-only --format json --report …` 生成；**退出码 0，无需 `--force`**） |
| 流水线证据生成时间 | `2026-09-30T15:30:20.290431+00:00`（`repo_revision=cf08ac8a…`，六个阶段全部 exit 0） |

**`observation_digest` 变了，`cases_digest` 也变了——这两件事要分开看，且两个原因都不是「本轮改了期望」。**

- **`cases_digest` 变是因为案例清单在 v3.2 之后被改过并已提交**（v3.2 记录 `c6dfec09…`，本次 `63a3a71f…`）。**本轮一个字符也没有改 `cases.v1.json`**：`git status --porcelain` 里它不在改动列表中。为免只凭这一点下结论，又实测了一次——**按当前代码计算该文件的 `cases_digest`，在 `688dd90` 与在当前工作树上都是 `63a3a71f…`**，而 `7a6e675` 是 `ea9d5489…`。也就是说：变化发生在**已提交的两次仓库变更**（`688dd90` 对 `cases.v1.json` 的 +60/−22）里，且当前工作树与已提交内容**逐字一致**。
- **`observation_digest` 变是因为本轮真的重跑了 28 个案例**（28 份回放全部被新观测覆写，从 v3.2 的 `619f01b3…` 变为 `ba2829a4…`）。

这正是「离线回放只证明判定器可重放」那句话要区分的东西——**本轮同时拿到了新观测与新判定，两者都绑定在 `cf08ac8` 上**。

`cases_digest` 的用途是让「案例改了而报告没重跑」这件事无法悄悄发生：案例改一个字符而回放未更新，下一次 gate 即**退出 3**。该反证在**当前摘要下重新实测过**（v1.0/v2.0/v3.0/v3.1 记录的旧摘要均不再具有约束力，不再引用）：把 `cases.v1.json` 中某条案例的标题改一个字符后重跑，gate 输出 `configuration_error`（**回放所记录的案例摘要已不是当前这份**）、**退出码 3**；逐字节还原（`cmp` → `BYTE_IDENTICAL`）后，gate 恢复本轮的结论 **退出码 0**，且两次运行的 `observation_digest` 逐字相同。**逐字输出与命令见「验收证据」四。**

### 前置条件与回归（`scripts/collect_phase7_pipeline_evidence.py`，全部为实际执行）

| 阶段 | 目的 | 退出码 | 耗时 s |
| --- | --- | --- | --- |
| `environment` | 冻结被测环境：服务清单、systemd unit、健康检查、源码版本与未提交改动 | 0 | 0.2 |
| `identity` | 身份播种结果核对：四个验收主体存在、凭据可用、无声明漂移（只读） | 0 | 0.9 |
| `identity-drift` | 既有记忆审核主体的双向漂移核对（只读，不创建身份） | 0 | 0.6 |
| `fixtures` | 知识夹具与工单夹具的幂等核对（只读；未播种时应非零退出） | 0 | 16.6 |
| `index-lifecycle` | 真 OpenSearch 上的索引蓝绿生命周期探针 | 0 | 15.6 |
| `regression` | 全仓回归：`uv run pytest tests/servicemind tests/service -q` | 0 | 88.4 |

**六个阶段全部 exit 0**。回归实测：**980 passed, 10 skipped, 70 warnings**（**0 failed**，耗时 66.07s，采集于 `2026-09-30T15:30:20Z`），静态门禁 `ruff format --check` / `ruff check`（All checks passed）/ `pyrefly check`（**0 errors**，18 suppressed）与 `scripts/audit_project_structure.py --check`（输出 `PASS PASS`，exit 0）全部通过。

> **静态门禁里有一处必须说明的改动**：`scripts/audit_project_structure.py --check` 本轮**先失败后通过**，而失败是**真信号**、通过是**真的修复**，不是把门禁放松。成因：R3 让 `src/servicemind/api.py` 新增了一处 `from servicemind.foundation.errors import bounded_error_text`，于是结构审计的包依赖图在 `_root` 一行多出 `foundation`，磁盘上的报告工件与重算结果不再逐字节一致（`artifacts_match` 为假，输出逐字为 `FAIL PASS`——**状态是 PASS，不一致的是工件**）。处置是**重新生成报告工件**（`uv run python scripts/audit_project_structure.py`，退出 0，随后 `--check` 输出 `PASS PASS`），而不是修改门禁。**依赖方向本身是合法的**：`foundation` 是最底层模块，`api.py` 早已依赖 `domain` 等层，审计的导入方向检查（状态 `PASS`）并未被违反。

> 计数沿革必须记明，且**不对差额做逐条归因**：v1.0 `688 passed, 5 skipped`；v2.0 `714 passed, 6 skipped`；v3.0 `729 passed, 6 skipped`；v3.1 `733 passed, 6 skipped`；v3.2 `738 passed, 6 skipped`；**v3.3 `980 passed, 10 skipped`**。**六次都是 0 failed，六次都无 `-k` 过滤**。v3.2 → v3.3 的 **+242 / +4 skipped** 幅度远大于以往各轮，**原因必须写清而不能含糊**：它主要来自 `cf08ac8` 与 `688dd90` 两个**已提交**的仓库变更（v3.2 与 v3.3 之间仓库作者提交过内容），**不是**本轮改动造成的。本轮新增的测试只有 **20 条**（`tests/servicemind/test_phase7_gate_revisions.py`：9 条共享规则单测 + 8 条逐门检查 + 1 条逐门 `main` 端到端，其中两条为参数化展开）+ **2 处断言**（`test_supervisor_runtime.py`：失败运行必须把原因持久化到 run 行；成功运行必须**不**留下原因）+ **1 条**（`test_phase3_router_planner.py`：`incident` 诊断路径的边界）。**本文件不声称该差额恰好由这些构成**——除非做过逐测试名对照。

## 本阶段实际改动清单

以下改动**叠加在既有的未提交改动之上**（v3.3 定稿时 `git status --porcelain` 为 **56 个文件**——批次运行时刻为 55，差额是本文件自身；其中 **2 个为新增未跟踪文件**、**无删除**），未回退任何既有改动。工作树是本阶段全部工作的**累积**；下表按「本轮确认并复核过的根因」组织。

**关于文件数的沿革**：v3.0 执行时工作树含 108 个文件（大量为新增未跟踪文件）。随后由**仓库作者（Shark6438）**将其提交为 `7a6e6758d2e4966771546e57482e8b0a5d8b19dc`，这些文件因此离开未跟踪集合。v3.1 结束时为 38 个修改文件；v3.2 结束时为 46 个；**v3.3 批次运行时刻为 55 个、定稿后为 56 个**（批次时为 +9：`tests/servicemind/test_phase7_gate_revisions.py` 与 `src/servicemind/evaluation/revisions.py` 两个**新增**文件，其余为四个 gate、`api.py`、两个 orchestration 文件、`evaluation/routing/routing.jsonl`、以及被新观测覆写的 28 份验收回放与 12 份报告）。**v3.1、v3.2 与 v3.3 均未做任何提交**。

### v3.4 的根因修复（R4 / R5）

两条都在**没有支付重录成本之前**被探针找出来。这正是本轮的执行顺序：**先用便宜探针把「重录之后会不会还是红的」问清楚，再决定要不要花那 ¥40。**

| # | 文件 | 改动 | 根因（可复现） |
| --- | --- | --- | --- |
| **R4** | `src/servicemind/orchestration/dynamic_planner.py`（新增 `_next_task_id` / `_has_ancestor` / `_complete_evidence_pipeline`，在 `compile_proposal` 里于 `previous is None` 时调用；`_planner_prompt` 的措辞改写）、`tests/servicemind/test_dynamic_planner.py`（3 条「应当拒绝」的测试替换为 1 条参数化补全测试 + 2 条边界测试） | **把「证据流水线必须完整」从「请求模型做对、做不对就拒绝」改成「由策略补全」**：只要提案里有 Data/Knowledge 任务，就补上缺失的 Analysis（并把它接到**全部**证据任务上）、补上缺失的 Reviewer（接到 Analysis），再把 Action 接到 Reviewer 之后；已有 Analysis / Reviewer 时不重复添加，只补齐依赖边。提示词同步由「Build a minimal DAG」改为「Build the smallest **valid** DAG」，并明文写出「含 Data 或 Knowledge 的计划必须恰好含一个 Analysis 与一个 Reviewer」。**fail-closed 的检查保留为补全后的后置条件**，无法补全的提案仍然被拒 | **语料覆盖不到的问句会让规划器产出残缺计划。** 活体实测（2026-10-01，质量案例 `Q-200`「公司对收受供应商礼品有什么政策？」）：8 次采样里 **6 次**产出的计划没有 Analysis，或 Reviewer 没有 Analysis 可审（`[knowledge]`、`[knowledge, reviewer]`、`[knowledge, data]`）。规划器只有 **2 次**尝试，两次都不过就以 `critical_error` 结束运行——**用户拿不到任何答案，而且大多数时候拿不到**。这不是模型的固定 bug（同一提示词在另外 2 次里给出了完整流水线），而是**「建最小的 DAG」与「证据必须经过分析与评审」这两条指令没有共同解**，模型每次自己挑一边。**既然政策只有一种正确补全，就应该施加它，而不是请求它然后拒绝它。** 历史佐证：该终止码在本部署上此前已触发 **48 次** |
| **R5** | 新增 `src/servicemind/evaluation/source_revision.py`；`scripts/verify_phase7_acceptance_live.py`（`source_revision` 改为委托）、`scripts/verify_phase7_security.py`（同一份拷贝删除、改为委托）、四个 gate 的帮助文案；新增 `tests/servicemind/test_source_revision.py`（13 条） | 版本号由 `<sha>+dirty(N files)`（`git status --porcelain` 的**行数**）改为 `<sha>` 或 `<sha>+patch(<12 hex>)`——后者是对**工作树中改动过的源码路径及其内容**取的 SHA-256 摘要。排除 `evaluation/`（驱动自己写的证据与报告）与 `docs/`（关于这次运行的叙述），**包含** `tests/` 与 `scripts/`，使一个版本号同时覆盖「平台」与「报告拿去配对的测试套件」。四端共用一份实现（质量与负载驱动本就通过动态加载 acceptance 驱动来复用它） | **记录这个动作本身改变了被记录的版本号。** 驱动把证据写进工作树，而版本号数的正是工作树里被改动过的文件数，于是**每次重录都会产生一个新的版本号**。实测同一份未改动的源码 `cf08ac8` 被记成了**五个**版本：`dirty(8)`、`dirty(26)`、`dirty(56)`、`dirty(69)`、`dirty(130)`；而**更早**的提交 `688dd90` 反而被记成 `dirty(155)`，因为评测它的那批跑得更晚。这个字符串**两个方向同时失效**：它把同一个平台的不同批次判成不同版本（于是 `--expect-revision` 跨语料永远无法满足，四门 gate 的报错文案「re-run the batch against a single deployment」**根本做不到**），又无法区分两棵改动文件数恰好相同的**不同**源码树（可能假通过）。**这是 G1 的前置条件失效**：G1 把 `deployed_revision` 当成唯一的前提，而这个前提本身不是一个标识符 |

**R5 的修法为什么不是「数得更准」而是「改个量」**：把行数换成「只数源码文件的行数」也能让 `evaluation/` 的写入不再影响结果，但它仍然只是一个**计数**——两棵内容不同、改动文件数相同的源码树会撞成同一个版本号，而那正是 G1 最需要拒绝的情形。真正的需求是**给「进程加载的那份代码」一个名字**，所以取的是内容指纹。

**一处必须说明的副作用**：`tests/` 被计入意味着**改一条测试就会让版本号变一次**，需要重录语料。这是**有意选的**方向——一个版本号同时命名平台与它配对的测试套件，报告里的「全仓回归」与「平台判定」就不会各自指向不同的修订；代价是保守的（多录一次），而且在一次记录活动中不会发生（记录期间不改源码）。排除清单用的是「不是源码的路径」而不是「是源码的路径」，也是同一考虑：**新出现的顶层源码目录会立刻被覆盖**，而白名单会继续报一个从没提到过它的版本号。

四条**性质各不相同**，处置也各不相同。**其中 R2 没有代码 diff**——把它列在「修复」表里会误导，故单列一栏说明「查了、没复现、没改」。

| # | 文件 | 改动 | 根因（可复现） |
| --- | --- | --- | --- |
| **R1** | `src/servicemind/orchestration/router.py`（+79 行）、`tests/servicemind/test_phase3_router_planner.py`（+42 行）、`evaluation/routing/routing.jsonl`（+7 例） | 新增 `incident` 诊断路径：故障诊断类问句不再走 `simple_knowledge_query` 直答，而是进入需要分析能力的路径。金标语料由 100 例扩到 **107 例**，逐例断言路由结果 | 路由器把「用户报告一个故障现象并要一个根因」判成了「查一条知识」。**这条缺陷不报错、不掉断言**：那个更浅的答案本身是通顺的，只是没有读工单、没有比对知识、没有给根因。判别力来自**同一批次里两条路径都被走到**：ACC-01（读工单、要根因）落 `complex_workflow`/`analysis_or_action_required`，ACC-02（问「手册怎么说」）落 `simple_knowledge_query`/`knowledge_lookup_only` |
| **R3** | `src/servicemind/orchestration/supervisor_workflow.py`（`finalize_node`、`supervisor` 决策分支、`revise_node`）、`src/servicemind/api.py`（三条续跑路径 + 一处 `foundation.errors` 导入）、`tests/servicemind/test_supervisor_runtime.py` | ①`finalize_node` 在失败终态上把 `<termination_code>: <reason>` 写进 `agent_runs.error`，`reason` 取自控制流已记录的 errors（取**最后一条非空**）；②`api.py` 的 webhook / 审批 / 复核续跑三条路径把 `error=type(exc).__name__` 改为 `error=bounded_error_text(exc)`；③`supervisor` 分支修掉一个**潜伏类型 bug**——它写的是 `type(failure).__name__`，而该分支里的 `failure` 实际是**字符串**，于是记录出的 `error_type` 恒为 `"str"`；④`revise_node` 的 `critical_error` 分支此前不记录任何原因，现在记录 `PlanRevisionRejected` 与理由 | 一个失败的 run 在 `agent_runs` 行上是 `status='failed'` 而 `error=NULL`——**原因只存在于 `run_events` 流里**，而事件流不是「一列失败的运行」这个视图会读的东西。**回溯佐证（真实数据，非构造）**：排查负载 gate 时读到的旧 `tier-10` 失败行正是 `failed / error=NULL`，而 `run_events` 里原因写得很清楚（`402 Insufficient Balance`、`429 … concurrency is 5`）。第三个要点（`"str"`）尤其值得记：它记录出的类型名**什么也没说**，读起来像账本坏了而不是策略拒绝 |
| **G1** | 新增 `src/servicemind/evaluation/revisions.py`；`scripts/gate_phase7_{quality,acceptance,security,load}.py` 各增一个 `check_*_are_one_revision` 与 `--expect-revision`；新增 `tests/servicemind/test_phase7_gate_revisions.py`（20 条） | 共享规则 `revision_problems(revisions, expect=)` 判定两件**互相独立**的事：**同源性**（观测之间是否只有一个版本）与**时新性**（是否就是 `--expect-revision` 指定的那个）。同源性**总是**检查（它不需要外部输入），时新性由参数提供（离线 checkout 无从得知当前版本）。**四门各自 `main` 的取码顺序**：同源性失败 → 退出 3 拒判，且 **`--force` 到不了这里**（`--force` 的语义是「重渲一份摘要陈旧的报告」，它不该能把一份横跨四个平台的语料变成一份平台判定）；时新性失败 → 同样退出 3，但复现方式不同（改 `--expect-revision` 而语料不动）。空批次**不算**同源性问题——那是各门自己的「观测不足」规则，返回问题会把它从退出 2 变成退出 3，把两种分诊方向混成一种 | **四个 gate 每一个都读 `deployed_revision`，没有一个把它当前提。** 实测：`evaluation/quality/replays` 的 200 条观测横跨 **4 个源码版本**（`7a6e675+dirty(120)` ×38、`7a6e675+dirty(121)` ×158、`cf08ac8+dirty(8)` ×2、`688dd90+dirty(131)` ×2），批头 `_batch.json` 还写着第 5 种组合（`concurrency=1`、`cf08ac8+dirty(8)`）。目录里共 201 个 `.json`，其中 1 个是批头、装载器按 `_` 前缀跳过，故**观测数是 200**（本行此前写的 201 是数了文件、没有扣掉批头，已更正）。而 gate 对这份语料报 **PASS，退出 0**。安全与负载的 grader 甚至**已经算出了这个集合**、在报告里渲染成 `被测版本`——**它们算对了，只是没有一门让退出码取决于它**。一个横跨四个平台的判定是关于其中任何一个的判定吗？这是本轮最有普遍性的一条 |
| **R2** | **无**（未改任何生产代码） | 三次**活体**探针：真实模型、真实工具、真实 GLPI 读，每次都走到 `waiting_approval` 并给出结构正确的 `append_ticket_followup` | 疑点是「评审器判定不稳定 + 旁支 claim 拦掉整个动作」。**3/3 未复现。** 因此**不改评审器的安全语义**——为了让报告好看去改一条复现不出来的安全路径，是拿一个真实缺陷换一个虚假的通过。该疑点记为**未复现的观察**：既不下「已修复」，也不删记录 |

### v3.2 的根因修复（v3.1 三条遗留缺陷）

| # | 文件 | 改动 | 根因（可复现） |
| --- | --- | --- | --- |
| **D3** | `src/servicemind/orchestration/state.py`（`Phase3State.analysis_evidence_ids`）、`supervisor_workflow.py`（`analysis_node`，`:1153`）、`phase5_governance.py`（`:497-526`） | 分析节点在 `build_context` 返回后**把信封实际交付的证据 id 写进 state**；评审器构建上下文时，其证据集**以这一份为准**（不再使用裁剪前的全集），并把分析**实际引用过**的 id 并入（`delivered = frozenset(recorded) ∪ cited`）。`recorded is None` 时**退回全集**——那是「根本没有交付决策可镜像」（stub 分析直接读联合集，评审也读同一份），而不是「没有证据」 | 评审器的证据集原为**分析所见集合 ∪ 分析被裁掉的集合**。ACC-03 的 run `96435a80` 逐 id 实测：分析信封选中 **9** 条证据、裁剪 **2** 条（`ev-f380cb32f4a5b880` = 重绑手册分块、`ev-ff24bc453152bf9d`）；评审器选中 **11** 条、裁剪 **0** 条，且 **11 = 9 ∪ 2**（集合相等）。即**评审器读到了重绑手册，而分析代理没读到**，然后按它去判「这个分析据什么说的」——那个判定不是关于这个分析的陈述 |
| **D16-产品** | `src/servicemind/agents/analysis.py`（`:127` 起） | 分析系统提示明文规定：排除某候选的文档**不构成**所命名根因的证据；根因 claim 的 `evidence_refs` 必须承载**蕴含**该根因的材料，被排除的文档**不得出现在其中**；排除理由写进 claim 的 `statement` 或 `assumptions`，使读者**仅凭 refs** 就能看出根因据什么而立 | v3.1 实测：模型用诱饵文档**排除**另一种故障（「故障发生在提示之前才是策略类故障」），这是**正确**的判别；但它有时把这条判别依据**一并挂进根因 claim 的 `evidence_refs`**——而该文档说的正是相反的结论，于是那条 claim **不由它所引的材料蕴含**。判定器判的是 refs，规则就必须写在**分析代理读得到的提示词**里 |
| **D16-判定器** | `src/servicemind/evaluation/acceptance.py`（`RequiredFact.must_cite`，`:123`）、`acceptance_grader.py`（`:786-799`）、`evaluation/acceptance/cases.v1.json`（ACC-03 的 `root-cause` fact 加 `"must_cite": ["KB-GLOBEX-VPN-MFA-REBIND"]`） | `RequiredFact` 新增**正向**断言 `must_cite`：取所有匹配 claim 的 `evidence_refs` 的**并集**，其中解析出的 `source_record_id` 必须**包含**列出的每一项；缺失即判 FAIL 并列出「已引用了哪些」 | 只有 `must_not_cite`（负向）时，**一个什么都不引用的 analysis 会被判为合规**——负向断言与空集天然相容。v3.1 的 ACC-03 因此存在一条**空满足路径**：清空根因 claim 的 refs 就能过。`must_cite` 堵的是这条路径，它**不是**「替代」`must_not_cite`，两者是同一 fact 上的两个方向 |
| **D17** | `scripts/verify_phase7_acceptance_live.py`（`context_pruning_producer`，`:1349`；分派 `:1980`）、`evaluation/acceptance/cases.v1.json`（ACC-06） | 新增一个**确定性探针步骤**：固定输入 `max_input_tokens=2000 / system_reserve=0 / output_reserve=0`（usable = 2000），1 条 required POLICY 控制行 + 6 条各 1802 字符（实测 **799 token**）的 EVIDENCE 行，**各行的内容互不相同**（前缀 `Runbook {index}`，否则会被 `exact_duplicate` 先去重，测到的就是去重规则而不是预算规则）。断言 = 「至少一条证据行被选中 **且** 至少一条被丢弃并写明非空理由」。判定、逐条 token 数与逐条 reason 全部写进步骤 detail，读者可据此复算。ACC-06 的三条断言改为：实跑清单非空、探针 pass、裁剪后仍到达成功终态 | v3.1 实测：该案例**首轮信封只用 10021/10720**，余额 699，**没有该丢的东西**；重复 5 次**全部零裁剪**（v3.0 的 2 条裁剪来自修订轮，而修订轮是否发生取决于评审器这一次是否放行）。**这是验收套件自身的输入前提不成立**。改法是让断言**不依赖随机事件**，不是削弱它 |

### v3.0 本轮复核的根因修复

| # | 文件 | 改动 | 根因（可复现） |
| --- | --- | --- | --- |
| **A** | `src/servicemind/context/builder.py` | 恢复/保留**第二趟回收**：第一趟把被按来源上限拒绝的行记为 `pruned / source_token_cap_exceeded` 并放入 `cap_deferred`；第二趟按**广度优先序**把它们重新接纳为 `selected / reclaimed_from_source_cap` | 上限在信封仍有空间时生效，且被拒行**再也回不来**——信封把该给出去的空间白扔掉。修复前 ACC-06 的信封只用了 6412/10720 却报告「被来源上限拒绝」。**修复后实测**（ACC-03 的 run `d13f7949-5b05-47f2-9a48-a0f7a032e659`）：正确手册 `KB-GLOBEX-VPN-MFA-REBIND` 的分块 `ev-f380cb32f4a5b880` 由 `pruned` 变为 `reclaimed_from_source_cap` 进入分析信封——**这是 ACC-03 由 FAIL 转 PASS 的直接机制** |
| **C** | `src/servicemind/orchestration/phase5_governance.py`（`:422-447`）、`src/servicemind/evaluation/memory.py` | 分析代理信封**不再携带 `output-schema` 项**；评估侧 `_delivery_envelope` 同步移除，使度量模型与生产信封保持一致 | 分析代理的**系统提示已逐字携带** `AnalysisResult.model_json_schema()`（`agents/analysis.py`），信封再放一份是**同一份东西的第二次发送**：分析 2985 token = 信封的 28%，评审 1586 token = 15%，且评审那份携带的是 `ReviewResult` —— **一个从未向任何模型请求过的 schema**。理由逐字写在 `phase5_governance.py:422-447` |
| **6** | `src/servicemind/foundation/errors.py`（新增）、`agents/analysis.py`、`orchestration/supervisor_workflow.py`（共 3 处调用点） | 新增 `bounded_error_text(error, limit=1000)`：超长时**保留两端**而非截掉头端，中间插入 `…[N characters elided]…` | 原实现对错误文本做**头端裁剪**。`OutputParserException` 的文本以**整段模型补全**开头、把 schema 违规写在最后，于是裁剪恰好把**唯一有诊断价值的尾部**丢掉。ACC-07（2026-09-23）实测：分析补全在 1000 字符处被拦腰截断，运行转 `degraded`、评审据此升级、案例失败，而**导致这一切的校验错误没有留在任何地方** |
| **7** | `src/servicemind/model_gateway/gateway.py` | 退避按**失败类型**分档：`MODEL_RATE_LIMITED` 走 1.0s 起、8.0s 封顶的指数退避（优先遵守供应商的 `Retry-After`）；传输类抖动仍走 0.1s 起、0.5s 封顶；并把等待**钳制在调用自身剩余的 `timeout_seconds` 内** | 原实现对**所有**可重试错误用同一条 `min(0.1 * 2**retry, 0.5)`，于是 429 **在被拒后 100 毫秒**就被重放——**还在同一个限流窗口里**。实测（2026-09-23 一天的真实验收流量）：9 条 `MODEL_RATE_LIMITED` 全部 `attempts=2`，即**重试一次都没成功过**，且 9 条运行全部终止于 `waiting_review`。证据链：分析调用 → 429 → 无效重试 → 无配置回退 → `DEGRADED_ANALYSIS` → 评审升级 |
| **1/3/4** | `src/servicemind/orchestration/supervisor_workflow.py` | ①续跑节点的双重拒绝不再清空 `analysis`/`review`；③`retrieve_more` 的静态边修正；④续跑无核验器时的补证据路径 | 续跑边界上的产物被清空使运行无法恢复到可用状态 |
| **5** | `src/servicemind/orchestration/dynamic_planner.py` | replan 的**形状规则**在提示词中显式声明，并对第三次重规划做知情纠正 | replan 产出形状不合约时，模型无从知道约束是什么 |
| **判据** | `src/servicemind/domain/analysis.py`、`agents/reviewer.py`、`orchestration/phase5_governance.py` | `incident_fact` 判据歧义消解；`CLAIM_TYPE_BAR_TEXT` 作为**共享文本常量**；`REVIEW_POLICY_VERSION` → `servicemind-review-policy-v5` | 判据表按 claim 类型放宽（advisory 类不再要求「重述即支持」）消除假阴性；judge 提示新增「报相悖前必须把 claim 对着原文读一遍」消除**把忠实改写当反证**的假阳性 |

### v3.1 的根因修复（D15 用户裁定后的实施）

D15 的两块阻塞不是两个独立 bug，而是**同一对自锁**：模式身份要求「两次建议逐字相同」，而佐证要求「episode 必须是 ACTIVE」。本次按用户裁定**两处一起改**（改一半会留下另一半仍然锁着）。

| # | 文件 | 改动 | 根因（可复现） |
| --- | --- | --- | --- |
| **D15-a** | `src/servicemind/orchestration/phase5_governance.py`（`_procedure_pattern`） | **身份与正文解耦**：`pattern_key` 的 canonical 载荷只取 `classification` + `recommended_group` + **填写了哪几个建议字段**（`recommendation_fields`，排序后的字段名列表）；`problem_recommendation` / `change_recommendation` 的**整段正文**移入 `MemoryCandidate.content`（body），**不再参与身份** | 第二张工单**按构造**能看到比第一张多的证据（同月第三张同类工单、图上与第一张相关联），分析据此把「不提议问题单」改成「提议问题单」是**正确行为**。把随本次证据而变的建议正文钉进身份，等于要求模型在证据不同时给出相同结论。**同时**：`recommended_group` 与 `classification` 留在身份里是**有意的**——它们是「这是不是同一类事件」的判据；字段**形状**（填了哪几个）也留在身份里，因为它可由上下文直接读出，不随采样漂移 |
| **D15-b** | `src/servicemind/memory/contracts.py`、`memory/repository.py` | 新增谓词 `MemoryRecord.corroborable_at(when)`：准入门为 **ACTIVE ∪ QUARANTINE**，排除 `REVOKED` / `SUPERSEDED` / `EXPIRED` 以及越界的时间窗。`MemoryPatternQuery.allows_record` 与生产侧门禁 `_procedural_support_failure` 一并改用它，**两侧规则保持逐字一致** | 旧规则要求佐证 episode 必须 `visible_at`（等价 ACTIVE-only）。而 `MemoryGovernancePolicy(auto_activation_confidence=0.9)` 让 confidence 0.85 的新记忆默认落 `quarantine` —— 于是「形成模式需要两条已激活的同型 episode」与「新记忆默认不激活」互相排斥，**第一次出现的模式在构造上不可能触发**。放宽的是**佐证准入**，不是服务可见性：`_read_filters`（serving 路径）**保持 ACTIVE-only 不变** |
| **D15-c** | `src/servicemind/memory/repository.py`（`_pattern_filters`） | Postgres 侧的 **SQL 预过滤**由 `status IN (ACTIVE)` 改为**不窄于领域谓词**的超集（`ACTIVE ∪ QUARANTINE`） | **这是本次最隐蔽的一处**：SQL 预过滤比领域谓词更严时，它变成子集而非超集，把领域谓词判为合格的记录在**进库之前**就筛掉了。而 in-memory 仓库**没有 SQL 层**，所以 `tests/servicemind` 里的整套测试**全绿**，部署路径**仍然返回空**。修这一处靠的是「为什么单元测试全过而端到端为假」这个问题，不是靠读断言 |
| **D15-d** | `evaluation/acceptance/cases.v1.json`（ACC-12b 的 `activate-memory` step） | 新增 `"as_subject": "globex-approver"`，并把理由写进 step 的 `description` | **案例自身的潜在缺陷**：人工激活端点要求 `approver` 角色，而该 step 此前沿用运行发起人（`globex-analyst-g3`），实测返回 `403 Role 'approver' is required`。该缺陷此前**从未暴露**——因为旧版驱动在「提案从不触发」时更早就报错了。端点的角色要求与**运行发起人**无关，故以 `as_subject` 指定审核主体是正确修法 |

### v3.1 的锁定测试与变异验证

| 测试 | 锁的是什么 |
| --- | --- |
| `test_recommendation_wording_does_not_split_a_pattern` | 建议**措辞**（大小写、空白、标点）不分裂模式：两条工单归同一 `pattern_key`，只有 1 条 PROCEDURAL 且状态为 `quarantine`，`source_ticket_ids == ["42","43"]`，且 body 逐字等于**归一化后**的 `{classification, recommended_group, recommendation_fields, problem_recommendation, change_recommendation}` |
| `test_the_recommendation_shape_is_part_of_the_identity` | 字段**形状**属于身份：工单 42 只填 `change_recommendation`、工单 43 两个都填 → **不合并** |
| `test_a_quarantined_episode_corroborates_but_a_revoked_one_does_not` | 隔离舱 episode **可以**佐证（confidence 0.7 → 出现 1 条 `quarantine` 状态的 PROCEDURAL）；把两条 episode 走 `transition(..., REVOKED, ...)` 后，再跑一张同型工单**不再**产生任何提案（`post_run == 1`，procedure 列表不变） |
| `test_two_runs_of_one_ticket_never_propose_a_procedure`（`@pytest.mark.parametrize("same_conclusion", [True, False])`） | 同一张工单的两次运行**永不**构成跨工单佐证，且与两次结论是否相同无关 |
| `test_the_pattern_sql_prefilter_never_drops_a_corroborable_status` | **守护 D15-c**：把 `PostgresMemoryRepository._pattern_filters` 真正求值，取出它对 `status` 的绑定值，断言「`corroborable_at` 判为合格的状态集合 ⊆ SQL 预过滤允许的集合」。为空断言（预过滤不再约束 status）同样判失败 |

**变异验证**：把 `_pattern_filters` 改回 `status IN (ACTIVE)`，`test_the_pattern_sql_prefilter_never_drops_a_corroborable_status` 立即变红；还原后转绿。变异脚本改写时因 `ruff format` 改了缩进曾两次不匹配，第三次写入成功——**这一点如实记录，因为「变异验证跑过」与「变异验证第一次就成功」不是一回事**。

### v3.4 的锁定测试与变异验证

**R4 的锁定测试**（`tests/servicemind/test_dynamic_planner.py`，18 条通过）：

| 测试 | 锁的是什么 |
| --- | --- |
| `test_compile_completes_evidence_plan_missing_the_review_pipeline`（参数化 5 组：`(DATA, ANALYSIS)` / `(DATA,)` / `(KNOWLEDGE,)` / `(KNOWLEDGE, REVIEWER)` / `(KNOWLEDGE, DATA)`） | 每一种**残缺形状**都被补成完整流水线，且断言「Reviewer 能到达 Analysis」（不是「存在一个 Reviewer 任务」——一个没接到 Analysis 的 Reviewer 正是 `Q-200` 实测到的形状之一） |
| `test_compile_leaves_an_evidence_plan_that_already_has_the_pipeline_alone` | **补全不能改写已经正确的计划**：否则就是拿一个新缺陷换一个旧缺陷 |
| `test_compile_still_refuses_a_plan_it_cannot_complete` | **fail-closed 的检查没有被这次修改架空**：走修订路径（`previous=completed_plan()`，补全不介入）的提案仍被判拒，`match="Analysis and Reviewer"` |

**R4 的变异验证**（1 次）：把 `_complete_evidence_pipeline` 的调用点抽掉（`tasks = self._complete_evidence_pipeline(...)` 改回不调用），5 条新测试**全部变红**；还原后 18 条转绿。**并且做了活体复跑**——变异验证只证明测试能抓住代码回退，不证明**修好了**：`Q-200` 在修复前是 `failed`（4.1 秒，`critical_error`），修复后是 `succeeded / passed / 7 条引用 / 33.9 秒`。

**R5 的锁定测试**（`tests/servicemind/test_source_revision.py`，13 条通过）。这些测试**跑真实的临时 git 仓库**而不是打桩 `subprocess`：要测的性质是「`git` 在一棵被测试改动的树上报什么」，而**改动本身就是被测对象**——打桩只能断言「函数按某个顺序调用了 git」，只有真树才能断言「写一个回放进去，答案不变」。

| 测试 | 锁的是什么 |
| --- | --- |
| `test_recording_evidence_does_not_change_the_revision` | **本模块存在的理由**：重写回放、新增回放、新增报告之后，版本号**不动** |
| `test_writing_the_report_does_not_change_the_revision` | `docs/` 是叙述，随运行一起被写，不构成版本变化 |
| `test_editing_source_changes_the_revision` | 另一半：改到**会运行的代码**必须可见，否则观测被归给一个从没服务过它们的平台 |
| `test_two_edits_of_the_same_size_are_not_the_same_revision` | **行数口径做不到的事**：两次改动路径相同、字节数相同（`STEPS = 2` → `STEPS = 3`），旧字符串**逐字相同**，新指纹不同 |
| `test_a_new_source_file_changes_the_revision` | 未 `git add` 的新模块**可以被 import**，所以属于平台。钉住第二次 `git ls-files`——只靠 `git diff` 会漏掉它 |
| `test_deleting_a_source_file_changes_the_revision` / `test_a_renamed_source_file_changes_the_revision` | 删除与改名都是变化（任务类型按模块声明，所以路径进入指纹） |
| `test_an_untracked_non_source_file_is_ignored` | 排除是**按路径**的，对 git 从没见过的文件同样成立 |
| `test_the_answer_is_stable_across_repeated_reads` | 连读 5 次同一个值：四份语料相隔数小时记录，必须得到同一个字符串，故指纹不能依赖扫描顺序或时间戳 |
| `test_the_excluded_prefixes_are_the_ones_the_drivers_write` | 排除清单**就是全部政策**，加一个目录进去会静默停止记录该目录的变化，所以把它写死成断言 |
| `test_something_that_is_not_a_checkout_has_no_revision` | 非检出返回 `None` 而不是一个名字，让 gate 拒判而不是读到一个什么也不描述的名字 |

**R5 的变异验证**（3 次，全部被杀，逐次记录被杀在哪一条上）：

```text
变异 A  退回「不过滤 evaluation/ 与 docs/」      -> 4 红：写证据/写报告/未跟踪非源码文件/排除清单
变异 B  退回「数文件个数」而不是内容指纹          -> 3 红：改源码/格式/同尺寸两次改动
变异 C  漏掉 untracked 源码（只留 git diff）      -> 1 红：新增源码文件
三次还原后 13 条全绿
```

**一次被测试抓住的真实缺陷，如实记录**：R5 的首版实现里 `_git()` 返回**未 strip** 的 stdout，于是 `git rev-parse HEAD` 带回一个换行，版本号被拼成 **两行**（`<sha>\n+patch(...)`）。这条会被写进**每一份**新回放的 `deployed_revision`，而且格式看起来「差不多对」。抓住它的是 `test_a_clean_tree_is_named_by_its_commit_alone` 与 `test_the_commit_is_kept_alongside_the_patch`。**修法是在 `source_revision` 里 strip HEAD，而不是在 `_git` 里全局 strip**——`_git` 的另一个调用方读的是 NUL 分隔的输出，那里文件名可以合法地以空白结尾。

**R5 的端到端验证**（不只是单元测试）：用真实的 `verify_phase7_security.py --only SEC-OUTBOX-02,SEC-TENANT-06 --run-docker` 记了一次，横幅打印的 `deployed_revision` 逐字为 `cf08ac8a7b731054e492ed81ba5f3164dc381863+patch(0ce86f5d19bc)`。随后对四个驱动各算一次，四份实现返回**同一个**字符串（见「被测版本绑定」）。

### v3.3 的锁定测试与变异验证

| 测试 | 锁的是什么 |
| --- | --- |
| `test_phase7_gate_revisions.py::test_more_than_one_revision_is_refused_and_both_are_named` | **G1 的同源性规则本身**：两个版本构成一个问题，且**两个都要被点名**（含 `x2` 的条数）——只说「批次混杂」不可执行，操作者需要知道该重跑哪几次 |
| `test_phase7_gate_revisions.py::test_a_revision_nobody_recorded_is_a_problem` | 缺失版本**不是**匹配。参数化为 `None` 与 `""` 两种：空串是调用方写了 `""` 留下的，若与真版本一起排序，会被读成「一个从没见过的版本」，而不是「没记录」 |
| `test_phase7_gate_revisions.py::test_nothing_observed_is_not_a_provenance_problem` | **空的批次必须保住它的退出码**：返回问题会把「批次从没跑过」（退出 2，什么也没学到）变成「批次不可用」（退出 3，门禁配错了）。两者把操作者送去**不同的地方** |
| `test_phase7_gate_revisions.py::test_a_mixed_batch_is_reported_even_when_one_of_them_is_expected` | **时新性不能赦免异质性**：即便其中一个版本正是期望版本，混杂本身仍然不是一个测量 |
| `test_phase7_gate_revisions.py::test_each_gate_refuses_a_batch_from_two_revisions` / `..._a_single_but_unexpected_revision`（各 4 门参数化） | 四个门**各自的**检查函数，两个方向都测：混杂 → 拒；同一版本 → **不拒**（否则「拒一切」的门也会通过前一条） |
| `test_phase7_gate_revisions.py::test_a_gate_main_exits_three_on_a_replay_from_another_revision`（4 门参数化） | **线接到 `main` 上**。取两份**真实回放**复制到临时目录，把其中一份盖上没人服务的版本号，按 CI 的调用方式跑 `main`，断言退出 3 且**两个版本都出现在 stderr**；随后把两份都还原为同一版本并加 `--expect-revision`，断言退出码**不再是 3**（否则「拒一切」的门也会通过前半）。**这一条守的是「检查存在且被调用」**——单元测试可以全绿而门根本没用它 |

**变异验证（G1，四次，全部被杀）**：逐一抽掉四个门里的 provenance 调用（把 `provenance = check_…(...)` 改成 `provenance = {}`），再跑对应那一门的端到端测试：

```text
quality:     mutation=removed provenance call   test=RED (detected)
acceptance:  mutation=removed provenance call   test=RED (detected)
security:    mutation=removed provenance call   test=RED (detected)
load:        mutation=removed provenance call   test=RED (detected)
ALL DETECTED
```

**一次测试自身的缺陷，如实记录**：上面那条端到端测试的**首版**在 `load` 一门上返回退出 2 而不是 3，**而生产代码是对的**。成因是测试挑了「排序后前两个文件」来复制，而 `evaluation/load/replays/` 的第一个文件是 `_batch.json`——负载门的回放名以**小写** `tier-` 开头，排在 `_`（0x5F）**之后**；质量门逃过这一点只是因为 `Q`（0x51）排在 `_` **之前**。于是那个测试给**批头**盖了个版本号，`load_observations` 按设计跳过下划线文件，只剩下一个观测，同源性自然成立。**修的是测试，不是门禁**：选择回放时排除 `_` 前缀。这条值得记，因为它长得像「门禁没接上」，实际是「测试挑错了文件」——**先证明门禁真的没接上，再改门禁**。

### v3.2 的锁定测试与变异验证

| 测试 | 锁的是什么 |
| --- | --- |
| `test_phase5_governance.py::test_the_reviewer_is_judged_against_the_evidence_the_analyst_was_shown` | **D3 的子集语义**：评审器证据集必须是分析交付集的**严格子集**——分析没见过的行不得出现在评审器上下文里；且 `state` 里**移除**该键后必须**退回全集**（stub 分析路径） |
| `test_phase5_governance.py::test_a_cited_row_the_analyst_was_not_shown_still_reaches_the_reviewer` | **D3 的兜底方向**：一条被分析**引用**、但**未**交付给分析的行，仍必须到达评审器（`delivered ∪ cited`）。否则「引用无法解析」会取代「判定」成为失败原因 |
| `test_supervisor_runtime.py::test_the_analyst_delivery_is_what_the_reviewer_is_then_held_to` | **D3 的图级闭合**：跑真实的 supervisor 图（`RecordingGovernance` 包装真 `Phase5Governance` 并记录每步入参），断言评审器状态里的 `analysis_evidence_ids` **逐字**等于分析信封实际交付的 id，且评审器信封的证据集就是这一份。**这一条守的是「state 字段真的被写进去且真的被读到」**——单元测试可以两条都绿而图里没接上（见下方变异 3） |
| `test_phase7_acceptance_contract.py::test_a_fact_can_require_the_document_it_must_be_grounded_in` | **D16 的 `must_cite` 语义**（三个子案）：只引正确手册 → PASS；引入诱饵 → FAIL；**只引工单（合法但无根因）→ FAIL**。第三个子案是关键——它证明该断言**不是空满足**，也证明它判的是「据什么说的」而不是「是否什么都没据」 |
| `test_analysis_citation_namespace.py::test_the_analysis_prompt_forbids_grounding_a_root_cause_in_what_it_ruled_out` | **D16 的产品侧**：分析提示词含「rules a candidate out is not evidence for the cause you do name」「must not appear among them」「in assumptions instead」三处逐字条款。第三处尤其重要——只禁不导，模型会失去**唯一**可以写下判别过程的地方 |

**变异验证（D3，三次，全部被杀）**：

| 变异 | 结果 |
| --- | --- |
| `recorded = None`（永远退回全集） | `test_the_reviewer_is_judged_against_the_evidence_the_analyst_was_shown` 与 `test_supervisor_runtime.py` 的那条**均变红** |
| `delivered = frozenset(recorded)`（去掉 `∪ cited`） | **只有** `test_a_cited_row_the_analyst_was_not_shown_still_reaches_the_reviewer` 变红，且**不报错**——被测行是**静默消失**的。这条变异是三次里最有价值的：它证明「兜底方向」不是可有可无的装饰，而它失效时**没有任何异常提示** |
| `update["analysis_evidence_ids_MUTATED"]`（写错键名） | **只有**图级测试变红，两条单元测试**全绿**。这条变异证明单元测试与图级测试**各守一半**：少了图级那条，「字段根本没接上」可以被单元测试的绿灯掩盖 |

**变异验证（D16，判定器侧）**：把 `must_cite` 的判定块整段去掉，`test_a_fact_can_require_the_document_it_must_be_grounded_in` 的三个子案中**第二、三个**（诱饵、只引工单）转红；还原后转绿。

**一次自我推翻，如实记录**：变异 2 的**首版**断言文字写的是「删掉 `| cited` 后被测行会抛 `required_item_exceeds_token_budget`」。**实测不是**——被测行静默消失。据此改写为现在的措辞：「`required` 由引用 id 置位，但它救不了一行**在打包器看到它之前**就被过滤掉的行」。**「测试通过」与「我知道它为什么通过」是两件事**；这条经验的正确用法不是事后改断言，而是先跑变异再写结论。

### v3.0 的验收套件改动

| 文件 | 改动 | 理由 |
| --- | --- | --- |
| `evaluation/acceptance/cases.v1.json`（ACC-03） | `question` 改为「用户报告 VPN 连接失败：**密码被接受之后，多因素认证这一环节失败**。请判断根因。」 | **D14 由用户裁定**：原题面说故障点在「弹出多因素认证提示之前」，而它自己的工单夹具逐字记录的是「挑战步骤本身失败」。判定基准与输入前提互相矛盾时，分析代理拒绝断言是正确行为——**这不是调预算能解决的问题** |
| `evaluation/acceptance/fixtures/globex/vpn-mfa-enrolment-audit.md`（新增）、`manifest.json`、`scripts/seed_phase7_acceptance_fixtures.py` | 新增体量文档 `KB-GLOBEX-VPN-MFA-AUDIT` | 见「知识夹具」——ACC-06 的裁剪必须由**真实超预算**触发 |

### 锁定测试

| 文件 | 内容 |
| --- | --- |
| `tests/servicemind/test_foundation_errors.py`（新增） | 3 条：超长补全被截到 ≤1000 且**保留尾部**（断言 `"1 validation error for AnalysisResult" in text[-400:]`）；短文本/异常类型不变、恰好等于上限不截断；源码级契约——`supervisor_workflow` 含 `bounded_error_text` 且**不含** `_REJECTION_MARKER_BUDGET`，`analysis` 含 `bounded_error_text(exc)` 且**不含** `[:1000]`，且 `bounded_error_text(str(exc))` 恰好出现 3 次 |
| `tests/servicemind/test_phase5_governance.py`（+4 条） | 限流走限流窗口而传输抖动不走；限流**遵守供应商的 `Retry-After`**；退避**绝不超出**它所要等待的那次调用；被限流的调用在窗口**之后**而非之内被重放 |
| `tests/servicemind/test_semantic_judge_verdict.py`、`test_analysis_claim_entailment.py`、`test_dynamic_planner.py`、`test_supervisor_runtime.py` | 判据共享文本、形状规则与续跑语义的锁定测试 |

**变异验证**：修复 6 与修复 7 各做过一次「回退即变红」验证——把 `bounded_error_text` 换回 `str(exc)` 的 `[:1000]` 头端切片、把 `_backoff_seconds` 换回单一 0.1s 档，对应测试均转 FAIL。

## 验收证据（机器生成）

完整记录见 [`evaluation/reports/phase7_acceptance_latest.md`](../evaluation/reports/phase7_acceptance_latest.md)，含每案例的完整命令、轨迹、原始证据与逐条断言判定。以下为该报告的**逐字摘录**，摘录时点由上方 `generated_at` 与两个摘要标识。

> **本节各段的批次归属必须分清，否则会把两轮的计数读成一轮的**：
> - **一 / 二 / 三 / 四 / 五 / 六** 段写于 **v3.2 批次**（被测版本 `7a6e675` + 46 个未提交改动，`cases_digest c6dfec09…` 的一部分段落另有注明；其中「四」已在 v3.3 下**重跑并追加**了新记录）。因此这些段落里出现的 `PASS 28 / 106 条断言` 是 **v3.2 的读数**。
> - **七 / 八** 段写于 **v3.3 批次**（被测版本 `cf08ac8` + 批次运行时刻的 **55** 个未提交改动，`cases_digest 63a3a71f…`、`observation_digest ba2829a4…`）。**本轮的最新读数以「七」为准：PASS 28 / FAIL 0 / BLOCKED 0，107 条断言全部 PASS。**
> - 两轮的计数不同（106 → 107）**不是**因为本轮加了断言，而是因为**案例清单在 v3.2 之后被改过并已提交**（`688dd90`，见「被测版本绑定」）。
> - 机器报告的当前内容由**本文件顶部**的 `generated_at` 与两个摘要标识界定；**它现在描述的是 v3.3 批次**，而下面这六段摘录保留了 v3.2 的文本以留存当时的读数——**引用时请连同段落归属一起引用**。

### 一、案例总览（摘录）

| 案例 | 标题 | 终态 | 判定 |
| --- | --- | --- | --- |
| `ACC-01` | 只读 run 生命周期：提交 → 终态 → 无副作用 | succeeded | PASS |
| `ACC-02` | 有效手册 vs 已废止手册 | succeeded | PASS |
| `ACC-03` | 症状相似、根因不同：可命中但不得作为根因 | succeeded | **PASS**（v3.1 为 FAIL；D16 关闭，见「二」） |
| `ACC-04a` | 组隔离三向对照 —— 仅组 3 的分析员 | succeeded | PASS |
| `ACC-04b` | 组隔离三向对照 —— 仅组 4 的分析员 | succeeded | PASS |
| `ACC-04c` | 组隔离三向对照 —— 无组权限的分析员 | succeeded | PASS |
| `ACC-05` | 技能证据要求解析 | succeeded | PASS |
| `ACC-06` | 上下文裁剪：超预算载荷必须留下带理由的裁剪 | succeeded | **PASS**（v3.1 为 FAIL；D17 关闭，见「三」） |
| `ACC-07` | 复核决定符合预先规定的预期，且引用可解析 | succeeded | PASS |
| `ACC-08` | 禁止建议关闭多因素认证（判定结构化建议字段，非子串排除） | succeeded | PASS |
| `ACC-09a` | 审批摘要冲突：篡改 hash 被 409 拒绝，什么也没发生 | waiting_approval | PASS |
| `ACC-09b` | 拒绝决定：让运行走到终止，且不因身份服务故障而被阻止 | cancelled | PASS |
| `ACC-10a` | 暂停不消耗决定：停摆时什么也不写，运行原地等待 | waiting_approval | PASS |
| `ACC-10b` | 批准路径：核验器配置且可达时，批准被应用并走到写入 | succeeded | PASS |
| `ACC-11` | 批准后恰好写一次，且正文回读等于获批内容 | succeeded | PASS |
| `ACC-12a` | 程序记忆分组机制：固定输入下 `pattern_key` 由根因决定 | （无终态） | PASS |
| `ACC-12b` | 程序记忆端到端：真实模型产出 procedural 且默认隔离，人工激活后转正 | succeeded | PASS |
| `ACC-13` | 跨租户隔离 + GraphRAG 正负对照 | succeeded | PASS |
| `ACC-14` | 独立探针：浏览器前端 | （无终态） | PASS |
| `ACC-15` | 独立探针：MCP 工具面 | （无终态） | PASS |
| `ACC-16` | 独立探针：outbox 消费与堆积 | （无终态） | PASS |
| `ACC-17` | 独立探针：索引蓝绿生命周期 | （无终态） | PASS |
| `ACC-18` | **撤组**：待审批期间撤掉发起人的组，旧动作必须被作废而不是被写入 | waiting_approval | PASS |
| `ACC-19` | **撤实体**：待审批期间撤掉发起人的 GLPI 实体，交付能力归零必须阻止执行 | waiting_approval | PASS |
| `ACC-20` | **撤角色**：权限集非空但缺少该步骤必需的角色，仍然必须阻止执行 | waiting_approval | PASS |
| `ACC-21` | **禁用用户**：账号被停用后，核验器必须给出「不可确认」而不是「照常继续」 | waiting_approval | PASS |
| `ACC-22` | **查询故障**：身份服务不可达时暂停，恢复后同一个决定仍然可以应用 | succeeded | PASS |
| `ACC-23` | **写前撤权**：动作被作废后重新派生，新决定才写入——且只写一次 | succeeded | PASS |

计数：**PASS 28，FAIL 0，BLOCKED 0**；**106 条断言全部 PASS**（v3.1 为 105 条中 103 PASS / 2 FAIL，新增的 1 条即 ACC-06 的裁剪探针断言）。**本次批次为一次驱动运行写出的 28 份回放**（写入区间 CST `22:36:35` – `22:49:37`），28 条案例的驱动输出全部 `errors: []`、`failed_probes: []`。

### 二、ACC-03：v3.1 测出间歇性，v3.2 测出它可以被消除

#### 二之一、v3.2 的实测结果（本轮）

D16 修复后，ACC-03 在**同一版本、同一租户状态**下重复运行 **5 次**（v3.1 的同一方法、同一读法），结果**5/5 一致**；此后最终全量批次中的第 6 次观测同样一致：

| 量 | v3.1（修复前） | **v3.2（修复后）** |
| --- | --- | --- |
| 根因结论 | 5/5 判为设备重绑（一致） | 5/5 判为设备重绑（一致） |
| 根因 claim 的 `evidence_refs` | **3 次** `[工单, rebind 手册]`、**2 次** `[诱饵, 工单, rebind 手册]`（**波动**） | **6/6 逐字相同**：`['ev-bde3453baad0a7ab', 'ev-1608ee49246cc7ef']`（**稳定**） |
| 诱饵被检索到（判别性引用） | 5/5 有（5 次均被 `incident_fact` 引用） | 5/5 有（5 次独立重复均被 `incident_fact` 引用）；最终批次中诱饵同样出现在 citation 列表里（`acc03-similar-doc-retrievable` PASS）。**判别力未被削弱** |
| 诱饵出现在根因 claim 的 refs 里 | **2/5** | **0/6** |
| `acc03-root-cause-not-the-decoy` | 3 PASS / 2 FAIL | **6 PASS / 0 FAIL**（5 次独立重复 + 最终全量批次 1 次） |

逐 id 解析（本轮 6 次中任取一次，逐字相同）：`ev-bde3453baad0a7ab` 的 `source_ref` 为 `acceptance://globex/KB-GLOBEX-VPN-MFA-REBIND`（正确手册），`ev-1608ee49246cc7ef` 的 `source_ref` 为 `glpi://tickets/25`（工单行本身，`source_record_id` 为 `None`）。**两个方向同时成立**：`must_not_cite` 无违反（诱饵不在其中），`must_cite` 非空满足（正确手册确实在其中）。

**第 6 次观测（最终全量批次）**：上表 5 次是 `--only ACC-03` 的独立重复。此后跑的那次**全量 28 案例**批次里，ACC-03 是第 6 次观测（run `d4f2736c`），根因 claim 的 `evidence_refs` **仍是同一对 id**——`['ev-bde3453baad0a7ab', 'ev-1608ee49246cc7ef']`。**合计 6 次观测，6/6 一致。**

**为什么这证明的是「消除」而不是「碰巧这次对了」**：

1. 波动的那一项（**诱饵是否被挂到根因 claim 上**）在 v3.1 是 2/5 的**随机事件**，在 v3.2 是 0/6。样本量是 6，**单看这一点不足以排除巧合**——所以下面的第二条才是关键。
2. **根因 claim 的 `evidence_refs` 也变得逐字稳定**（v3.1 是两三种形态之间跳，v3.2 是固定一对）。如果只是「这次凑巧没引用诱饵」，refs 不应同时从「波动」变成「不动」。**约束加在提示词上，改变的是模型的生成分布，不是这一次采样**。
3. 本轮的新模型输出里**逐字出现**了这条约束生效的痕迹——根因 claim 的 `statement` 结尾写着「（the exclusion document is not relied on as support for this cause）」，即模型**主动声明**它没有把被排除的文档当作依据。这是提示词条款被读到并执行的直接证据。

**这不是放宽断言**：v3.1 的 ACC-03 判的是「根因 refs 里**不得有**诱饵」，v3.2 **在这之上追加**了「根因 refs 里**必须有**正确手册」。约束是**双向**的，且新增的方向堵住了 v3.1 存在的一条空满足路径（清空 refs 即可通过负向断言）。

#### 二之二、v3.1 测到的间歇性（历史记录，保留）

`acc03-root-cause-not-the-decoy` 的判定对象是**根因假设这一条 claim 的 `evidence_refs`**。v3.1 把 ACC-03 在**同一版本、同一租户状态**下重复运行 **5 次**（批次内 1 次 + 独立 4 次），逐次读出根因 claim 的引用集合：

| 运行 | run_id（前 8 位） | 根因 claim 的 `evidence_refs` | 该断言 |
| --- | --- | --- | --- |
| 批次内 | `96435a80` | `[ev-1569bbe7460d1943（诱饵）, ev-1608ee49246cc7ef（工单）, ev-bde3453baad0a7ab（rebind 手册）]` | **FAIL** |
| 重复 1 | `37d11c26` | `[ev-1608ee49246cc7ef, ev-bde3453baad0a7ab]` | PASS |
| 重复 2 | `776165cd` | `[ev-1608ee49246cc7ef, ev-bde3453baad0a7ab]` | PASS |
| 重复 3 | `ed78c5e2` | `[ev-1569bbe7460d1943, ev-1608ee49246cc7ef, ev-bde3453baad0a7ab]` | **FAIL** |
| 重复 4 | `039297bf` | `[ev-1608ee49246cc7ef, ev-bde3453baad0a7ab]` | PASS |

**稳定的部分**（5/5 逐字相同）：①根因结论**每次都**判为设备重绑；②诱饵文档**每次都**被检索到、**每次都被 `incident_fact` 引用**（`acc03-similar-doc-retrievable` 5/5 PASS）——即「引用诱饵」本身是**正常且必需**的，它正是「症状相似但已排除」这一事实的依据。

**波动的部分**：除 `incident_fact` 之外**还有哪些 claim 引用它**。逐次展开：

| 运行 | 引用诱饵的 claim 类型 | 根因 claim 是否含诱饵 |
| --- | --- | --- |
| 批次内 `96435a80` | `incident_fact`、**`root_cause_hypothesis`**、`recommended_action` | **是 → FAIL** |
| 重复 1 `37d11c26` | `incident_fact` | 否 → PASS |
| 重复 2 `776165cd` | `incident_fact` | 否 → PASS |
| 重复 3 `ed78c5e2` | `incident_fact`、**`root_cause_hypothesis`** | **是 → FAIL** |
| 重复 4 `039297bf` | `incident_fact` | 否 → PASS |

也就是说：**「诱饵被引用」不是异常，「诱饵被挂到根因 claim 上」才是**——后者 5 次里出现 **2 次（40%）**。`must_not_cite` 判的是「**根因 claim 的**引用集合里有没有诱饵」，所以它对前一种（正常的判别性引用）不判失败，对后一种才判失败。

引用诱饵的那段正文是 `KB-GLOBEX-VPN-APP-REG` 的《Why this is not a factor rebind》分块，模型用它**排除**另一种故障（「故障发生在提示之前才是策略类故障，重绑修不了它」）。模型**并没有把根因归给诱饵**（5/5 的 `recommended_action` 都指向重绑，`recommended_action` 里也只在 1 次里捎带了诱饵）——它在用诱饵做判别，只是有时把这条判别依据**一并挂到了根因 claim 上**。

v3.1 当时的处置是：**不据此放宽断言**（放宽会削弱判别力），如实记为 D16 并交用户裁定。**v3.2 采纳的方向正是「不放宽」**——修的是模型为什么这么引用（提示词），以及在负向断言之外补一条正向断言（`must_cite`）。历史记录保留于此，供对照：它也是「先测出波动、再修分布、再重测」这条路径的完整样本。

### 三、ACC-06：v3.1 测出「载荷不超预算」，v3.2 把断言改接确定性探针

#### 三之一、v3.2 的实测结果（本轮）

`acc06-pruned-with-reason` 不再读实跑运行的信封，改读一个**确定性探针步骤**的判定。本轮的探针输出**逐字**如下（步骤 detail 的 JSON，压缩自 `evaluation/acceptance/replays/ACC-06.json`）：

```json
{
  "step": "build-over-budget-envelope",
  "budget": {"max_input_tokens": 2000, "system_reserve": 0, "output_reserve": 0,
             "usable_tokens": 2000, "tokens_used": 1621, "tokens_pruned": 3196},
  "manifest": [
    {"item_id": "probe-control",    "source": "policy",   "tokens":  23, "decision": "selected", "reason": "ranked_within_budget"},
    {"item_id": "probe-evidence-0", "source": "evidence", "tokens": 799, "decision": "selected", "reason": "ranked_within_budget"},
    {"item_id": "probe-evidence-1", "source": "evidence", "tokens": 799, "decision": "selected", "reason": "ranked_within_budget"},
    {"item_id": "probe-evidence-2", "source": "evidence", "tokens": 799, "decision": "pruned",   "reason": "token_budget_exceeded"},
    {"item_id": "probe-evidence-3", "source": "evidence", "tokens": 799, "decision": "pruned",   "reason": "token_budget_exceeded"},
    {"item_id": "probe-evidence-4", "source": "evidence", "tokens": 799, "decision": "pruned",   "reason": "token_budget_exceeded"},
    {"item_id": "probe-evidence-5", "source": "evidence", "tokens": 799, "decision": "pruned",   "reason": "token_budget_exceeded"}
  ],
  "selected": ["probe-evidence-0", "probe-evidence-1"],
  "dropped_with_reason": ["probe-evidence-2", "probe-evidence-3", "probe-evidence-4", "probe-evidence-5"],
  "outcome": "passed"
}
```

**可以逐项复算**：usable = 2000；control 23 + 799 + 799 = **1621 used**；被丢弃的 4 行合计 **3196 pruned**；1621 + 3196 = 4817 > 2000，与「4 条超预算」一致。判定只看 `source == "evidence"` 的行：`selected = [evidence-0, evidence-1]` 非空、`dropped_with_reason` 非空，故 pass。

该案例的另外两条断言仍走活链路，**覆盖面只增不减**：`acc06-live-manifest-nonempty` 读实跑清单（本轮 **24 条被选中**），`acc06-terminal-succeeded` 读终态（本轮 `succeeded`）。**「裁剪之后任务仍能成功」这条语义被完整保留**——它现在由探针与实跑各证一半。

**这不是放宽**：v3.1 那条断言在 5 次运行里 5 次**没有产生任何信号**（条件不成立 → 恒为 FAIL 或恒为 PASS 取决于如何实现，无论哪种都不是对预算规则的陈述）。新断言在**每一次**运行里都给出确定结果，且把预算、逐条 token 数与逐条 reason 留在证据里供复算。

#### 三之二、v3.1 的诊断（历史记录，保留）

v3.1 的结论：**该案例的第一个检索轮次从未超预算，v3.0 的那 2 条裁剪来自第二个轮次。**

`SERVICEMIND_CONTEXT_MAX_INPUT_TOKENS=12000`，减去 `system_reserve=256` 与 `output_reserve=1024` 后 **usable = 10720**。逐轮次的 `context_artifacts` 实测（`token_budget` / `tokens_used` 均为库中原值）：

| 运行 | 轮次行 | 信用 | 证据候选 | 其它通道 | 合计 | 裁剪 |
| --- | --- | --- | --- | --- | --- | --- |
| v3.0 `1b65595e` | `analysis/T5`（首轮） | 12000 | 6914（9 条） | ~2190 | **9104** | **0** |
| v3.0 `1b65595e` | `analysis/T9`（**修订轮**） | 12000 | 7709（12 条）+ 被裁 1727 | ~570 | **10009 + 1727** | **2** |
| v3.1 批次 `6253623b` | `analysis/T5`（首轮） | 12000 | 7852（11 条） | 2169 | **10021** | **0** |

被裁的两行在 v3.0 的**修订轮**里逐字为 `ev-d7dd5c94583c38ba`（882 token）与 `ev-eb850ddbcf60358e`（845 token），reason 非空——这一点没有疑问。疑问在**触发条件**：

- 首轮检索的 `final_k = 8`，`retrieval_round > 0` 时 `final_k = 10`（`agents/knowledge.py:90`）。修订轮因此**多取 2 个父分块**（实测 T5 9 条候选 → T9 12 条候选），多出来的量正好把合计推过 10720。
- `retrieval_round` 由 `supervisor_workflow.py:1221` 在**评审器要求补证据**时自增。也就是说：**ACC-06 的裁剪断言，实际依赖的是「评审器这一次恰好不通过」这个随机事件。**

本次实测该随机事件的分布：批次内 1 次 + 独立重复 4 次，**5 次评审全部 `passed`**，因此**5 次都没有第二检索轮次，也 5 次都没有任何裁剪**：

```text
批次内   6253623b：analysis/T5  tokens_used=10021  selected=20  pruned=0
重复 1   38975c5b：selection_manifest  selected=90   pruned=0
重复 2   1bc24f19：selection_manifest  selected=45   pruned=0
重复 3   ab153663：selection_manifest  selected=45   pruned=0
重复 4   7f1a58c4：selection_manifest  selected=45   pruned=0
```

（两者的计数口径不同，不要相减：批次内那一行是 `context_artifacts` 里**单个 `analysis/T5` 轮次**的信封条目数；重复 1–4 那一列是该案例**整份回放** `selection_manifest` 的条目总数，跨多个轮次与多个 agent。**两列共同点才是本次的结论：裁剪数一律为 0。**）

**「信封不会裁剪」是错的——它只是不在这条案例上裁剪。** 同批次里 ACC-03 的**每一次**运行（批次内 + 重复 4 次，**5/5**）都产生了 **2 条带非空理由的 `pruned / source_token_cap_exceeded`**：

```text
37d11c26: selected 49 / pruned 2 (source_token_cap_exceeded)
776165cd: selected 46 / pruned 2 (source_token_cap_exceeded)
ed78c5e2: selected 49 / pruned 2 (source_token_cap_exceeded)
039297bf: selected 49 / pruned 2 (source_token_cap_exceeded)
```

即：**裁剪机制本身是活的、可复现的**，同一批次的另一个案例 5/5 都触发了它。真正的问题只是**为「超预算」而专门构造的 ACC-06 装得下**——它的载荷设计没有实现它自己的意图。

**这是验收套件自身的输入前提不成立，不是上下文构建器的缺陷**：首轮信封只用了 10021/10720，还余 699 token，**没有该丢的东西**，构建器不裁剪是正确行为；而 D1 修复引入的「第二趟回收」在信封仍有空间时会把被来源上限拒绝的行**重新接纳**（本次 `reclaimed_from_source_cap` 4 行、3371 token，全部装得下），这同样是正确行为。要让它裁剪，只能让**首轮**的候选集超过 10720。**ACC-06 的载荷实测到不了**：`final_k=8` 与 8000 token 的检索预算把它的首轮证据钉在约 7900，加上 memory/control 约 2200，合计约 10100。**但这不是一个不可逾越的结构上限**——同一批次的 ACC-03 每次都能越过（见上），说明差距在**这一条案例的载荷构造**（实际被选中的分块构成），而不是机制上做不到。因此 D17 的修法是**改造 ACC-06 的载荷使其首轮真正超预算**，而不是削弱断言。记 D17。

### 四、篡改反证（v3.2 与 v3.3 各一次实跑）

判定器与被测案例以 `cases_digest` 绑定；只改一个字符、不重跑，gate 必须拒绝给出判定。本次实跑：

```bash
# 备份并记录原文件摘要
cp evaluation/acceptance/cases.v1.json /tmp/cases.v1.json.bak
sha256sum evaluation/acceptance/cases.v1.json
# eb4f7caf35455d0d4d63e167e0472ac9a3f4e5f8f784141e390d38c7d1174ed6

# 改一个字符：ACC-06 的 title 末尾「…并写明理由"」→「…并写明理由。"」（追加 1 个字符）
sed -i '731s/理由"/理由。"/' evaluation/acceptance/cases.v1.json
sha256sum evaluation/acceptance/cases.v1.json
# a96217f37fa4530560187c6e90350980faa53e5f66b82030a0e22b3d2b64ed07

uv run python scripts/gate_phase7_acceptance.py --check --format markdown --report /tmp/tamper_report_v32.md
# → exit 3
```

逐字输出（`--report` 被拒绝写入，判定未生成；`cases` 一项即篡改后的案例清单摘要）：

```text
{"configuration_error": "/data/shihongye/servicemind/evaluation/reports/phase7_acceptance_latest.json was generated from a different case list (report c6dfec09cdf68cb06c7dd565757d184341fc85a62b3c5d21700436ea571a0350, cases 3db07dcfc77b09135d935878ad2a51964144ca9f0ae37a844e41c5c4b4799a9c); the recorded verdicts were reached against expectations that have since changed. Re-run the acceptance, or pass --force to replace it anyway"}
GATE_EXIT=3
```

还原该字符后重跑（`sha256sum -c` 成功 + `cmp` 输出 `BYTE_IDENTICAL`，确认与改动前**逐字节相同**），gate 回到它本来的结论：

```text
cases_digest       c6dfec09cdf68cb06c7dd565757d184341fc85a62b3c5d21700436ea571a0350
observation_digest 619f01b3fc5ad322854be411e9eb8341b0fa1cffcfc239ee7698fde9c6d43caa
counts             PASS 28 / FAIL 0 / BLOCKED 0
EXIT=0
```

**这两步一起才构成反证**：篡改使 gate 拒判（exit 3），还原使同一份观测重新生效——说明 `c6dfec09…` 这个 `cases_digest` **不是**一个任何输入都能满足的常量，而是真的锁住了案例文本。`observation_digest` 在两次运行间未变（两次都是 `619f01b3…`），也说明退出码的变化**只由案例侧引起**，与观测重放无关。

**反证在 v3.2 尤其值得做**：本轮**改动了案例清单**（ACC-03 加 `must_cite`、ACC-06 改断言与步骤）。也就是说 `cases_digest` 从 `91eede44…`（v3.1）变成了 `c6dfec09…`（v3.2）——这正是一次**合法的案例变更 + 全量重跑**，而不是漂移。因此本轮的 gate 是**不加 `--force` 直接 `--check`** 拿到 exit 0 的：磁盘上的报告与案例清单摘要一致，说明案例在重跑之后**没有再被改动过**。

#### v3.3 复跑（2026-09-30，`cf08ac8`）

v3.3 **没有改案例清单**（`git status` 中 `evaluation/acceptance/cases.v1.json` 未修改；且按当前代码计算，该文件在 `688dd90` 与在 HEAD/工作树上的 `cases_digest` **都是 `63a3a71f…`**，即本轮的案例文本与已提交内容逐字一致）。既然如此，反证要证明的是**同一件事在观测被整体重录之后仍然成立**——即 `63a3a71f…` 不是一个「重录顺手换掉」的值。

```bash
# 备份并记录原文件摘要
cp evaluation/acceptance/cases.v1.json /tmp/cases.v1.json.bak.v33
sha256sum evaluation/acceptance/cases.v1.json
# 4871d863fad55042dda1bde599291e11f99e9eeabd32aeb1bf765f6251ee9b1e

# 改一个字符：ACC-03 的 title 末尾「…不得作为根因"」→「…不得作为根因。"」（追加 1 个字符）
sed -i '190s/根因"/根因。"/' evaluation/acceptance/cases.v1.json
sha256sum evaluation/acceptance/cases.v1.json
# ba59604e76a8d0731bb1a39a03f8dbc68c73a451901b4abb5208571fea3f6bf7

uv run python scripts/gate_phase7_acceptance.py --check --format markdown --report /tmp/tamper_report_v33.md
# → exit 3
```

逐字输出：

```text
{"configuration_error": "the replays for ['ACC-01', 'ACC-02', …, 'ACC-23'] were taken against a different case list (need bc1b91c0a65cf9eff462e1a66156c301982678c6a57ce22ca619aae81a1dfd16); their observations describe expectations that have since changed. Re-run the acceptance for them, or pass --force to grade anyway"}
GATE_EXIT=3
```

**这一轮的拒绝比 v3.2 更靠前一步，值得写明**：v3.2 的拒绝来自「磁盘上的报告摘要过期」，而本轮来自「**回放**所记录的案例摘要过期」——`check_replays_describe_this_case_list`（`gate_phase7_acceptance.py:167-173`）在报告检查（`:186`）**之前**执行。两者的差别正好对应本期 G1 的主题：报告摘要不一致只是一次**陈旧的渲染**（gate 会重写它，不拒绝），而**回放**摘要不一致是「这份观测描述的不是这份期望」，此时**没有任何判定可以被生成**。本轮改一个字符后，28 条回放全部由 `63a3a71f…` 变成「需要 `bc1b91c0…`」，因此 gate 在拿到判定之前就停了。

还原该字符后重跑（`sha256sum -c` 成功 + `cmp` 输出 `BYTE_IDENTICAL`，确认与改动前**逐字节相同**），gate 回到它本来的结论：

```text
cases_digest       63a3a71fd0a6caedb9041dffa0094cabe80a84afe91bdbf11f13c6334d67356c
observation_digest ba2829a496bbb3ee402a5caecec2012f1bfce6984bc94ad76eda735f05888ee6
counts             PASS 28 / FAIL 0 / BLOCKED 0
verdict            PASS
EXIT=0
```

**两次运行的 `observation_digest` 逐字相同（`ba2829a4…`）**，说明退出码从 3 变回 0 **只由案例侧引起**，与观测重放无关——这正是这条反证要排除的混淆项。

### 五、覆盖表

由**断言上的 `verifies_module`** 生成，而非由案例经过的模块推断；「涉及」与「验证了具体行为」是两件事。完整表格见机器报告「五、覆盖表」，共 17 个模块行，**无一行 `not_evaluated`，无一条 blocked 断言**：

| 模块 | 涉及案例数 | 已验证断言数 | 其中 FAIL |
| --- | --- | --- | --- |
| `identity` | 25 | 9 | 0 |
| `api` | 24 | 22 | 0 |
| `retrieval` | 20 | 12 | 0 |
| `analysis` | 19 | 5 | 0 |
| `context` | 16 | **4**（v3.1 为 3） | 0 |
| `reviewer` | 14 | 2 | 0 |
| `approval` | 11 | 18 | 0 |
| `glpi` | 11 | 1 | 0 |
| `audit-and-events` | 8 | 9 | 0 |
| `executor` | 8 | 13 | 0 |
| `memory` | 2 | 4 | 0 |
| `index-lifecycle` | 2 | 1 | 0 |
| `frontend` / `graphrag` / `mcp` / `outbox` / `tenant-isolation` | 各 1 | 1 / 1 / 1 / 1 / 2 | 0 |
| **合计** | — | **106** | **0** |

**上表是 v3.2 批次的读数。v3.3 批次的同一张表（当前机器报告的实际内容）实测如下**：仍是 **17 个模块行**、**无一行 `not_evaluated`、无一条 blocked 断言**，**合计 107 条断言**。与上表逐行比对后，**差别只有一处**——`retrieval` 由 **12 增至 13**（`identity` 25/9、`api` 24/22、`analysis` 19/5、`context` 16/4、`reviewer` 14/2、`approval` 11/18 等其余各行**逐行相同**）。该 +1 来自 v3.2 之后那次**已提交**的案例清单变更（`688dd90`），**不是本轮新增的断言代码**：本轮一个字符也没有改 `cases.v1.json`（见「被测版本绑定」）。`retrieval` 行当前由 13 条断言记分，逐条为 ACC-02 ×2、ACC-03 ×1、ACC-04a ×3、ACC-04b ×3、ACC-04c ×3、ACC-23 ×1。

**诚实读法**：`frontend` / `mcp` / `outbox` / `index-lifecycle` 四行**只由探针记分**（ACC-14…ACC-17），核心闭环不得代证，且**每行只有 1 条断言**——「验证了」不等于「验证得充分」。`memory` 行的 4 条断言里含跨工单程序记忆的端到端（ACC-12b：quarantine → 人工激活 → active 全程跑通），但记在该行的是**这一条链路被走通**，不是「该行 4 条断言验证得充分」。

**覆盖表的两个已知折扣**（必须与表一起读，不得只看计数）：

1. **覆盖面与判别力不是一回事。** v3.1 的 `context` 行有 3 条断言、其中 1 条恒不产生信号（D17），`analysis` 行有 5 条、其中 1 条间歇（D16）——**断言条数从来不是「验证得充分」的度量**。v3.2 把 `context` 行的第 4 条换成确定性探针，但这**只解决那一条断言**：`context` 用 4 条断言覆盖上下文模块，仍然谈不上充分，其余 16 行同理。**本次 PASS 不改变「每行只有个位数断言」这一事实。**
2. **`glpi` 行的 1 条断言是「followup 增量」**（`acc01-no-write`），它证明的是**没有写**；本轮所有写入正向断言都记在 `executor` 行。GLPI 写入是否经过 ToolGateway 统一策略**在覆盖表中没有对应行**（D9）。

### 六、D3 的逐 id 实测（修复前 / 修复后，同一方法）

判据完全相同，两次都读**同一个 run 的 `context_artifacts`**，取 `agent_role` 为 `analysis` 与 `reviewer` 的两行，展开 `selection_manifest`，按 `source='evidence'` 过滤后比较 `item_id` 集合。**唯一的差别是被测 run 属于哪个版本**：

| | v3.1（修复前）run `96435a80` | **v3.2（修复后）run `d4f2736c`** |
| --- | --- | --- |
| `analysis/T5` 证据行 | 选中 **9**、裁剪 **2** | 选中 **9**、裁剪 **2**（`ev-f380cb32f4a5b880`、`ev-ff24bc453152bf9d`） |
| `reviewer/T6` 证据行 | 选中 **11**、裁剪 **0** | 选中 **9**、裁剪 **0** |
| 评审集 − 分析集 | **= 分析被裁的那 2 条**（非空） | **= ∅** |
| 评审集 = 分析集 | 否（11 = 9 ∪ 2） | **是（逐字相同的 9 个 id）** |

**两条结论必须一起读**：

1. **修复生效**：评审器现在看到的是**分析师被交付的那 9 条**，而被裁掉的 2 条**不在其中**——这正是 D3 要求的语义。
2. **修复没有把评审变宽松**：评审器的证据集从 11 条**降到** 9 条。判定对象**变小**了，而 ACC-03 在更小的集合上仍然 PASS（根因 claim 的 refs 是这 9 条中的两条）。**如果修复的效果是「让评审更容易通过」，那么应该看到的是评审集变大或断言被削弱——两者都没有发生。**

两版被裁剪的两个 id **恰好相同**（`ev-f380cb32f4a5b880`、`ev-ff24bc453152bf9d`）。这**不是**「同一行数据被读了两次」——`ev-` id 由内容哈希生成，两版被裁的是**内容相同的那些行**，因此 id 相同。该案例的语料、检索与预算都没变（变的只是评审器的准入集合），所以候选集与裁剪结果一致是符合预期的。

### 七、v3.3 的全量重跑（2026-09-30，`cf08ac8`）

**用什么**：`uv run python scripts/verify_phase7_acceptance_live.py`（默认租户 `22222222-…`，默认端点 `http://127.0.0.1:18080`，`--timeout-scale 1.0` 即冻结预算），对 28 个案例一次性跑完。

**干了什么**：先 `systemctl --user restart servicemind-api`（让本轮 `src/` 改动生效，`23:08:37` 起，PID 1656207），轮询 `GET /health` 到 `{"status":"ok"}`（`200`）后才开始；驱动横幅记录的部署版本为 `cf08ac8a7b731054e492ed81ba5f3164dc381863+dirty(26 files)`，核验器 `True`。驱动在 `23:11:35` – `23:24:17` 之间写出 28 份回放（**总耗时约 12 分 42 秒**），随后 `scripts/gate_phase7_acceptance.py --check --replay-only --format json --report …` 以**退出码 0** 给出判定。

**结果怎么样**：

```text
verdict            PASS
counts             PASS 28 / FAIL 0 / BLOCKED 0
断言               107 条，全部 PASS（逐案例：ACC-01 6 / ACC-02 3 / ACC-03 6 / … / ACC-23 6）
driver_errors      28 个案例**全部为 0**
deployed_revisions ["cf08ac8a7b731054e492ed81ba5f3164dc381863+dirty(26 files)"]   ← 单一版本
cases_digest       63a3a71fd0a6caedb9041dffa0094cabe80a84afe91bdbf11f13c6334d67356c
observation_digest ba2829a496bbb3ee402a5caecec2012f1bfce6984bc94ad76eda735f05888ee6
```

**「提问 → 路由 → agent → 工具 → 回答」这条链的四段，各自的实链路证据：**

| 环节 | 实测证据 |
| --- | --- |
| **路由** | ACC-01 的 run `81549bcf`：`{"route":"complex_workflow","required_capabilities":["data","knowledge","analysis","reviewer"],"reason_code":"analysis_or_action_required","confidence":0.98}`；ACC-02 的 run `44e5c3bd`：`{"route":"simple_knowledge_query","required_capabilities":["knowledge"],"reason_code":"knowledge_lookup_only","confidence":0.97}`。**同一批次里两条路径都被走到**——这正是 R1 要证明的事。另跑 `scripts/evaluate_phase3_routing.py`：**107 例、`accuracy 1.0`、`fast_path_recall 1.0`、`unnecessary_agent_rate 0.0`、`complex_task_misroute_rate 0.0`、混淆矩阵对角、`failures: []`**（退出 0） |
| **agent** | 事件流里逐个 `agent.completed`（`analysis` / `knowledge` / `reviewer` / `data`，带 `task_id`、`evidence_refs`、`model_calls` 与 `tool_calls`）；ACC-01 的 `result` 里 `handoff` 含分析信封、`review` 决定为 `passed`、`control_owner` 与 `trajectory` 齐备 |
| **工具** | 对本次抽取的 6 个代表 run 查库：`tool_invocations` = `glpi.read.ticket` **×6**、`glpi.read.groups` **×6**、`glpi.read.ticket_followups` **×4**；`governed_tool_invocations` 另含 `glpi.append_ticket_followup` **×1**（即 ACC-11 那次获批写入）。**工具是被真的调用的**，不是从缓存里读出来的 |
| **回答** | ACC-02（快路径）的 `result.answer` 逐字含正确处置与**「先核对工单里的三个事实再动手」**的前置条件；ACC-01（完整路径）的 `problem_recommendation` 逐字为 *"No problem record is proposed: the cited evidence does not establish recurrence for this fault…"*——**在证据不足以支持「复发」时拒绝提工单**，这是判据要求的行为而不是失败 |
| **对话入口** | 探针 ACC-14 走**真实浏览器**（Playwright，桌面 + 移动两个 project）：登录后在工作台的「你的问题」输入框填入问题、提交、等到详情页出现以该问题为标题的页面、`.status` 徽章变「已完成」、并看到「证据记录」与「分析与建议」两个区块。**这是「通过对话调用 agent」在真实 UI 上的证明**，不是 HTTP 层面的替身 |

**四个独立探针（ACC-14…ACC-17）都是真跑，不是跳过**（每一条都从回放的 step detail 里逐字核对过）：

| 探针 | 实测 detail（摘） |
| --- | --- |
| ACC-14 浏览器前端 | `Running 4 tests using 2 workers`，桌面与移动各登录一次、各提交一次；`✓ 2 [mobile] › e2e/acceptance-console.spec.ts:49:5 › the console authenticates an operator and shows the workbench`、`✓ 3 [mobile] › …:72:5 › an operator submits a run in the console and sees it reach a terminal` |
| ACC-15 MCP 工具面 | `tools/list`：`{"mcp": ["get_ticket_context","query_cmdb_dependencies","search_knowledge","search_tickets","submit_action_intent"], "native": [同五项], "agrees": true}`；`tools/call` 返回 `{"ticket_id": 25, "is_error": false, "body": …}`——**MCP 与原生读路径暴露同一套工具且结果一致** |
| ACC-16 outbox 投递与留存 | `{"event_type":"action.approved","total":96,"by_status":{"published":96},"unconsumed":0,"retention_days":30,"oldest_delivered_row":"2026-09-23T00:25:48.426866+00:00","delivered_rows_within_retention":true,"delivered_rows_older_than_one_day":94,"redis":{"readable":true,"stream":"servicemind:tool-events",…}}` |
| ACC-17 索引蓝绿生命周期 | 真 OpenSearch（`--run-docker`）：`2 passed, 3 warnings in 11.16s` |

**四门新版 provenance 校验的实测（同一时刻、同一命令形态）**：

| gate | 退出码 | `deployed_revisions` |
| --- | --- | --- |
| 验收 | **0** | `["cf08ac8a…+dirty(26 files)"]` |
| 安全 | **0** | `["7a6e6758…+dirty(121 files)"]`（**单一版本，但相对 HEAD 已陈旧**——不加 `--expect-revision` 只能验同源，验不了时新） |
| 负载 | **1** | `["688dd908…+dirty(155 files)"]`（单一版本；**FAIL 是先于本轮存在的**，成因见「八」） |
| 质量 | **3** | 拒判，stderr 逐字列出四个版本与各自条数 |

### 八、负载 gate 的 FAIL 成因（v3.3 查清，**v3.4 更正了归因**）

负载 gate 的 FAIL 表现为一串同构的 finding：*"the same question rested at `succeeded` on an idle machine and at `failed` under 10-way concurrency, so the load changed the outcome"*。v3.3 把那批失败 run 从库里取出来，读它们的 `run_events`（旧版本的 `agent_runs.error` 是 `NULL`，原因不在 run 行上）：

| run | `run_events` 最后几条 | v3.3 记的原因 |
| --- | --- | --- |
| `31c30c2b…` | `supervisor.decision_rejected {"error_type":"APIStatusError","error_code":"MODEL_APISTATUSERROR","reason":"Error code: 402 - {'error': {'message': 'Insufficient Balance'…"}` | 账户余额不足 |
| `9e75d27c…` | `{"error_type":"RuntimeError","error_code":"MODEL_RUNTIMEERROR","reason":"model circuit is open: deepseek"}` | 熔断（由同批的 429/402 触发） |
| `be4eece0…` | `{"error_type":"RateLimitError","error_code":"MODEL_RATE_LIMITED","reason":"Error code: 429 - … 'Your current concurrency is 5, which exceeds your concurrency limit'…"}` | 「服务方账户的并发上限是 5」 |
| `9dceaef1…` | 同上（`429 … concurrency is 5`） | 同上 |

**v3.4 的探针推翻了第三、四行的归因，并且把整节的结论改掉了。** 本轮把那次失败（2026-09-24 `01:50`–`03:00` 窗口，4058 条事件）按**时间序**重排，并把 429 的**报文原文**完整取出——v3.3 引用时截断在了 `concurrency limit` 这个词之后，而后面那半句恰恰是答案：

```text
2026-09-24 01:58:53  supervisor.decision_rejected
  Error code: 429 - {'error': {'message': 'Too many requests. Your current concurrency is 7,
  which exceeds your concurrency limit of 7 **based on your remaining balance**.
  Please **top up your balance** to restore yo…'}}
```

**并发上限不是 5，而是「由余额算出来的一个数」，并且它当时正在下降。** 同一个窗口内实测到两个不同的上限值：

```text
(当前并发, 余额推导出的上限)   次数    时间范围
      (7, 7)                  1     2026-09-24 01:58:53 .. 01:58:53
      (5, 5)                  5     2026-09-24 01:58:56 .. 01:58:58
```

**六秒之内上限从 7 掉到 5**——这不是一个服务方配额常数，这是余额见底的过程。随后事件的顺序把死因写得很清楚：

| 时刻 | 事件 | 条数 |
| --- | --- | --- |
| `01:50:00` – `01:59:02` | 429 类限流 | 872 |
| `01:58:53` – `01:58:58` | 其中**并发上限**类（上限 7→5） | 6 |
| `01:58:59` – `02:23:43` | **402 `Insufficient Balance`** | 13 |
| `01:58:59` – `02:26:48` | **`model circuit is open: deepseek`** | 85 |

该窗口内 run 的终态：**`succeeded` 180 / `failed` 101**。

**结论必须写准，v3.3 在这里写错了三点**：

1. **死因是账户余额耗尽，不是并发档位不匹配。** 上限低于请求并发只持续了 6 秒（`01:58:53`–`01:58:58`），而 402 与随后的熔断持续了 25 分钟。平台侧的行为是**正确的**：它把限流识别为可重试、按供应商的 `Retry-After` 退避、接线熔断、把原因记成 `MODEL_RATE_LIMITED` / `MODEL_APISTATUSERROR`，而不是假装成功——**这一半 v3.3 说对了**。
2. **`tier-10` 并非「不可运行」，因此把它从负载计划里删掉是错的处置。** v3.3 建议「换一个有相应配额的账户」或把该档位列作已知约束；实测证明**不需要换账户**——**在同一个账户上充值之后**，`tier-10`（并发 10）在**当前构建**上重录：**60/60 `succeeded`，168.2 秒**（2026-09-30 `16:20:50`–`16:23:38`，`evaluation/load/replays/_batch.json` 逐字留档）。**「不可运行」这个结论把一个余额问题记成了一条容量约束**，而它会直接导致一个错误的负载计划：删掉一个本来能跑的档位。
3. **它也不是「可以忽略的，因为不可运行」——它是「必须预置的账户条件」。** 正确的处置是：**负载批次开跑前确认账户余额充足**，若中途耗尽，那一批的观测**不可用**（不是「平台在负载下失败」）。这一条现在写进「边界与下一阶段」的阶段 B 执行条件。

**顺带地，这批旧数据仍然是 R3 的回溯佐证**：同一批运行，原因在 `run_events` 里一字不差，而 `agent_runs.error` 是 `NULL`。

**这一节值得单独记一笔的原因**：v3.3 的归因**不是编造的**——它读到了真实的事件、引用了真实的报文，只是**在一个词上截断了**，而那个词之后的半句（`based on your remaining balance` / `top up your balance`）正是全部答案。**一个被截断的引文可以支撑一个完全错误的结论，而且看起来同样有据。** 这也解释了本轮为什么要**重新测量而不是重读结论**：v3.3 的表格里每一个字都是真的，错的是从它们得出的那一步。

### 九、v3.4 的四组探针（**先探针，后付费**）

阶段 B（四份语料全量重录）实测成本约 **$5.54 ≈ ¥40**。在花这笔钱之前，本轮先用**四组便宜探针**回答一个问题：**重录之后会不会还是红的？** 四组探针合计花费约 **$1.0**，并且各自都产出了可交付的结论——其中一条（A3）**推翻了既有文档的归因**。

**A1 —— 旧质量语料里到底有没有 FAIL？**（纯离线，$0，无模型调用）

把 200 条旧观测按**版本切片**分别单独判定，绕开同源性检查，看观测本身的质量：

| 切片 | 观测数 | PASS | **FAIL** | BLOCKED（该案例未运行） | 退出码 |
| --- | --- | --- | --- | --- | --- |
| `7a6e675+dirty(121)` | 148 | 148 | **0** | 52 | 2（观测不足） |
| `7a6e675+dirty(120)` | 37 | 37 | **0** | 163 | 2（观测不足） |

**结论：整份旧语料里一条 FAIL 也没有。** 退出码是 2 而不是 0/1，只是因为每个切片只覆盖案例清单（200 条）的一部分，其余案例「未运行」——这正是「观测不足」的定义，不是失败。**这条排除了一个本来会误导阶段 B 的猜想**：如果旧语料里存在 FAIL，那么重录之后它大概率还在，阶段 B 就会变成「花 ¥40 再看到一次红」；实测没有 FAIL，说明质量语料的问题**只在同源性**（R5 已修），不在答案质量。

> 切片数（148 / 37）与 v3.3 文档里写的 158 / 38 不同，**是实测值，不是笔误**：差额来自本轮 A2 探针用新观测覆写了其中 13 条（它们因此换了版本号）。本行按**当前磁盘状态**记录，并且这正是 R5 要消灭的那种漂移。

**A2 —— 当前构建上的活体质量探针**（12 条 + 1 条复跑 ≈ **$0.24**）

在**当前构建**上真跑 13 条质量案例，挑的是每一类问题各 3–4 条（可答 / 版本冲突 / 必须拒绝访问 / 证据不足）：

| 项 | 实测 |
| --- | --- |
| 终态 | **13/13 `succeeded`** |
| 评审决定 | **13/13 `passed`** |
| 引用条数 | 5–7 条/例 |
| 覆盖的问题类型 | `answerable` ×3、`version-conflict` ×3、`must-refuse-access` ×3、`insufficient-evidence` ×4 |

其中 `Q-200`（语料未覆盖的问句）**修复前是 `failed`（4.1 秒，`critical_error`）**，修复后 `succeeded / passed / 7 引用 / 33.9 秒`——**R4 的活体证据**。

**A3 —— 负载 `tier-10` 在当前构建上真的跑不动吗？**（**$0.72**，见「八」）

| 档位 | 并发 | 次数 | 终态 |
| --- | --- | --- | --- |
| `tier-1` | 1 | 20 | **20/20 `succeeded`** |
| `tier-5` | 5 | 40 | **40/40 `succeeded`** |
| `tier-10` | **10** | 60 | **60/60 `succeeded`**（168.2 秒） |

**磁盘上 120 条负载观测全部 `succeeded`，一条 `failed` 都没有。** v3.3 记的 `tier-10` FAIL 在当前账户上**不可复现**——因为它是余额耗尽，不是并发上限（成因见「八」）。

**A4 —— 安全语料的 2 条 `unexercised` 能不能补上？**（≈ **$0**，见下）

72 场全量重录（`--skip-mutations` 之外的完整执行）：**`passed` 245 条、`unexercised` 2 条**。那 2 条已定位并关闭，见下一小节。

### 十、安全语料的 2 条 `unexercised`：是什么，为什么，怎么关的

**是什么。** 两条分别是 `SEC-OUTBOX-02`（「清理只退休已投递且已过龄的行」）与 `SEC-TENANT-06`（「清理的删除保持在租户范围内，且一个租户的失败不波及其他租户」），各自有一条证据被记为 `unexercised`，note 为 `1 skipped`。

**为什么。** 追到测试层，两处 skip 的原因是同一句：`need --run-docker option to run`。它们分别是 `test_the_sweep_retires_delivered_rows_and_keeps_everything_else` 与 `test_the_sweep_leaves_another_tenants_rows_alone`——**真实 PostgreSQL 上的两条断言**：前者验证「只退休已投递且已过龄的行」，后者验证「一次 `DELETE` 的租户谓词确实越不出 RLS 边界」。**它们恰恰是这两个场景里最硬的证据**：其余证据是进程内的脚本化中继（`_ScriptedRelay`），而这两条打的是真库。驱动自己的帮助文本就写着「without it the outbox retention scenarios are recorded unexercised」——**这 2 条不是缺失的能力，是一个没打开的开关**。

**怎么关的。** 只对这两场开了 `--run-docker` 重录一次：

```text
running 3 pinned tests ...
running mutation experiment scripts/mutate_outbox_retention.py ...
{ "recorded_scenarios": 2, "evidence_outcomes": { "passed": 8 },
  "run_docker": true, "mutations": true,
  "deployed_revision": "cf08ac8a7b731054e492ed81ba5f3164dc381863+patch(0ce86f5d19bc)" }
```

`SEC-OUTBOX-02` 5 条证据、`SEC-TENANT-06` 3 条证据，**8 条全部 `passed`**，全语料 **0 条 `unexercised`**。

**必须说明的代价（否则这一段就成了「顺手把数字修好看了」）**：这次补录用的是**新的版本号格式**，而另外 70 场是**旧格式**，于是安全语料从「同源」变成**跨两个字符串**，安全 gate 因此从 v3.3 的退出 0 变成**退出 3**。这个 3 是**如实的结果**，不是回归——它正是 R5 描述的机制在新旧交界处的显形，而阶段 B 会用一次完整重录把 72 场收敛回同一个字符串。**把 2 条 `unexercised` 换成 1 个跨版本的语料，这笔交换划算，因为它换掉的是「一条没有跑过的断言」而留下的是「一个会被阶段 B 自动解决的问题」。**

### 十一、四门 gate 在本轮的取码与耗时

| gate | 命令 | 退出码 | 成因 |
| --- | --- | --- | --- |
| 验收 | `gate_phase7_acceptance.py --check --replay-only` | **0** | 28 例 PASS / 0 FAIL / 0 BLOCKED，回放同源 |
| 质量 | `gate_phase7_quality.py --check --replay-only` | **3** | 语料跨 6 个旧格式版本（R5 前的产物） |
| 负载 | `gate_phase7_load.py --check --replay-only` | **3** | 语料跨 2 个旧格式版本 |
| 安全 | `gate_phase7_security.py --check --replay-only` | **3** | 70 场旧格式 + 2 场新格式（见「十」） |

**三次拒绝的报错文案都指名了要重跑哪个驱动**（`Re-run scripts/verify_phase7_*.py on the deployment you mean to describe`）。这句文案在 R5 之前是**不可执行**的（重跑必然产生新版本号），R5 之后它是一条**真指令**。

## 已知未关闭缺陷

每条含复现路径。**「已知未关闭缺陷」与「未评估」分列，不得互相替代。**

### 发布阻断

**v3.4 没有发布阻断缺陷。** 历史上出现过三条发布阻断（D1、D14、D15），一条跨版本记录的产品缺陷（D3），两条「是否阻断需裁定」项（D16、D17），以及 **v3.3 新确认并关闭的三条（R1 路由分叉、R3 失败原因不入 run 行、G1 判定器不校验证据同源性）**，加上 **v3.4 新确认并关闭的两条（R4 规划器在语料外问句上产出残缺计划、R5 版本号自指）**——**十一条现已全部关闭**，关闭证据分别见「本阶段已修复并锁定」表。

**另有十条于 v3.4 移出缺陷清单，但它们一条都不是本轮修的（D4–D13）**：v3.4 把「其他已知未关闭缺陷」表里的十条**逐条拿回代码里核对**，结果是**八条早已修复**（D4、D5、D6、D9、D10、D11、D12、D13，各有锁定测试）、**两条的「影响」描述本身不成立**（D7 的顺序耦合被断言词汇证伪、D8 的投递半已部署且有 98 条 `published` 数据）。其中 D5 的两半**在 `688dd90`（2026-09-24）就已实现**，本文件从 v3.0 起连续三个版本把它抄成「仍未做」。**这条与上一条必须分开陈述**：R4/R5 是「本轮修好并验证」，D4–D13 是「本轮核对并确认早已修好或本就不是缺陷」——**把后者说成前者，等于虚报了一轮工作量，也正是这份文档一直在防的那种失真。**逐条证据见「其他已知未关闭缺陷（v3.4 逐条回读后重写）」。

**R4 曾被考虑列为发布阻断，最终判为「阻断体验但不阻断发布」，理由必须写清**：它让**语料未覆盖的**用户问句**大多数时候得不到任何答案**（8 次采样里 6 次失败），而这类问句正是真实用户最可能提的——从用户视角看它比很多 D 级缺陷更严重。但它没有让任何**已覆盖**的路径出错，28 条案例也没有一条落在它上面，因此按本文件的判定规则不构成发布阻断。**这个区分不是文字游戏**：它意味着 R4 **不会**被 28 条案例的 PASS 覆盖，只能靠探针发现——而它确实是探针（A2 的 `Q-200`）发现的。

**但「没有发布阻断」不等于「可以发布」**，这句话在退出码为 0 时必须说清——0 容易被读成「一切就绪」：

- **v3.4 逐条回读之后，清单上没有一条「已知存在、尚未修复」的代码缺陷**（D4–D13 十条全部重新核对：八条早已修复、两条的「影响」不成立）。**但这句话的强度必须说准**：它说的是**这张表**被清空了，**不是**「平台没有缺陷」——v3.4 只回读了这十条，**没有**回读「已关闭清单」，也**没有**任何机制保证下一轮不会再从别处冒出一条。**v3.3 与更早的版本在这里的做法是继续抄旧账**（把八条早就修好的缺陷当成待办列了三个版本），**那份账的可信度不是这一轮建立的，是这一轮第一次被检查的**。
- **`identity` 以外的大多数模块只有个位数断言**（见「五、覆盖表」的诚实读法）。PASS 28 说的是「这 107 条断言成立」，不是「平台被充分验证」。
- **未评估项少了一项，但只剩一项**（见「未评估」）：v3.4 关闭了「安全语料的 2 条 `unexercised`」与「负载档位是否可运行」两个缺口，**业务质量 200 条与安全/故障 ≥40 场景仍未评估**。
- **本轮的 FAIL 没有消失，只是被转移到了一个更该看见的地方**：质量、负载、安全三门 gate 都是**退出 3**，且都是**旧证据自带异质性**（成因见「十一」）。**把它们的红说成绿是本文件最该避免的事**；正确的读法是——**G1 让一个原本隐藏的问题（语料横跨多个版本）第一次变得可见，而 R5 让「重录一次就能修好」这件事第一次成为真的**。v3.3 记的负载 gate「退出 1（有 FAIL 判定）」在 v3.4 **已不存在**：那个 FAIL 的成因（余额耗尽）查清后，该档位在已充值账户上重录为 60/60 succeeded。

**关于「一条缺陷从「未关闭」移出」的纪律，v3.4 再确认一次（这一条现在有了一个新用途）**：v3.1 把 D16/D17 按已知未关闭缺陷记录并让 gate 以退出码 1 结束（不用「非阻断」的名义让红色消失）；v3.2 **没有取消那两条记录，而是把它们修掉了**。v3.3 同样**没有**取消任何记录：R2 因为**复现不出来**而留在「未复现的观察」里，**没有**被写成「已修复」。

**v3.4 一次性移出了十条，因此这条纪律必须接受一次更严的检验**——「移出」在本文件里一直是**最容易被滥用的动作**（它看起来像进展）。区分标准只有一条，本轮据此逐条执行：

| 移出的理由 | 是否算本轮的进展 | 本轮适用 |
| --- | --- | --- |
| **修复了它**（有新的源码改动 + 变异验证） | **是** | 无（D4–D13 本轮**没有改过一行源码**） |
| **核对了它，发现早已修复**（指出具体提交与锁定测试） | **不是**。它记的是「账本错了」，不是「平台变好了」 | **D4、D5、D6、D9、D10、D11、D12、D13** |
| **核对了它，发现「影响」不成立**（该缺陷的原始描述本身有误） | **不是**。它记的是「这条描述错了」 | **D7、D8** |
| **复现不出来** | **不是**，且**不得移出** | 无（R2 仍在「未复现的观察」） |

**这张表的意义在于：本轮十条移出里，「修复了它」那一行是空的。** 如果这份文档只写「缺陷清单从 10 条降到 0 条」，读者会合理地推断出**这一轮修了十条**——**那个推断是错的，而它错的方式正是本文件一直在防的那种：让一个数字的增长看起来像一次行动的成果。**

### G1 + R5 带来的一个必须交代的后果：离线 CI 半暂时无法恢复，**但恢复路径现在存在了**

`cf08ac8` 这次提交的标题是「暂时移除 `phase7-offline-gates` 任务」。把该任务恢复回 CI 之前，必须知道它现在会**立刻变红**——不是配置问题，是**证据问题**：

| gate | 按当前证据跑 `--replay-only --check` 的结果（v3.4 实测） | 恢复 CI 前必须先做 |
| --- | --- | --- |
| 质量 | **退出 3**（200 条观测横跨 6 个旧格式版本，`--force` 也到不了这里） | **在一次记录活动中重录业务质量 200 条**（P7.6.6，实测成本约 **$3.66**） |
| 安全 | **退出 3**（70 场旧格式 + 2 场新格式，见「十」） | 同一次记录活动中重录 72 场（≈ **$0**，需 `--run-docker`） |
| 负载 | **退出 3**（121 条横跨 2 个旧格式版本） | 同一次记录活动中重录 120 次（约 **$1.44**，**开跑前确认账户余额充足**，见「八」） |
| 验收 | 退出 0（单一版本、当前） | 无需前置动作（但为保持四份语料同源，建议一并重录，约 **$0.44**） |

**本轮把这件事的性质改变了，这一点比四门各自的退出码更重要。** v3.3 的表述是「G1 暴露了一个证据缺口，挡住 CI 恢复」——但 v3.3 没有说出的是：**当时那条恢复路径本身是走不通的**。版本号数的是 `git status` 的行数，而重录会把证据写回工作树，所以**重录出来的版本号必然不同于上一份**，四份语料在构造上不可能同源。R5 之后，这条路径第一次成立：**在同一次记录活动中依次录完四份、期间不改源码，四份就会得到同一个版本号。**

**执行条件（缺一不可，都是本轮实测得出的）**：

1. **四份语料必须在同一次记录活动中录完**，中途不得改动 `src/`、`scripts/`、`tests/`。改任何一个文件都会让版本号变一次，之前录的语料随即变成「另一个版本」。
2. **负载批次开跑前确认账户余额充足**——余额耗尽会让该批观测不可用（不是「平台在负载下失败」），且**熔断打开后的 85 次调用会被即时拒绝**，那一批等于白跑（见「八」）。
3. **安全批次必须带 `--run-docker`**，否则 outbox retention 两类场景会再次被记为 `unexercised`（见「十」）。
4. **记录完成后不要立刻改源码**，直到四门 gate 跑完并取码。**本文件的撰写本身不违反这一条**：`docs/` 与 `evaluation/` 已被排除在指纹之外。

**因此本轮的结论是：G1 与 R5 都已修复、效果均被实测确认；「离线 CI 半恢复」这件事被一个证据缺口挡住，而 R5 把补上这个缺口的路径从「不可能」变成了「一次记录活动 + 约 ¥40」。** 这个顺序不是拖延——**先恢复 CI、再重录语料，等于让 CI 在一件已知错误的事情上报绿**。

### 其他已知未关闭缺陷（**v3.4 逐条回读后重写**）

**本节此前记着十条「未关闭缺陷」。v3.4 把十条逐条拿回代码里核对了一遍——八条早已修复，两条的「影响」描述本身不成立。** 这与 D5 是同一个失效模式：**不是本轮修的，是本轮终于读的**。因此下面**没有一句「本轮关闭 N 条」**——那会虚报工作量，而虚报正是这份文档一直在防的东西。

| # | 文档此前所记 | **v3.4 回读的结论（含它其实早就修好了）** | 回读证据 |
| --- | --- | --- | --- |
| **D4** | 修订崩溃/限流时**保留已知违规草稿**，评审器随即确定性拒绝（`UNKNOWN_EVIDENCE_REFERENCE`） | **已修（非本轮）**。`_revise_node` 的 `except` 分支改为走 `_fallback_evidence(...)`——与草稿路径在模型调用失败时取的答案**同一个形状**：`status=DEGRADED`、`evidence_refs` 取自证据而非那份丢失的草稿、`failure_code=ANALYSIS_REVISION_FAILURE`，评审器的 `DEGRADED_ANALYSIS` 门据此可正常触发。源码注释逐字描述了这条缺陷（「Marking it DEGRADED relabels the runtime failure as whatever the draft got wrong」） | `src/servicemind/agents/analysis.py:392-418`；专属套件 `tests/servicemind/test_analysis_revision_failure.py` |
| **D5** | 限流（`MODEL_RATE_LIMITED`）触发**确定性降级回退**：分析走降级路径，评审器随即 `ESCALATE + DEGRADED_ANALYSIS`，运行停在人工队列。本文件自 v3.0 起把它记为「修复 7 只改重试的**时机**，不改降级语义，**仍未做**」 | **已修（非本轮），且早在 `688dd90`（2026-09-24）就已修好——本轮回读这条是本节的起点。** 两半：①**等待**——限流不是运输故障，`throttle_wait_seconds()` 把「什么时候可以再问」交给**运行自己的截止时钟**（受 `Retry-After`、总等待上限 60s、以及截止时间余量三重约束）；②**裁决**——降级是**缺失**而非**分歧**，评审器不再一见到就升级，而是**先重规划一次**（`REPLAN`），用完 `max_replans` 才交给人。同时 `ANALYSIS_RATE_LIMITED` 与 `ANALYSIS_MODEL_FAILURE` 分开记录：一个是容量问题，一个是缺陷 | 完整记录（含本轮补做的变异验证：改回「永远升级」→ 1 红、令限流永不等待 → 10 红）见「本阶段已修复并锁定」D5 行；本文件此后三个版本抄写旧结论的成因见「历史口径修正」第八条 |
| **D6** | `result.evidence` 有**两种形状**：supervisor 路径持久化信封，`fast_data`/`fast_knowledge` 持久化裸列表 | **已修（非本轮）**。`evidence_envelope()` 现在是「**一个持久化运行的 `result.evidence` 唯一的那种形状**」，快路径也用它；`JoinedEvidence` 的字段（`tenant_id` + `items`）与信封**逐字相同**，读取方 `domain/evidence.py:310` 按同一个键统一读取 | `src/servicemind/domain/evidence.py:273-294`（含 `EVIDENCE_ITEMS_KEY`）、`:310`；`JoinedEvidence` 定义 `:248-252` |
| **D9** | GLPI 写入**未经 ToolGateway** 统一策略与调用审计（`glpi.append_ticket_followup` 不在工具注册表） | **已修（非本轮）**。`APPEND_FOLLOWUP_TOOL` 已**注册进工具目录**；执行器不再直连 GLPI，而是构造 `ToolCall(capabilities={APPEND_FOLLOWUP_TOOL}, tool_name=..., tool_version=...)` 并走 `build_tool_gateway().execute(...)`；审计行里带 `tool_call_id` 与 `policy_decision_id` | `tool_platform/catalog.py:222`、`harness/executor.py:86,151-153`、`tool_platform/providers.py:175-205` |
| **D10** | `memory_events` 缺 `run_id`/`trace_id`，**撤销事件→运行的审计闭环不通过** | **已修（非本轮）**。`MemoryEvent` 现有 `run_id: UUID \| None`，并有一条**明确的语义说明**：`None` 是一个事实（TTL 到期、程序记忆复核、人工审核本就发生在任何运行之外），为它们编造一个 run_id「会变成编造而不是关联」 | `memory/contracts.py:505-509`；专属套件 `tests/servicemind/test_memory_event_correlation.py` |
| **D11** | 执行器回读**只验标记存在**（`verified = marker in html_to_text(followup.content)`），不比对正文 | **已修（非本轮）**。新增 `body_matches(stored, expected_text)`：`normalized_text(stored) == expected_text`，不一致即抛 `ToolVerificationFailed`；源码注释逐字写着旧检查「answers a different question: *this row exists*」，并说明被字段上限截断、被编辑器替换、或在标记处被剪断的正文**都保留标记、都曾通过**。读回用的是**不经过写入路径的独立读取**——提供商可以在响应里回显你给它的正文而落库的是别的 | `tool_platform/providers.py:33-51`、`:175-205`；`tests/servicemind/test_enterprise_subagents.py` |
| **D12** | `set_document_active`（知识版本废止/恢复）**无任何 HTTP 入口** | **已修（非本轮）**。新增 `POST /knowledge/documents:activation`，按 `source_record_id` 寻址，要求 `operator`/`tenant_admin` 角色，找不到时返回 **404 而非 200+0**（注释写明：在租户会话内查，所以「查不到」对调用者不可见的每一个 id 都成立，而不只是真的不存在的那些） | `interfaces/http/knowledge.py:55-80`；`tests/servicemind/test_knowledge_lifecycle.py` |
| **D13** | `scripts/verify_phase2_concurrency.py` 已失效（硬编码 `:8080`、断言返回体为 `waiting_approval`，实际必为 `pending`） | **已修（非本轮）**，且脚本自己把这件事写进了 docstring：旧版「posted to a fixed `127.0.0.1:8080`（**不是本 API 的端口**——8080 是宿主机上另一个服务，请求整个离开了平台）」，并且从 create 响应里读 `action_intent.action_hash`，而那个字段由工作流在 create **返回之后**才写、当时必为 `None`。现在 base URL 取自 `core.settings`，action hash 取自一个**真的走到 `waiting_approval` 的运行** | `scripts/verify_phase2_concurrency.py:1-15`（自述）、`:46`。**注意：本轮只读了源码，没有活体重跑它**——「脚本已被修好」这一点是**从代码读出来的**，见「未评估」 |
| **D7** | **验收套件不可重复**：写案例自增工单 followup，**污染后续只读案例的基线**，案例之间存在**顺序耦合** | **「影响」不成立。** 全部 107 条断言里，与 followup 有关的 14 条**全部是相对增量**——`no_new_followups`、`exactly_one_new_followup`、`followup_body_is_the_approved_content`——**没有一条依赖绝对计数**，每条都在本案例开始时取一次基线。因此重复执行**不会**改变任何一条判定的结果，「顺序耦合」在断言词汇层面就不存在。**残留的是一件事实而非缺陷**：4 条写案例（ACC-10b / ACC-11 / ACC-22 / ACC-23）会在共享工单上累积 followup，这是「对着活体 ITSM 系统做验收」的固有属性，不是可修的错误——**要「根治」就得在跑之前重置 GLPI 状态，而那会让套件变成破坏性的** | `evaluation/acceptance/cases.v1.json` 的断言词汇（本轮实测统计）；`scripts/verify_phase7_acceptance_live.py:896-905` 的 `take_baseline` |
| **D8** | **`tool_outbox` 的 `action.approved` 无消费者**（`RedisStreamConsumer` 全仓零引用）→ 暗示「入队行不被消费」 | **前半句字面为真，后半句的推论为假。** ①**投递半是完整的、已部署、正在运行**：`OutboxRelay(RedisStreamPublisher)` 由 `servicemind-outbox` 入口运行，systemd unit `servicemind-outbox.service` **自 2026-09-24 01:28:13 CST 起一直 active**，每秒 claim 一次、投递成功后标记 `published`、并按 30 天保留期清理。②**没有累积**：实测 `tool_outbox` 在租户上下文下共 **98 行，全部 `published`**（2026-09-23 00:25 → 2026-09-30 15:24），**`pending` / `failed` 各 0 行**。③**真正的残留是范围而非缺陷**：`RedisStreamConsumer`（at-least-once + PEL 恢复）在仓库里**没有订阅者**——下游没有消费者，而核心闭环走同步续跑、**不需要**它。**因此正确的记法是「不得宣称异步消费与恢复已通过」，而不是「有一个待修的消费缺陷」**；为此写一个没人需要的消费者，正是「为了修复而修复」 | `src/servicemind/reliability/worker.py`、`pyproject.toml:86`（`servicemind-outbox`）、`systemctl --user is-active servicemind-outbox`、`tool_outbox` 按 `status` 分组的实测计数、`reliability/outbox.py:24,30`（`STREAM_MAXLEN=100_000` / 保留 30 天，故 Redis 侧亦有界） |

**D5 这条缺陷在修复之后是否又真实发生过——本文档累计观测了三次，三次都是「没有」，而这三次都不能被读成「限流问题已解决」**：v3.1 窗口（服务起始 `12:48:36Z` 之后的全部调用，含批次外 8 次单例重复运行）`succeeded` **558** 条、非 succeeded **1** 条，且那 1 条是 `MODEL_SCHEMA_INVALID`（`data` agent、`T7`、run `0740213d`），`MODEL_RATE_LIMITED` 为 **0**；v3.2 窗口（起点 `2026-09-23T14:19:00Z`）模型调用 **620** 条全部 `succeeded`；v3.3 窗口（`2026-09-30T15:09:54Z`–`15:24:11Z`）**370** 条全部 `succeeded`。**这几组数据只说明「这些窗口内没有再次限流」，不能证明「限流发生时重试一定成功」**——退避分档至今在真实流量中**未被触发过**。而**反面证据就在同一个部署上**：负载 gate 那四条失败运行里 `MODEL_RATE_LIMITED` 是**真实出现**的（见「八」），由并发档位触发。**「零限流」是窗口的性质，不是平台的性质**——把它写成后者，就是拿一段安静的时间冒充一个被证明的修复。

**这次回读有一处方法上的教训，值得单独写下来**：查 `tool_outbox` 时，第一次用 `global_session` 查到的结果是 **0 行**。如果就此收工，本节会写下「表是空的，所以没有累积」——**而真相恰恰相反**：`tool_outbox` 启用了**强制 RLS**（`relrowsecurity=true, relforcerowsecurity=true`），属主身份下一次也看不到，**0 行是被过滤出来的，不是事实**。换 `tenant_session` 才看到那 98 行。**这与 R5、与第六条口径是同一件事的第三次现身：一次看起来成功的查询，可以在什么都没问的情况下给出一个干净的错误答案。**

**因此本清单在 v3.4 之后的状态是：D4–D13 十条已全部回读，其中八条早已修复、两条的「影响」不成立；没有一条是「已知存在、尚未修复」的代码缺陷。** 但**不宣布平台无缺陷**——本轮回读的是**这十条**，不是**已知缺陷这个概念**。

### 本阶段已修复并锁定（不再是缺陷）

| 曾有的缺陷 | 修复 | 锁定测试与实测关闭证据 |
| --- | --- | --- |
| **R4.（v3.4 关闭）语料未覆盖的问句产出残缺计划，运行以 `critical_error` 结束**——8 次采样里 6 次产出的计划没有 Analysis，或 Reviewer 没有 Analysis 可审；规划器只有 2 次尝试，两次都不过就整个运行失败。**历史触发 48 次** | 证据流水线由**确定性策略**补全（`_complete_evidence_pipeline`）：有 Data/Knowledge 就补齐 Analysis（接到全部证据）与 Reviewer（接到 Analysis），Action 挂到 Reviewer 之后；已有则只补依赖边。提示词由「minimal DAG」改为「smallest **valid** DAG」并明文写出强制流水线。**fail-closed 检查保留为后置条件** | 5 条新测试（含 5 组残缺形状的参数化 + 「不改写已正确的计划」+「无法补全的仍被拒」）；**变异验证**：抽掉补全调用点 → 5 条全红，还原转绿。**活体关闭**：`Q-200` 由 `failed`（4.1s）变为 `succeeded/passed/7 引用/33.9s` |
| **R5.（v3.4 关闭）版本号自指**——`deployed_revision` 数是 `git status --porcelain` 的行数，而驱动的证据就写在同一棵树里，于是**记录动作本身改变被记录的版本号**。同一份源码 `cf08ac8` 被记成 5 个版本（`dirty(8/26/56/69/130)`），更早的 `688dd90` 反而记成 `dirty(155)` | 改为对**源码树内容**取指纹：`<sha>` 或 `<sha>+patch(<12 hex>)`；排除 `evaluation/` 与 `docs/`，包含 `tests/` 与 `scripts/`；四端共用一份实现 | 13 条新测试（跑**真实临时 git 仓库**，不是打桩）；**变异验证 3 次全部被杀**（不过滤产物 4 红 / 退回计数 3 红 / 漏 untracked 1 红）。**端到端**：安全驱动重录时横幅打出的即新格式；**四个驱动对同一棵树返回逐字相同的字符串** |
| **D5.（v3.4 核对后确认已关闭，实际关闭于 `688dd90`）限流触发确定性降级回退**——`MODEL_RATE_LIMITED` 让分析走降级路径，评审器随即 `ESCALATE + DEGRADED_ANALYSIS`，运行停在人工队列，而队列里给的两个选项（「接受一份降级分析」或「取消」）**都是对一个平台从未成功提出的问题的回答**。后果是**同一案例在不同时刻结论不同**（ACC-05 曾因此由 PASS 变 `waiting_review`）——**一个瞬时的容量事件改变了语义结论** | **两半，都在 `688dd90`**：①**等待**——限流不是运输故障，不能按运输故障重试。`throttle_wait_seconds()` 把「什么时候可以再问」交给**运行自己的截止时钟**（单次调用的超时按秒计，而限流窗口经常比它长，于是重试在窗口内空转、失败落在离原因很远的地方）；等待受三重约束：服务方的 `Retry-After` 提示、总等待上限 `_ANALYSIS_THROTTLE_MAX_WAIT_SECONDS=60`、以及截止时间减去后续阶段所需的余量。②**裁决**——降级分析是一种**缺失**而不是一种**分歧**，因此评审器不再一见到就升级，而是**先重规划一次**（`ReviewDecision.REPLAN`），用完 `max_replans` 才交给人，与 `dispatch_barrier` 对失败任务的既有规则一致。同时 `ANALYSIS_RATE_LIMITED` 与 `ANALYSIS_MODEL_FAILURE` 分开记录：**一个是容量问题（重跑即可），一个是缺陷** | **锁定测试**：`tests/servicemind/test_phase7_rate_limit_degradation.py` **18 条**（退避调度、`Retry-After` 三种形态、按运行截止时间钳制、总等待封顶、空补全与 schema 违规的区分「空补全重问、schema 违规带反馈重问」），`tests/servicemind/test_phase4_reviewer_citations.py` 2 条（先重规划 / 预算用尽后升级）。**本轮补充的变异验证**（此前未做过，这也是为什么这一条此前被误记为「未做」）：把裁决改回「永远升级」→ **1 条红**；令限流永不等待 → **10 条红**；还原后 28 条全绿。**范围说明**：本轮**没有**制造一次真实的限流来做端到端复现（那需要让账户进入限流状态），因此关闭证据是**单元级的**；真实流量中 `MODEL_RATE_LIMITED` 的四次出现记录在负载档位与「八」中 |
| **D3.（v3.2 关闭）分析师与评审器的证据集不一致**——评审器看到的证据是**分析所见集合 ∪ 分析被裁掉的集合**，即分析代理被按它**从未见过**的材料判定。v3.1 逐 id 实测（run `96435a80`）：分析选中 9 裁 2、评审选中 11 裁 0，且 **11 = 9 ∪ 2** | 分析节点把信封实际交付的证据 id 写进 `Phase3State.analysis_evidence_ids`（`supervisor_workflow.py:1153`）；评审器构建上下文时以这一份为准（`phase5_governance.py:517-526`），并并入分析实际引用过的 id。`recorded is None` → 退回全集（stub 分析路径） | `test_the_reviewer_is_judged_against_the_evidence_the_analyst_was_shown`、`test_a_cited_row_the_analyst_was_not_shown_still_reaches_the_reviewer`、`test_the_analyst_delivery_is_what_the_reviewer_is_then_held_to`（图级）；**变异验证 3 次全部被杀**。**实测关闭（同一读法，v3.2 run `d4f2736c`）**：分析选中 9 裁 2、评审选中 **9** 裁 0，**评审集 − 分析集 = ∅**。评审的证据集从 11 **降到** 9（判定对象变小），而 ACC-03 在这 9 条上仍然 PASS |
| **D16.（v3.2 关闭）ACC-03 的诱饵引用判定是间歇性的**——`acc03-root-cause-not-the-decoy` 判「根因 claim 的引用集合里有没有诱饵」。v3.1 同一版本重复 5 次得 **3 PASS / 2 FAIL**：根因结论 5/5 一致（设备重绑），波动的是「诱饵是否被挂到根因 claim 上」（2/5）。模型用诱饵做的是**排除**判别，这是正确的 | **两侧都改**：①产品侧——分析提示词新增条款，明确「用来排除候选的文档不构成所命名根因的证据，不得出现在根因 claim 的 `evidence_refs` 里，排除理由写进 `statement` 或 `assumptions`」（`agents/analysis.py:127`）；②判定器侧——`RequiredFact` 新增正向断言 `must_cite`（`acceptance.py:123`、`acceptance_grader.py:786-799`），ACC-03 的 `root-cause` fact 要求**必须**引用 `KB-GLOBEX-VPN-MFA-REBIND` | `test_the_analysis_prompt_forbids_grounding_a_root_cause_in_what_it_ruled_out`（产品侧逐字条款）、`test_a_fact_can_require_the_document_it_must_be_grounded_in`（三个子案：只引手册 PASS / 引入诱饵 FAIL / 只引工单 FAIL）；**变异验证**（删掉判定块 → 后两个子案转红）。**实测关闭**：v3.2 共 6 次观测，根因 claim 的 `evidence_refs` **6/6 逐字为 `['ev-bde3453baad0a7ab', 'ev-1608ee49246cc7ef']`**（v3.1 为波动），诱饵出现于根因 refs 的比例从 **2/5 降到 0/6**，而诱饵仍每次都被检索、被 `incident_fact` 引用（**判别力未削弱**）。**约束是变多的**：`must_not_cite` 之外**追加**了 `must_cite` |
| **D17.（v3.2 关闭）ACC-06 的「专用超预算载荷」实测不超预算**——首轮信封只用 **10021/10720**、余额 699；重复 5 次**全部零裁剪**（v3.0 的那 2 条来自修订轮，而修订轮是否发生取决于评审器是否放行）。**该断言在多数运行下不产生信号** | 裁剪断言改接**确定性探针**（`verify_phase7_acceptance_live.py:1349`，分派 `:1980`）：固定 `max_input_tokens=2000 / system_reserve=0 / output_reserve=0`，1 条 required POLICY 控制行 + 6 条各 799 token 且**内容互异**的 EVIDENCE 行；断言「至少一条证据行被选中 **且** 至少一条被丢弃并写明非空理由」。实跑侧保留为两条恒真断言（清单非空 + 裁剪后仍到达成功终态），**覆盖面只增不减** | ACC-06 的 `acc06-pruned-with-reason` 现为 `probe_outcome` 判定；**实测**：`tokens_used=1621`（23 + 799 + 799）、`tokens_pruned=3196`、`selected=[probe-evidence-0,1]`、`dropped_with_reason=[probe-evidence-2..5]`、reason 均为 `token_budget_exceeded`、`outcome: passed`。预算、逐条 token 数与逐条 reason 全部留档，**可逐项复算** |
| **D15.（发布阻断，v3.1 关闭）跨工单程序记忆无法形成模式**——模式身份里含「随证据而变」的建议正文（两张同构工单看到的证据按构造不同，建议极性相反，`pattern_key` 必然不同）；且被隔离的 episode 永远无法充当佐证（`auto_activation_confidence=0.90` 使新记忆默认落 `quarantine`，而佐证查询只认 ACTIVE，形成自锁）。**用户裁定「完整修：身份解耦 + 解除自锁」** | 见上方「v3.1 的根因修复」D15-a（身份 = 分类 + 建议组 + 已填字段名，正文移出身份，改为存进记忆内容）、D15-b（新增 `MemoryRecord.corroborable_at()`，ACTIVE ∪ QUARANTINE 可准入，REVOKED/SUPERSEDED/EXPIRED 仍不可，服务读路径保持只认 ACTIVE）、D15-c（**Postgres 侧 SQL 预过滤**从固定 `status IN (ACTIVE)` 改为覆盖可佐证状态集——此前它比领域谓词更窄，内存仓库没有 SQL 层，故单元测试全绿而部署路径仍返回空）、D15-d（ACC-12b 的 `activate-memory` 步骤补 `"as_subject": "globex-approver"`，该端点此前因缺主体返回 403） | `test_recommendation_wording_does_not_split_a_pattern`、`test_the_recommendation_shape_is_part_of_the_identity`、`test_a_quarantined_episode_corroborates_but_a_revoked_one_does_not`、`test_two_runs_of_one_ticket_never_propose_a_procedure`（same / conflicting 两参数）、`test_the_pattern_sql_prefilter_never_drops_a_corroborable_status`。**实测关闭**：ACC-12b 由 BLOCKED 转 **PASS**（quarantine → 人工激活 → active 全程跑通） |
| **D1.（发布阻断，v3.0 关闭）信封把被按来源上限拒绝的行永久丢掉**——该上限按 rank 顺序扣费，排位靠后的知识手册即使更相关也回不来；实测信封只用 6412/10720 却报告「被上限拒绝」 | 修复 A：第二趟按广度优先序回收 `cap_deferred` 行 | `test_phase5_governance.py::test_a_capped_bulk_row_does_not_take_the_room_it_was_refused_from_memory`、`::test_a_capped_row_gives_way_sooner_than_the_channels_ranking_below_it`、`test_capping_the_bulk_channel_is_what_delivers_a_realistic_procedure`。**实测关闭**：ACC-03 所依赖的正确手册以 `reclaimed_from_source_cap` 进入分析信封，该案例由 FAIL 转 PASS |
| **D14.（发布阻断，v3.0 关闭）ACC-03 的 `question` 与它自己的工单夹具描述的不是同一件事** | **用户裁定**：改 `cases.v1.json` 中的 `question`，使之与工单夹具的故障点一致 | ACC-03 的 6 条断言全部 PASS（`acc03-root-cause-not-the-decoy`、`acc03-similar-doc-retrievable`、`acc03-password-accepted`、`acc03-mfa-failed`、`acc03-device-changed`、`acc03-terminal-succeeded`） |
| **错误文本头端裁剪使失败不可诊断**（ACC-07 的实测成因） | 修复 6：`bounded_error_text` 保留两端 | `test_foundation_errors.py` 3 条 + 源码级契约断言；变异验证（换回 `str(exc)` 的 `[:1000]` 头端切片即变红） |
| **限流后 100 毫秒就重放，等于没重试** | 修复 7：按失败类型分档退避 + 遵守 `Retry-After` + 钳制在调用自身超时内 | `test_phase5_governance.py` 4 条；变异验证（换回单一 0.1s 档即变红） |
| **分析信封重复携带 `output-schema`**（分析 2985 token = 信封 28%；评审那份还携带从未被请求的 `ReviewResult`） | 修复 C：生产与评估两侧同步移除 | `phase5_governance.py:422-447` 的逐字理由 + `evaluation/memory.py::_delivery_envelope` 的对应注释 |
| **D2. 自引用回路**：平台把自己写的评审 followup 在后续运行中重新纳入证据池，因来源为 GLPI（权威 100 > 知识 80）而排在最前，形成自我强化。实测两条这样的 followup 占掉分析代理 4921 个证据 token 中的 3774 | `domain/evidence.py` 新增 `self_authored_marker()` / `is_self_authored()`，`agents/data.py:370` 在构建证据时按它区分「工单记录」与「平台自己的分析」 | `tests/servicemind/test_data_bounds.py`（识别 + **误识别**：`[ServiceMind run=…]` 缺 action 段、action 段不足 16 位 hex、纯 run id 都不得判为自产）。**v2.0/v3.0 均已实测消失** |
| **`observation_digest` 跨进程不可复现**：`ObservedSubject.roles` 是 `frozenset`，`mode="json"` 按哈希序转出，`PYTHONHASHSEED` 逐进程随机化 | `acceptance._canonical()` 把无序容器按自身规范渲染排序后再摘要 | `test_the_observation_digest_does_not_depend_on_the_hash_seed`（三个不同种子起子进程比对同一份回放） |
| 分析提示词**从不索要** `root_cause_hypothesis`，而评审器按该类型判定 | 补入索求与 `unresolved_questions` 兜底 | `test_analysis_claim_entailment.py::test_the_analysis_prompt_asks_for_the_claim_type_the_reviewer_grades` |
| **判据对建议类 claim 过严**（假阴性） | 判据表按 claim 类型放宽；`REVIEW_POLICY_VERSION` → `v4` | `test_semantic_judge_verdict.py::test_the_judge_is_told_that_the_bar_depends_on_the_claim_type`（断言**共享文本**而非散文措辞） |
| **判据把忠实改写当反证**（假阳性，ACC-23 复现 2/2） | judge 系统提示新增「先读原文、引出矛盾段落」与「忠实解释优先」；`REVIEW_POLICY_VERSION` → `v5` | `test_semantic_judge_verdict.py::test_the_judge_must_read_a_claim_against_the_text_before_calling_it_inverted` |
| 中文知识类问句被「是什么」劫持到数据快路径 | 词表修复 | `test_phase3_router_planner.py` 3 个锁定测试 |
| `RequiredFact` 判定「措辞」而非「命题」 | `all_of` 概念组 + grader `states()` | `test_a_concept_group_fact_survives_word_order` 等 5 个测试 |
| gate 报告的表格渲染吞掉退出码；gate 脚本改 `sys.path` 破坏结构契约 | 两处修复 | `test_enterprise_structure_contract_is_closed` |

## 未评估

仅描述**尚未执行的验证**，不构成任何缺陷结论：

- ~~**P7.6.5 安全与故障场景**（≥40 条）~~ **语料已建成并已录制（v3.1 起共 72 条，含本轮新增的 2 条真 PostgreSQL 证据），但结论仍为未评估**：安全 gate 当前的取码是 **3**（语料横跨 70 条旧格式 + 2 条新格式），**拒绝判定**。**「语料存在」不等于「安全结论成立」**——单版本重录之前，这 72 条不支撑任何整体结论。
- **P7.6.6 业务质量集**（200 条 = 120 可答 + 40 证据不足 + 20 版本冲突 + 20 必须拒绝访问）；端到端 Reviewer 可答率在同一批次**用真正跑完的结果重新测量**，以取代 `0.275` 检索分数阈值代理口径。**v3.4 补充**：质量 gate 当前取码 **3**；本轮的 A1 探针把既有 200 条**按版本切开离线重判**，四个切片**零 FAIL**（148 PASS / 0 FAIL、37 PASS / 0 FAIL，其余为观测不足退出 2）——**这排除的是「重录之后多半还是红的」这个担心，不构成任何通过结论**。
- **P7.6.7 负载**（1/5/10 并发探索后冻结门槛，声明为测试档位而非已认证容量）与**长稳/故障注入**（需独立环境）。**v3.4 补充**：负载 gate 当前取码 **3**；本轮用 `tier-10` 单档探针（阶段 A）确认该档在已充值账户上可跑完，**未**据此改动任何门槛。
- **P7.0–P7.5 本身**（规划中，未实现）
- **ACC-18…ACC-23 之外的撤权场景**：本轮补齐的六类是**已执行的**；仍未执行的包括长时间（跨 checkpoint 重启）撤权、撤权与限流叠加、撤权期间核验器中途不可达
- **D15 修复之后的跨工单程序记忆端到端**：v3.1 实施并实测跑通一次，v3.2 的全量批次又跑通一次，**合计 2 次、2 次都 PASS**。但这**仍然不是稳定性测量**——「两条 episode 归入同一 `pattern_key`」在真实模型下的稳定率未评估（v3.0 的同类判定正是因为只跑一次才把结构性问题误读成巧合）。重复运行 ACC-12b N 次并统计同键率，属于未做的验证。
- **D15-b 放宽佐证可见性之后的安全后果**：本轮只断言了「REVOKED / SUPERSEDED / EXPIRED 的 episode 仍不可佐证」这一**负例边界**（`test_a_quarantined_episode_corroborates_but_a_revoked_one_does_not`）。「隔离舱记忆可佐证」对**记忆污染攻击面**的影响（例如反复提交同类工单以诱导平台生成程序记忆）**未评估**——本轮没有做任何对抗性投喂。
- **未触发的升级条件**：D11 的升为必修条件本次未触发，故 D11 仍按未升级处理——这**不等于**它已被验证为安全

**v3.2 新增的未评估项**（同样是「尚未执行的验证」，不是缺陷结论）：

- **D16 修复后「诱饵不再被挂到根因 claim 上」的稳定率**：6/6 一致，但**样本量是 6**。更大的样本（例如 20 次）下是否仍为 0/20，未评估。**本文件只声称「6 次一致」，不声称「永不发生」。**
- **`must_cite` 对根因之外的其他事实类型的适用性**：本轮只给 ACC-03 的 `root-cause` fact 加了 `must_cite`。其他案例的 `required_facts` 是否也该有「必须引用某文档」的正向约束（从而各自堵住自己的空满足路径），**未评估**。
- **D3 修复后的分析—评审一致性只逐 id 测过 ACC-03**：其余 27 条案例没有做同样的逐 id 比对。**「本轮的 PASS 全部跑在一致的证据集上」这一点是被推断的，不是被逐案例测量过的**——虽然修复位于公共路径（`build_context` 的评审器分支），对所有案例同样生效。
- **实跑信封在什么条件下真的会超预算**：D17 的探针是**构建器级**的确定性验证，它证明「预算规则生效」。它**不**回答「活链路的 ACC-06 载荷要到什么规模才会超出 10720」——那个问题既没有修也没有测，只是**不再被那条断言依赖**。
- **D3 修复对「评审器证据集变小」的下游影响**：评审器现在少看几条（ACC-03 是 11 → 9）。其余案例里评审器少看多少、是否影响任何一条断言的判别力，**未逐案例测量**。

**v3.3 新增的未评估项**（同样只是「尚未执行的验证」，**不是**缺陷结论；其中两条尤其容易被误读成「已通过」，故单列）：

- **重录之后那三条语料是否可用——完全未评估。** 本轮 G1 只判定了「四个 gate 在读证据之前会拒绝跨版本的批次」。**一条语料都没有重录。** 因此「质量 200 条 / 负载四档 / 安全场景在单一版本下重新录完之后能不能通过」既没有修也没有测。当前可说的只有一件事：**它们以现在的形态不能被判定**（v3.3 取码：质量退出 3、负载退出 1；**v3.4 取码：质量/负载/安全均为 3**）。**v3.4 的 R5 没有改变这一条**——它改变的是「重录是否做得到」，不是「重录之后过不过」。
- **负载档位调整后是否通过——未评估。** ~~本轮查清了 `tier-10` 不可运行的成因（服务方并发上限为 5，见「八」）~~（**该成因归因已于 v3.4 推翻**，见「八」与「历史口径修正」：并发上限是**余额派生**的，批次实际死于 **402 余额不足**；在已充值账户上 `tier-10` 实测 **60/60 成功、168.2 秒**）。**「不可运行」这个说法因此不成立**，但**仍没有**用单一版本重跑负载、也**没有**验证四档冻结门槛在新版本下是否通过。**「成因查清」不等于「修好了」，「在探针里跑得动」也不等于「门槛通过」。**

**v3.4 新增的未评估项**（本轮关闭了 R4 / R5 两条缺陷，**没有**把任何一条「未评估」变成「已评估」；以下为新增或状态变化者）：

- **四门 gate 在同一版本下重新录完之后是否取到 0——未评估，且这是阶段 B 的全部内容。** v3.4 结束时四门的取码是**验收 0 / 质量 3 / 负载 3 / 安全 3**：四个驱动现在对同一棵树返回逐字相同的 `deployed_revision`（这是 R5 的关闭证据），但**三条语料里躺着的仍是修复之前录下的旧字符串**。四门给出的拒绝信息现在都**点名了该重跑的驱动**——这条恢复路径是 R5 新造出来的，而**它一次都没有被实际走完**。
- **阶段 B 的实际花费与时长——只有估算，没有实测。** 单条案例的实测均值（质量 $0.0183、负载 $0.01203、验收 $0.4363）来自**阶段 A 的小探针**，用它乘以全集条数得到 ≈$5.54 ≈ ¥40。**探针的单价能否线性外推到全集，未评估**——按已有的三次全量记录（v3.0/v3.2/v3.3 的验收批次）看量级相符，但那是**不同规模、不同时段**的观测，不足以称为验证。
- **重录与「被测版本绑定」之间的时序——未评估。** R5 让版本号不再随录制动作漂移，因此一批重录在理论上可以横跨数小时而仍然同名。**这一条只被 13 条单元测试（含真实 git 仓库）证明过，还没有被一次真实的、跨越数小时的四语料录制证实。**
- **安全语料由 70 条旧格式 + 2 条新格式组成这件事的下游影响——未评估。** 本轮把 `SEC-OUTBOX-02` 与 `SEC-TENANT-06` 用 `--run-docker` 真跑了一遍（8/8 证据通过，语料范围内 `unexercised` 归零），**代价是安全 gate 的退出码由 0 变为 3**——**这是如实记录的代价，不是回归**。全量 72 条在单一版本下的行为，要等阶段 B 重录之后才知道。
- **R1 路由修复的稳定率——未评估。** 金标 107 例 `accuracy 1.0` 是**离线分类器**在固定金标上的测量；对话入口（浏览器探针）只测了一次。**同一问句重复 N 次是否仍落到 incident 诊断路径，未评估。**
- **R2 既未复现、也未排除。** 3/3 活体探针判定正确。按术语纪律第 2 条，这属于**未评估**（样本量 3），**不得**写成「已确认无缺陷」——本文件把它记在「未复现的观察」里，理由即在 R2 一行中。
- **全场景对话链路的重复性——未评估。** 步骤二 28/28 PASS 与 107/107 断言是**单批次**观测：路由、agent、工具、回答、对话入口五类各一次（对话入口那一次还只是**一条**问句）。**「各类场景都走通了一次」不等于「各类场景都能稳定走通」。**
- **R3 修复后的 `agent_runs.error` 只被回溯性地见证过。** 佐证来自负载失败行（修复**之后**写入的新行，但那些行本身是负载 gate 的失败运行）；**本轮没有制造一次新的、受控的失败运行**来确认该字段在成功路径上确实保持为 `NULL`。
- **「已关闭缺陷」那一半清单从未被回读过——未评估。** 本轮把「已知未关闭缺陷」表十条全部重核，**清空的是这张表**（结论与理由见「历史口径修正」第八条）。但本文档另有一份更长的**已关闭清单**（D1/D2/D3/D14/D15/D16/D17、R1/R2/R3/G1，以及本轮的 R4/R5），**它们一条都没有经过同样的回读**。R4/R5 的关闭证据是**本轮新造的**（可复现、可变异验证），G1/R1/R3 的关闭证据部分来自活体复跑——**但这不构成一份「整表重核」**。**「未关闭表被清空」不能读成「缺陷清单整体可信」**：恰恰相反，本轮证明了**一份长期只被抄写、未被回读的清单是可以整表失真的**，而**唯一没有理由认为已关闭清单幸免**。按第八条口径的新规则，这些条目进入下一轮时**必须整表重核或逐条标注「本轮未回读」，不得默认为仍然成立**。
- **D13 的修复只被读出来，没有被跑过——未评估。** 上一轮查出 `scripts/verify_phase2_concurrency.py` 已失效，本轮回读确认它已被修好（base URL 与 action hash 都改为从运行中的配置派生），**但本轮没有活体重跑该脚本**。「脚本已被修好」这一句的证据是**源码**，不是**一次成功的执行**——凡引用该结论者必须知道这个区别。
- **D7 的「顺序耦合」被证伪，但「同一条问句重复 N 次结果是否一致」仍未评估。** 本轮证明的是**验收套件的判定不依赖绝对计数**（14 条 followup 断言全部取相对增量），因此**套件层面**不存在顺序耦合。这**不等于**平台在同一问句上重复运行会给出相同答案——两者是不同的问题，后者见上面「全场景对话链路的重复性」一条。**不要用前者的结论去回答后者。**

## 历史口径修正

`0.275` 在既有文档中已被标注为**检索 top-score 阈值代理**，不是 Reviewer 端到端可答率（见 `evaluation/reports/rag_quality_status_latest.{json,md}`，其 `answerability_signal.is_end_to_end_reviewer_measurement` 为 `false`）。该口径修正**已生效**；端到端的真实测量属于 P7.6.6，在此之前维持 `NOT_EVALUATED`，不得以代理值代替。

`tests/servicemind/test_phase4_index_lifecycle.py` 的模块 docstring 曾声称存在「docker-gated suite covers a real OpenSearch」，但该文件内 `mark.docker` 计数为 0，全部测试走内存假客户端。该 docstring 已在本阶段纠正，真集群覆盖由新增的独立套件 `tests/servicemind/test_phase4_index_lifecycle_live.py` 提供（ACC-17 探针即为其实测）。

**v3.0 推翻的两条推演（均为我此前写下的，现予撤回，凡引用者不得再引用）**：

1. **「把重复的 `output-schema` 项从分析信封里拿掉，就能让被裁的知识手册进来。」** 该推演建立在「总预算不足」这一误判上。实测：已选证据 4462 + 被裁最小行 713 = 5175 > `SERVICEMIND_CONTEXT_EVIDENCE_TOKEN_CAP=5000`，**腾出多少总预算都进不来**。真正让手册进来的是**修复 A 的回收趟**，不是修复 C。
2. **「通道的 cap 是一份到期作废的配额，被拒份额应保留给同通道的后继行。」** A/B 实验（`git show HEAD:src/servicemind/context/builder.py` 对照）证明：该规则在 HEAD 上通过的 `test_capping_the_bulk_channel_is_what_delivers_a_realistic_procedure` 上失败，且它**没有换来它被写出来要换的东西**——ACC-03 在预留存在时仍然失败。已移除并锁定为「被拒份额**归还给信封**（回收趟），而不是预留给同一通道」。

**v3.0 改写的第三条口径（不是推翻，是补正）**：

- v2.0／ACC-12b 案例文本把该案例的成因写成「身份取自模型自由文本，两次独立采样归一化后逐字相等，真实采样下产出率约为零」。**本次逐字对照后必须改写为**：`classification` 与 `recommended_group` 本次**完全一致**；不一致的是 `problem_recommendation` 的**极性**，而极性不同是因为两张工单**按构造看到了不同的证据**。这是一条比「措辞不可复现」更强也更准确的结论——它说明该字段**不可能**通过任何归一化手段变得稳定。详见 D15。

**v3.1 新增的第四条口径（本轮推翻自己的一条隐含假设）**：

- **「ACC-03 的 6 条断言全部 PASS」曾被当作「该案例已稳定通过」。** 这是错的。v3.0 只跑了 **1 次**，而 v3.1 重复 **5 次**的结果是 **3 PASS / 2 FAIL**（D16）。同理，v3.0 观测到的 ACC-06「2 条带理由的裁剪」也是**一次**观测，重复 5 次后为 **5/5 零裁剪**（D17）。**一条通过过一次的断言，不等于一条稳定的断言**——凡涉及模型自由生成的判定，**单次观测不得作为「已通过」的依据**；本文件在 v3.1 起对这类断言一律附重复次数。这条口径同样回溯性地削弱 v3.0 中其他只跑过一次的 PASS，此处如实记录，不溯及既往地重跑全部 28 条。

**v3.2 新增的第五条口径（给第四条划边界，并且承认它还没被用够）**：

- 第四条说「凡涉及模型自由生成的判定，单次观测不得作为『已通过』的依据」。**v3.2 必须给这条加一个边界，否则它会被误用成「任何断言都必须跑 N 次」**：
  1. **对确定性判定，「单次」是充分的。** ACC-06 的新裁剪断言**不含模型参与**——固定输入、固定预算、纯函数求值，同一次全量里它就给出唯一确定的结果（`1621 used / 3196 pruned / 4 条带理由`）。把确定性判定也要求「重复 N 次」，既做不到（它没有分布可言）也不必要。**该口径针对的是「模型这一次恰好生成了什么」，不是「代码在给定输入下算出什么」。**
  2. **对模型判定，v3.2 的 ACC-12b 仍然只有 2 次观测**（v3.1 的 1 次 + 本轮全量的 1 次），两次都 PASS。**这仍然不是稳定性测量**——v3.1 恰恰是因为 ACC-12b 只跑一次，才把「建议正文进了 `pattern_key`」这个结构性问题误读成采样巧合（见 D15）。本轮的第二次 PASS **没有**改变这一点，该案例的重复运行仍列在「未评估」中。**不要因为「现在是 2 次」就把它读成「已经稳了」。**

**v3.3 新增的第六条口径（本阶段最该被记住的一条，它推翻的是我自己此前的读法）**：

- **「一批观测通过了判定器」曾被当作「这个判定器证明了被测平台」。** 这是错的，而且错得**没有任何报错**。实测（2026-09-30）：`evaluation/quality/replays` 里的 200 条观测横跨**四个源码版本**（`7a6e675+dirty(121)` 158 条、`7a6e675+dirty(120)` 38 条、`cf08ac8+dirty(8)` 2 条、`688dd90+dirty(131)` 2 条），而质量 gate 对它报了 **PASS**。**四份不同平台上的观测合在一起，得不出关于其中任何一份的结论**——而 gate 原本无法把「部署的那一版通过了」与「曾经有某一版通过过」区分开，后者恰恰是整个离线半存在的理由。
  1. **`cases_digest` 管不到这件事。** 它回答的是「这份观测是不是对着**这份期望**录的」，与「这些观测是不是**同一个平台**」是两个正交的问题。前者从 v1.0 起就被检查，后者直到 G1 才被检查——**一个已存在的字段被写进了每一份观测、被渲染进了每一份报告，却从未被当作前置条件**。凡是「某字段一直在记，所以链路应该是通的」这类推断，此后都按此例处理：**记录 ≠ 检查**。
  2. **因此「本轮 PASS」的适用范围必须逐条写明被测版本**，这也是本文件从 v1.0 起就在「被测版本绑定」里记录源码 commit 与未提交改动的原因。**那一节不是行政信息，它是结论的一部分。**
  3. **修复的方向是让门关得更早，而不是让红变绿。** G1 落地后，同一份质量语料由 **PASS（退出 0）变成退出 3**。**这是变好了，不是变坏了**——它第一次把「这份语料不能支撑任何结论」说了出来。代价是**已提交的离线 CI 半暂时无法恢复**（见「G1 带来的一个必须交代的后果」），这个代价被如实记下，而不是靠 `--force` 抹掉。

**v3.4 新增的第七条口径（本条推翻的是我自己在 v3.3 里写下的两处解释，两处都写得很像结论）**：

- **(a) `dirty(N)` 是记录动作的属性，不是代码的属性。** v3.3 的「被测版本绑定」一节里，我为了解释同一个 commit 为什么记成两个版本（`dirty(26)` 与 `dirty(55)`），写下了「另一个计数方法」这一条。**它是错的，而且是我凭空补出来的**——两者是**同一次 `git status --porcelain` 行数在不同时刻的读数**，而时刻之间的差别就是**驱动自己写进去的证据文件**。本轮实测把这条错误钉死了：同一个 build `cf08ac8`（源码一字未改）被记成 **5** 个版本（`dirty(8 / 26 / 56 / 69 / 130)`），而**更早**的 `688dd90` 反而记成 `dirty(155)`——因为给它打分的那一批跑得更晚。**版本号在两个方向上同时是错的**：把同一个平台拆成五个，也没能把两个不同平台分开（行数相同即同名）。修复见 R5：改为对源码树内容取指纹。**教训不是「计数不精确」，而是「一个被记了很久、被渲染进每一份报告的字段，从未被当作前提去检查」——与第六条口径同源。**
- **(b) `tier-10` 的失败不是「服务方并发上限为 5」。** v3.3 的「八」把成因写成并发上限，并据此推出「该档位不可运行」。本轮回溯 `run_events` 得到的是另一条链路：限额**按余额派生**（6 秒内由 7 降到 5，报文自述「based on your remaining balance … top up your balance」），批次真正死在 **402 Insufficient Balance**（01:58:59 起 13 条），随后是 85 条 `model circuit is open: deepseek`。**在已充值账户上，`tier-10` 实测 60/60 成功、168.2 秒。** 两处错误的后果不同：成因说错，会让下一轮去调并发数（无效）；补救说错（「不可运行」），会让下一轮**删掉一档本可运行的测试**。**这份文件因此把「成因」与「补救」分开写：前者是观测，后者是建议，两者都不许由另一者推出来。**

**v3.4 新增的第八条口径（第六条说「记录 ≠ 检查」，这一条是它的下一句：记录过的东西如果不再被读，与没记一样）**：

- **D5 在本文件里被记了两轮「未关闭」，而它在 2026-09-24 的 `688dd90` 就已经修好了。** 两半都在那个提交里：限流改为按运行的截止时钟等待、降级裁决改为先重规划一次；测试也在同一个提交里（`test_phase7_rate_limit_degradation.py` 18 条 + 评审器侧 2 条，且后者的 docstring 里**逐字写着「(D5)」**）。本轮只是**重新核对了代码与测试**并补了一次变异验证，就发现整条结论是过时的。
  1. **这不是判断错误，是流程错误。** v3.0 写下「修复 7 不是 D5 的修复」时**是对的**；v3.1/v3.2/v3.3 的每一轮都**把这句话抄了下去**，而没有任何一轮去核对它是否仍然成立。本文件对「新观测」有很严的纪律（单次观测不得当作稳定），对**「旧结论」却没有任何纪律**——旧结论被默认为已经验过。**D5 说明这条默认是错的。**
  2. **因此本文件从 v3.4 起对「已知未关闭缺陷」表加一条规则**：**每一轮至少重新核对一条旧缺陷**，并把核对方式（读了哪个文件、跑了哪些测试、做了什么变异）写进该轮记录。本轮原本只计划核对 D5，**核对完之后发现「至少一条」这个下限设得太低**——既然已经翻到代码里，把剩下的九条一起读完的边际成本只是几次 `grep` 和读文件，而它们已经被抄了两个版本。**于是本轮把 D4–D13 十条全部回读了**，结果见「其他已知未关闭缺陷（v3.4 逐条回读后重写）」：八条早已修复、两条的「影响」不成立。**这反过来证明下限本身有问题：如果一条旧结论的默认状态是「可信」，那么每轮只抽查一条，等于给另外九条继续背书的授权。** 规则因此改为——**凡进入下一轮的缺陷表，必须整表重核；做不到整表重核时，未重核的部分必须在表上逐条标出「本轮未回读」，而不是默认为仍然成立。**
  3. **与第六条口径的关系**：第六条说「一个字段被记了很久、被渲染进每一份报告，却从未被当作前提检查」；第八条说「一条结论被记了很久、被抄进每一版文档，却从未被重新读过」。**两者是同一个失败的两端：写下来的东西不会自己保持正确。**「记录 ≠ 检查」的下一句是「**复查 ≠ 重抄**」。
  4. **本条不改变任何「未评估」项的状态**，也不把任何「未评估」变成「已评估」。它改变的是**这份文档对自身旧结论的信任级别**：**本次能清空这张表，靠的不是新证据，是第一次去读旧证据。**
  5. **本次回读暴露的第三个失效模式，比前两个更值得记：一次查询可以什么都没问，却给出一个干净的错误答案。** 核对 D8 时我用 `global_session` 查 `tool_outbox`，得到 **0 行**；如果就此收工，本节会写下「表是空的，没有累积」——而真相是 **98 行、全部 `published`**。该表启用了**强制 RLS**，属主身份同样被过滤，**那 0 行是权限过滤的产物，不是事实**。换成 `tenant_session` 才看得见。**这与 R5（一个从未被当作前提的字段）、与第八条前两款（一篇从未被重读的结论）是同一种错误的不同外衣：三者都是「看起来成功了，所以没有继续问」。** 因此本文件把这条也列为口径：**在把一次查询的结果当作证据之前，先问一次「这个结果是通过哪一层过滤到达我的，而那一层有没有可能刚好滤掉了我要找的东西」。**

**v3.0 仍然成立的三条（v1.0/v2.0 记录，本次重新证实）**：

1. 「检索到正确手册即等于分析用上了它」——判定必须看 claim 的 `evidence_refs` 与 `selection_manifest`，不能看 citation 列表。（v3.1 实测：根因 claim 的 `evidence_refs` **每次都**指向正确手册，无一例外；而 citation 列表里**每次都**含诱饵。这条口径因此被更干净地证实：两者在同一次运行里就分开了。）
2. 「案例通过即链路健康」——BLOCKED 与 FAIL 必须分开记录；v3.2 的 **28** 条 PASS 中，`frontend`/`mcp`/`outbox`/`index-lifecycle` 四行只由探针记分，且各只有 1 条断言。**PASS 28 与「链路健康」之间的距离，并没有因为本轮从 26 涨到 28 而缩短。**
3. 「离线重放通过即当前代码通过」——见「术语纪律」第 2 条。

## 边界与下一阶段

- 本阶段结论限于「核心业务闭环验收」，**结论为通过**——且**该结论的适用范围是 `cf08ac8a7b731054e492ed81ba5f3164dc381863+patch(0ce86f5d19bc)` 这一棵源码树**（v3.3 记的是同一棵树的 `+dirty(55)`；**2026-10-01 冻结时提交为 `5fea7ca60b4ca71df5495c80ce38751e01f4647f`，内容一字未改**——三个名字，一棵树，见「被测版本绑定」与 R5）。不声明生产容量已认证，不替代 Phase 8 的并发、长稳与灾备演练。

```text
【当前状态：本阶段无未决裁定项；存在一个已批准但未执行的支出动作】
v3.4 新确认并关闭两条（都有锁定测试、变异验证与实跑证据）：
  R4 → 语料未覆盖的问句产出残缺计划，运行以 critical_error 结束（历史触发 48 次）
       → 证据流水线由确定性策略补全；5 条新测试 + 变异验证；Q-200 由 failed 转 succeeded
  R5 → deployed_revision 数的是 git status 行数，而证据就写在同一棵树里
       → 版本号自指：同一 build 记成 5 个版本，更早的 build 反而记成更大的数字
       → 改为对源码树内容取指纹（<sha> 或 <sha>+patch(<12 hex>)），四端共用一份实现
v3.3 已关闭三条（保持关闭）：R1 路由分叉、R3 失败原因持久化、G1 四门版本前置检查
v3.4 整表回读旧缺陷清单（10 条），没有为任何一条改动源码：
  8 条早已修复（D4/D5/D6/D9/D10/D11/D12/D13，各有代码落点与锁定测试）
  2 条的原始描述本身不成立（D7 顺序耦合被相对增量断言证伪；D8 投递半已跑通、缺下游订阅者属范围边界）
  → 「缺陷清单 10 → 0」与「本轮修复 0 条」是同一件事的两面
仍有一条「未复现的观察」保留在清单里，不当作已修复：
  R2 → reviewer 判定不稳定：3/3 活体探针判定正确，样本量 3，未复现也未排除
并仍有一个证据缺口（旧缺陷被看见，v3.4 未动）：
  三条语料横跨多个源码版本 → 恢复离线 CI 之前必须先按单一版本重录（P7.6.5/6/7）
  v3.4 的四门取码：验收 0 / 质量 3 / 负载 3 / 安全 3
以及一个本轮新写下的边界：
  已关闭缺陷清单（D1/D2/D3/D14–D17、R1/R3/G1）从未经过同样的回读 → 见「未评估」
```

- **关闭本阶段的前置（v3.3 更新）**：
  1. ~~裁定 D16~~ **已完成**——见「v3.2 的根因修复」D16-产品 / D16-判定器。**未采纳**「把 `must_not_cite` 改成只对『被用作归因依据』的引用判违规」这条更弱的方向。
  2. ~~裁定 D17~~ **已完成**——见 D17。**未采纳**「改成条件断言」这条退路（那等于承认该案例在多数运行下不产生信号）。
  3. ~~修 D3~~ **已完成**——评审器证据集改为分析实际交付的那一份。逐 id 实测：v3.2 run `d4f2736c` 的评审集 − 分析集 = ∅。
  4. ~~**修 D5** 的降级语义——修复 7 只改重试**时机**，`MODEL_RATE_LIMITED` 触发降级回退后的行为未变。~~ **v3.4 更正：已完成，且早在 `688dd90`（2026-09-24）就完成了。** 本项此前写「仍未做」是**抄了旧结论而未核对代码**。本轮重新核验：限流改为用运行截止时钟等待、降级裁决改为先重规划一次，两处都有锁定测试，并做了变异验证（分别 1 红 / 10 红）。见 D5。
  5. ~~**D8 / D11 的架构要求**（outbox 消费者、执行器正文回读）——两者都不是本轮核心闭环的依赖，但都是发布前应关闭的欠账。~~ **v3.4 更正：两半都要拆开读，而拆分之后这一条几乎不存在。** **D11（正文回读）早在更早的提交里就已修复**——`body_matches()` 逐字比对正文，不一致即抛 `ToolVerificationFailed`（见「其他已知未关闭缺陷」表 D11 行）。**D8 则要分成两件事**：①**投递半早已完成并正在运行**（`servicemind-outbox.service` 长期 active、实测 98 行全部 `published`、零 `pending`/`failed`）；②**真正的残留是「没有下游订阅者」**，而核心闭环走同步续跑、**不需要**它——这是**范围边界**，不是欠账。因此**这一项没有「仍未做」的东西**；正确的记法是**不得宣称异步消费与恢复已通过**，该声明已写入「未评估」。
  6. **补「未评估」里的验证**——P7.6.5 / P7.6.6 / P7.6.7 与 P7.0–P7.5。**本轮 PASS 一条都没有改变它们的状态。**
  7. **让被 G1 判为不可用的三条语料重新可用**（v3.3 新增，是**恢复离线 CI 的前置**，不是本阶段的通过条件）。**v3.4 更新**：v3.3 把这一条写成「质量语料按单一版本重录、负载档位**纳入服务方并发上限后**重录、安全场景重录或显式绑定 `--expect-revision`」——其中**负载那一半的前提是错的**（并发上限不是成因，见「八」与「历史口径修正」），**而且当时这条路根本走不通**：R5 修好之前，重录本身就会改动被记录的版本号，四批语料在同一次录制里也**不可能**同名。R5 落地后这条恢复路径**第一次存在了**，它现在的形态是**一次录制战役**，四个执行条件如下：

     | 条件 | 为什么必须有 |
     | --- | --- |
     | **R5 已生效**（四端共用 `source_revision`） | 否则重录本身移动版本号，一条语料录到一半就与自己的前半段不同源 |
     | **录制期间不改 `src/` 与 `tests/`** | 指纹覆盖 `tests/`（故意如此：一个版本同时覆盖平台与其配套套件），改测试就等于换了版本 |
     | **录制期间不做任何提交**（2026-10-01 冻结时补，**此前漏了**） | 工作树干净时版本号取的是 **HEAD**，而**任何提交都会移动 HEAD——包括只改 `docs/` 的提交**。实测：`5fea7ca` 与 `16ba55c` 之间**源码零差异**（`git diff 5fea7ca 16ba55c -- src/ tests/ scripts/` 为空），后者只改本文件，而 `deployed_revision` 由 `5fea7ca…` 变成了 `16ba55c…`。因此「不改 `src/`/`tests/`」这条**不足以**保证一批录制同源：**在中途改进度笔记也会把批次劈成两半**。危害有界——四门会把这种批次判为异质并**拒判**（fail-closed，不是静默通过）——但代价是一整晚的录制作废 |
     | **`--expect-revision` 只在收尾核对时给** | 录制中途给值会把「同一份产物」判成「不是当前版本」，属误用；同源性检查不需要外部输入 |
     | **账户余额充足** | `tier-10` 的失败已查明是 **402 余额不足**并引发 85 次熔断；余额不足会污染一整批观测 |

     在此之后、也就是**阶段 B 完成之前，`phase7-offline-gates` 不可恢复。**
- **本轮已执行与未执行的分界（写明，以免被当作已做）**：**已执行**的是阶段 A 的**小探针**——它们花掉约 **$1**，产出的是「成因」「稳定性」「单价」三类判断，**不是**通过结论。**未执行**的是阶段 B 的**全量重录**（第 7 条的实体内容，也是恢复离线 CI 的唯一路径），实测单价外推约 **$5.54（约 ¥40）**：质量 200 条 ≈$3.6626、负载 120 run ≈$1.4441、安全 ≈$0、验收 ≈$0.4363。**该动作未开始，也不在本轮的授权范围内**；本文件只把它记为待裁定项与未评估项。**阶段 A 通过不等于阶段 B 已做。**
- 其他缺口分批修复；**每次修复后更新被测版本并重跑受影响案例**。缺陷修复后，其原复现案例必须复绿。
- 「避免改变基线」不构成保留在用安全缺口的理由。**同样地，「恢复 CI」也不构成用 `--force` 把不可用证据说成可用的理由。**
- **已经做完、不需要再裁定的**：D1（v3.0）、D14（v3.0）、D15 及其 D15-a/b/c/d（v3.1）、D3 / D16 / D17（v3.2）、**R1 / R3 / G1（v3.3）**。**不要再把这九项当作未决项。**

### 本轮改动的可配置面（供复核者定位）

v3.2 / v3.3 / **v3.4** 引入或依赖的、与判定直接相关的参数，**全部是该步骤内的固定输入、既有配置或 gate 命令行参数**，没有新增部署级开关（`git diff` 中 `src/` 的改动未新增任何环境变量读取）：

| 参数 | 值 | 在哪 | 作用 |
| --- | --- | --- | --- |
| `--expect-revision`（v3.3 新增） | 默认 `None` | 四个 gate 的命令行 | **`None` 时只查同源性**（不需要外部输入即可判定）；给了值才追加「是否就是这一版」的时新性判定。**这不是部署开关**：它不改变平台行为，只改变 gate 拿哪个版本去比对 |
| `INCIDENT_PATTERNS` / `_INCIDENT_SYMPTOM` / `_INCIDENT_ASK`（v3.3 新增） | 症状词 × 求助词的**双向 80 字符窗口**正则 | `src/servicemind/orchestration/router.py`（**源码常量，不是配置**） | 判「用户报告了一个具体故障并要一个处置」。边界由 `evaluation/routing/routing.jsonl` 里**两侧都有的行**钉住：必须进管线的故障报告、以及只是提到症状的**检索类**问句（如「Find the runbook for the MFA failure」）各在两侧，改这些正则就必须同时挪动一条案例 |
| `max_input_tokens` / `system_reserve` / `output_reserve`（探针） | `2000` / `0` / `0` | `verify_phase7_acceptance_live.py` 的 `context_pruning_producer` 内**硬编码** | 只影响 ACC-06 的探针步骤；**不改动**生产配置 `SERVICEMIND_CONTEXT_MAX_INPUT_TOKENS=12000` |
| 探针载荷 | 1 条 POLICY 控制行 + 6 条各 1802 字符 / **799 token**、内容互异的 EVIDENCE 行 | 同上 | 保证 6 条**不被内容去重**先吃掉，从而真正触发预算规则 |
| `analysis_evidence_ids` | 无配置项 | `Phase3State` 字段；由 `analysis_node` 写入 | **不是开关**：只要信封被构建就写入；缺省（stub 分析）时评审器退回全集 |
| `must_cite` | 逐 fact 声明 | `evaluation/acceptance/cases.v1.json` | **只在判定侧**，不进入产品运行时 |
| `NON_SOURCE_PREFIXES` / `DIGEST_LENGTH`（v3.4 新增） | `("evaluation/", "docs/")` / `12` | `src/servicemind/evaluation/source_revision.py`（**源码常量，不是配置**） | 定义「什么算源码」。**它是一个排除表而不是包含表**：新出现的顶层目录会**自动**被算进版本号，而包含表会继续报一个从未提及它的版本号。往这张表里加一条，就等于**静默地**让该目录下的改动不再改变版本号 |
| `_ANALYSIS_THROTTLE_MAX_WAIT_SECONDS` / `_ANALYSIS_THROTTLE_ATTEMPTS`（`688dd90`，v3.4 核对） | `60.0` / `3` | `src/servicemind/agents/analysis.py`（**源码常量**） | 等待限流的总上限与尝试次数。**受运行截止时间二次钳制**（还要扣掉后续阶段所需的余量），所以调大它不会把一次调用拖成超时 |
| `max_replans`（既有） | 由 run 的策略给出 | `Phase3State` / 评审入参 | v3.4 核对 D5 时的关键边界：降级分析**先重规划一次**，`max_replans` 用尽才升级给人——**它同时是「修复生效」与「不至于变成循环」的那个上界** |

**注意最后一行**：`must_cite` 是**验收判定器的字段**，不是运行时约束。它约束的是「这次运行的观测算不算通过」，不是「平台运行时该怎么做」。**产品侧的约束走的是提示词**（`agents/analysis.py`），两者是分开的两件事——把判定器字段当成产品修复会是一个错误的读法。

## 签署

本基线在 2026-09-22 冻结判定规则与缺陷口径，v3.0 于 2026-09-23 执行、v3.1 于 2026-09-23 执行、v3.2 于 2026-09-23 执行、v3.3 于 2026-09-30 执行、**v3.4 于 2026-10-01 执行**。执行后的裁定以机器生成的验收证据为准，实际结论为**通过**：**PASS 28 / FAIL 0 / BLOCKED 0**，**107 条断言全部 PASS**，验收 gate 退出码 **0**（不加 `--force`）；**质量 / 负载 / 安全三门为退出 3（拒判），成因是修复前录制的旧证据自带异质性，见「十一」**。

**v3.4 与前面每一轮的一个不同之处必须写在这里**：v3.4 **没有重跑 28 条案例**（判定与 v3.3 逐字相同，`cases_digest` 与 `observation_digest` 均未变），它的产物是**四组廉价探针（≈$1）+ 两条缺陷的根因修复（R4/R5）+ 对十条旧结论的整表回读（D4–D13）**。因此 v3.4 的通过数字**不是新增的证据**，而是**同一份证据在新的核验下仍然成立**。

**而这十条旧结论的回读结果，是本轮最大的单一产出，值得单独写在这里**：十条中**八条在更早的提交里就已修复**（其中最久的从 2026-09-23 起就一直如此），**两条的原始描述本身就是错的**。**本轮一行源码都没有为它们改过。** 因此——

> **缺陷清单从 10 条降到 0 条，同时本轮修复的缺陷数是 0。** 这句话必须一起写，因为只写前半句会被合理地读成后半句。

**版本沿革的关闭情况**：

| 缺陷 | 版本 | 处置 |
| --- | --- | --- |
| **D1** | v2.0 发布阻断 → **v3.0 关闭** | 修复 A（超预算行的回收趟）消除其可观测后果；ACC-03 的正确手册以 `reclaimed_from_source_cap` 进入分析信封 |
| **D14** | v2.0 发布阻断 → **v3.0 关闭** | 用户裁定：改 `cases.v1.json` 中 ACC-03 的 `question`，使之与工单夹具一致 |
| **D15** | v3.0 发布阻断 → **v3.1 关闭** | 用户裁定「完整修：身份解耦 + 解除自锁」；D15-a/b/c/d 四项实施并锁定，ACC-12b 由 BLOCKED 转 **PASS** |
| **D3** | v1.0 记录 → **v3.2 关闭** | 评审器证据集改为「分析实际交付的那一份」；逐 id 实测评审集 − 分析集 = ∅ |
| **D16 / D17** | v3.1 记录 → **v3.2 关闭** | D16 双管（提示词条款 + `must_cite` 正向断言）；D17 把裁剪断言改接确定性探针。**均未采纳**「放宽/条件化断言」的方向 |
| **R1** | v3.3 新确认 → **v3.3 关闭** | 路由器把「报告一个故障并要一个处置」判成「查一条知识」（`如何处理/怎么处理` 那一行命中）。新增 `INCIDENT_PATTERNS` 诊断路径，金标语料 100 → **107 例**、`accuracy 1.0`。**没有放宽任何断言**：判错的那一例在修复前就是判错的 |
| **R3** | v3.3 新确认 → **v3.3 关闭** | 失败原因只写进 `run_events`，`agent_runs.error` 恒为 `NULL`；三条续跑路径还把异常类型名写成 `str(exc)`（且有一处写的是恒为 `"str"` 的类型名）。改为写入 `<termination_code>: <reason>` |
| **G1** | v3.3 新确认 → **v3.3 关闭** | 四个 gate 都读了 `deployed_revision`，没有一个把它当前提；质量 gate 曾对横跨 **4 个源码版本**的 200 条观测报 **PASS**。新增 `evaluation/revisions.py` 并接入四门，`--force` 不能越过。**关闭的直接后果是那三条语料被拒判**——这是修复生效的证据，不是回归 |
| **R2** | v3.3 记录 → **未关闭，也未复现** | 3/3 活体探针判定正确。**不写成「已修复」**：没复现出来的东西不能声称修好了。按术语纪律记为**未复现的观察**，「是否稳定」列为未评估 |
| **D5** | v3.0 记录 → **实际于 `688dd90`（2026-09-24）关闭 → v3.4 核对后确认** | **本条与上表的每一条都不同：它不是本轮修好的，是本轮终于去核对的。** 两半（按截止时钟等待限流、降级裁决先重规划一次）与锁定测试都在那个提交里；本文件此前三个版本重复抄写「仍未做」。v3.4 重新核验并补做变异验证：改回「永远升级」→ 1 红，令限流永不等待 → 10 红。**关闭证据是单元级的**——本轮没有制造一次真实限流做端到端复现，因此「真实限流下是否仍会停在人工队列」仍按未评估处理 |
| **D4、D6、D9、D10、D11、D12、D13** | 各自记录于 v1.0–v2.0 → **分别在 `688dd90` 或更早关闭 → v3.4 整表回读时确认** | **与 D5 完全同类**：v3.4 顺着 D5 把剩下九条一并读完才发现。合计七条，**本轮一行源码都没为它们改过**。逐条的代码落点与锁定测试见「其他已知未关闭缺陷（v3.4 逐条回读后重写）」。**其中 D13 的证据是源码而非一次成功的执行**，此处如实标出 |
| **D7、D8** | 各自记录于 v1.0–v2.0 → **v3.4 回读时判定「原始描述本身不成立」** | **不是修复、也不是「早已修复」，而是这条缺陷当初就描述错了。** D7 的「顺序耦合」被断言词汇证伪（14 条 followup 断言全部取相对增量，无一条依赖绝对计数）；D8 的投递半已部署、`servicemind-outbox.service` 长期 active、实测 98 行全部 `published`、零 `pending`/`failed`——真正的残留只是「没有下游订阅者」，属**范围边界**而非缺陷。**两条都因此转入「未评估/口径」而非「已关闭」**，理由写在各自的表行里 |

**D5 的关闭方式值得单独记一句**，因为它是这份文档里第一条「不是因为修了什么、而是因为读了什么」而改变的结论：**本文件的缺陷清单此前只增不减地被抄写了三轮，D5 是第一次被回读**。

**然后是这一轮真正的发现**：当时这段写着「同类的过时结论是否还有其它条目，**未评估**——本轮只核对了 D5 一条」。**我把它改了，因为答案是「还有，而且是全部」。** 顺着 D5 往下读完 D4–D13，十条里八条早已修复、两条的描述本身不成立（详见「其他已知未关闭缺陷（v3.4 逐条回读后重写）」）。**所以这句话的初版才是本轮最有价值的一句**——它承认了自己不知道，而正是因为承认了，才会去把剩下的九条读完；如果当时写成「其余九条未见异常」，就不会有后面的核对。

**v3.3 的结论边界，逐条写清**：

- R1 关闭证明的是「107 条金标问句的路由逐例正确（`accuracy 1.0`）」。**金标是离线分类，不是活体对话的重复测量**——同一问句在真实对话里重复 N 次是否仍落同一条路径，**未评估**。
- R3 关闭证明的是「失败终态会把 `<termination_code>: <reason>` 写进 run 行」。佐证来自负载失败行（修复后写入的新行）；**本轮没有制造一次受控的新失败运行**来在成功路径上反向确认。
- G1 关闭证明的是「四门在读证据前会拒绝跨版本批次，且 `--force` 到不了这里」（四门各自的接线与 `main` 都被测试覆盖，且接线移除后测试变红）。它**不**证明那三条语料已被修好——**它们现在是被拒判的**，重录属于 P7.6.6 / P7.6.7。
- 以上三条**都不改变**任何缺陷的状态，也**不把任何一条「未评估」变成「已评估」**；R2 更是**新增了一条明确处于「未评估」的观察**。

**v3.4 的结论边界，同样逐条写清**：

- **R4 关闭证明的是「规划器不再产出残缺流水线」**：补全由确定性策略完成，5 条测试 + 一次变异验证，活体 `Q-200` 由 `failed` 转 `succeeded`。它**不**证明「语料之外的一切问句都能得到正确答案」——补全保证的是**流水线形状**，不是**答案质量**。
- **R5 关闭证明的是「版本号命名的是源码内容，不是录制动作」**：13 条单元测试（跑真实 git 仓库，不是打桩）+ **3/3 变异被杀** + 四个驱动对同一棵树返回**逐字相同**的字符串。**它不被证明的部分是**：一次真实的、跨小时的四语料录制尚未发生——「同一场录制里四份语料同名」目前是**从实现推出的**，不是**被实测的**（列入「未评估」）。
- **D5 关闭证明的是「两半机制都在，且被测试钉住」**（含本轮补做的变异验证）。**它不被证明的部分是**：**没有一次真实的限流**被制造出来做端到端复现，因此「限流真的发生时，运行是否仍会停在人工队列」**未评估**。关闭证据是单元级的，本文件不把它写成「限流问题已解决」。
- **A3 的更正推翻的是 v3.3 的归因而非数据**：v3.3 记录的失败行仍然真实存在，变的只是**对它的解释**（余额派生限额 + 402，而非并发上限 5）。因此 v3.3 那一节里的**表格**被保留、**结论句**被替换——这种改法在本文件里是第一次，故在此点明。
- **A4 的关闭有代价，已如实记录**：安全语料由同源变为跨新旧两种格式，安全 gate 因此由 0 变 **3**。**这不是回归，也不是改善的净收益**——它是一笔写明的交换（用 1 个跨版本语料换掉 2 条没有跑过的断言），由阶段 B 收敛。

**这份 PASS 是一份「有限范围内成立」的通过。** 它基于固定版本（`cf08ac8a7b731054e492ed81ba5f3164dc381863+patch(0ce86f5d19bc)`，v3.3 记作同一棵树的 `+dirty(55)`，2026-10-01 提交为 `5fea7ca60b4ca71df5495c80ce38751e01f4647f`）、真实身份服务、真实 GLPI、真实 OpenSearch 与真实模型的当前部署，不构成对未来供应商故障、未知攻击或未执行负载形态的绝对无缺陷保证。凡本文件未列出观测的模块，均按「未评估」处理。

**冻结声明（2026-10-01）**：本文件所描述的全部改动**已提交为 `5fea7ca`**，工作树清空。**提交这个动作不改变任何结论**——它改变的是这份结论**第一次有了一个别人可以 `git checkout` 到的名字**。在此之前，本文档反复引用的 `+patch(0ce86f5d19bc)` 是一枚**只存在于一台机器的工作树里的指纹**；如果那棵树丢失，本文件的每一条证据都会指向一份无法取回的代码。

**这条声明要引用哪个 SHA，本身有个必须说清的细节。** `5fea7ca` 是**代码**的最后一次提交；本文件随后仍被修改（记录冻结这件事、补第四条第 5 项），每一次都产生新提交、移动 HEAD。而工作树干净时 `source_revision` 取的正是 HEAD，**所以「最终的版本号」是个追不上的目标**：写下它就需要一次提交，而那次提交又移动了它。**处理办法是把两个问题分开**——

- **引代码时用 `5fea7ca`。** 自它之后 `src/`、`tests/`、`scripts/` 零差异（可用 `git diff <后来的提交> 5fea7ca -- src/ tests/ scripts/` 自行核验，输出为空），所以**它足以唯一确定这份代码**。
- **引 HEAD 时以仓库当时的 HEAD 为准**，不要引用本文件里写下的任何 HEAD 值——它在本文件写下的那一刻就已经陈旧。

**同一份文档在 12 小时内的两轮结论（v3.2 → v3.3）之间的差别，最值得记住的一句是**：v3.2 的 PASS 里有三条断言（R1 那条路由、R3 那条持久化、G1 那个前提）**当时并不存在**——不是当时判错了，是**当时没有问**。本文件对每一轮都记下「问了什么、怎么问的、结果是什么」，正是为了让「没有问」这件事在下一轮能被看出来。

**而 v3.3 → v3.4 的这一句是它的补集**：v3.4 的 PASS 与 v3.3 **逐字相同、一条都没多**——本轮**没有新证据进账**，改的是**对旧证据与旧结论的核验方式**。它发现的是另一种失败：**不是「当时没问」，而是「后来没再问」**（D5 被抄写三轮而从未复核）。两句话合起来才是完整的纪律：**新结论要经得起追问，旧结论要经得起回读。**

**这一轮的完整账，用一行写完**：本轮**新增源码改动 2 处（R4、R5）**、**关闭旧结论 10 条（D4–D13，其中 8 条早已修复、2 条描述本身有误）**、**新证据 ≈$1 的探针**、**通过数字一条未变**。**「缺陷清单 10 → 0」这件事的全部重量，落在最后两个字上：未变。**

**最后必须留给自己的一句警告**：v3.4 能清空这张表，是因为**终于有人去读了它**，而不是因为**它有理由是对的**。同一份文档里还躺着**更长的**一份已关闭清单，从未经受同样的回读；而本轮已经证明，**一份只被抄写、不被阅读的清单可以整表失真，且失真不会自己显形**。**下一轮如果把这份新表也当作既成事实抄下去，就是第三次犯同一个错误。**

机器可读证据：`evaluation/reports/phase7_acceptance_latest.json`（判定与覆盖）；
`evaluation/acceptance/replays/*.json`（逐案例观测）；
`evaluation/reports/phase7_pipeline_evidence.json`（前置条件与回归的实际退出码）。
