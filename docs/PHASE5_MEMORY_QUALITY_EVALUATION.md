# Phase 5 记忆质量评测（2026-09-15；判别力补强 2026-09-16；投递层补强 2026-09-16）

> 本文回答一个问题：**受治理长期记忆的写侧治理与读侧安全，有没有可执行的质量证据？**
> 在此之前的答案是「没有」——Phase 5 只有治理单元测试与迁移/工作流校验脚本，
> 没有任何按治理保证组织的评测语料与指标。本文档及其配套代码补齐这一层。

## 0. 结论摘要

1. 新增 `src/servicemind/evaluation/memory.py`（harness）、
   `evaluation/memory/scenarios.v1.json` + `scenarios.v2.json`（语料）、
   `scripts/evaluate_phase5_memory.py`（驱动）、
   `tests/servicemind/test_phase5_memory_evaluation.py`（39 条契约测试）。
2. **v2 语料实测**：写侧 48 步**动作精确匹配率 1.0 / 落库状态匹配率 1.0 / 原因码覆盖率 1.0**，
   零越权激活、零误拒；读侧 45 个探针**泄漏率 0.0**；
   声明式排名门禁 29 个受门禁探针**全部通过**；声明式投递门禁 4 个受门禁探针**全部通过**
   （启用 5000-token evidence cap 后，最紧探针的记忆通道可支配 5738 token）。
   离线模式确定性、无外部依赖；TEI 模式使用固定 revision 的生产嵌入服务。
3. **这些数字只证明「设计声称的保证在当前实现下成立」，不证明任何租户真实记忆分布的质量。**
   语料由本项目编写（与 Phase 4 gold set 同性质），不是生产记忆流量回放。见 §4。
4. **2026-09-16 补强（本文档的主要改动）**：审计发现 v1 语料里名为 `p-taint` 与
   `p-injection` 的两条探针**没有触达它们名字所指的过滤器**，因此那两条的 0.0
   对污点过滤器与读侧注入绊线**什么都没证明**；排序块的 1.0 也只是因为语料无法区分。
   v2 以三条机制补齐判别力（配对对照、直接播种、声明式排名门禁），并为每条机制
   提供了「把该机制打掉必须变红」的证伪证据。详见 §3.4。
5. 审计过程中发现并关闭了 `procedural` 路径的三层缺口：提取情节作用域与激活守卫
   互斥、同工单无法提供两个有效支撑来源、没有受 ACL 约束的待审读取/决策入口。
   现生产后运行中间件只按**跨工单、同一规范化建议**形成 quarantine 候选，永不自动激活；
   `approver` 必须在版本、内容摘要和状态三重快照仍一致时才能激活或拒绝。详见 §5。
6. 新增了**写入路径的读取保护判别器**：后运行摘要必须显式声明作用域策略
   （`provenance["post_run_scope"]`）才会被服务，未声明的遗留行继续被挡。
   这是「让提取器产出 TENANT 作用域情节」这条裁定能落地而不打瞎 episodic 召回的前提。
7. 2026-09-16 最终裁定并关闭两项实现缺口：Memory 写入、Memory 读取与工具参数策略
   共用同一份 23 条注入标记和同一个判定函数；检索候选截断与最终排序均改用
   `content_hash + idempotency_key` 稳定破同分，不再用随机 `memory_id` 参与排序。
8. **2026-09-16 补上「检索 ≠ 投递」缺口**（§3.5）：此前所有读侧结论只到检索为止，
   而记忆在 `ContextBuilder` 里 authority 最低、**永远最后填充**。按生产配置推导的
   声明载荷，在未设置 cap 的反事实基线中，可用信封 10720 token 里记忆通道实得
   **238 token（约 2%）**，
   一条已批准程序记忆会被静默丢掉且**上报零违规**。新增声明式投递门禁
   （4 条探针）+ 批量通道上限 `SERVICEMIND_CONTEXT_EVIDENCE_TOKEN_CAP`。
   最终裁定为 **5000**，只约束 ANALYSIS；REVIEWER 保留完整 evidence 信封。
   当前部署 `.env` 已启用，标准 lexical/TEI 报告均按此配置重跑通过。
9. **2026-09-16 关闭「论证寿命长于证据寿命」缺口**（§5.6）：Procedural 激活与每次
   候选读取/最终重校验现在共用同一支撑判定；任一必需 Episode 过期、撤销、污染、失去
   verified evidence、作用域不兼容，或 distinct run/ticket 数不足时，Procedure 在返回模型前
   自动转为 `REVOKED`，写入 `PROCEDURAL_SUPPORT_INVALIDATED` append-only 事件。
10. 人工复核已从 API 补到现有 Streamlit 控制台：使用 Keycloak OIDC、`approver` 角色和
    原 API ACL；浏览器提交仍绑定 `version + content_hash + quarantine status`，401/403/409
    分别呈现为重新登录、无审批权限和陈旧页面冲突，不存在绕过后端门禁的 UI 专用接口。

## 1. 为什么把「泄漏率」单列

记忆读侧的错误方向不对称：

- 少召回一条记忆 → 智能体少一条上下文，是**成本**；
- 多召回一条**不该出现**的记忆（跨租户、跨作用域、有污点、已吊销、已过期、注入标记）
  → 是**缺陷**，且是合规事件。

因此 harness 不给「记忆质量」打一个混合总分，而是把可部署指标分成两块：

| 块 | 内容 | 与相似度函数的关系 |
| --- | --- | --- |
| 读侧安全 | 隔离/污点/注入/陈旧/as-of 探针的**泄漏率** | **无关**：断言成员资格，不断言顺序 |
| 读侧排序 | Recall@k / MRR@k / NDCG@k + **声明式排名门禁** | **相关**：报告必须声明打分模式 |
| 读侧投递 | 在声明载荷下**必需记忆是否真的进了提示** + 记忆通道实得预算 | 无关：断言成员资格与预算，不断言顺序 |
| 写侧治理 | 动作精确匹配、落库状态、原因码覆盖、越权激活、误拒 | 无关 |

「模式无关」这一条使安全块可以在离线词法模式下跑 CI，而排序块必须显式标注
`lexical` 还是 `embedding`——当前生产用嵌入，代理离线跑词法，两者不可混为一谈。

### 1.1 一条绿色安全数字要成立，必须先证明那条记录本来够得着

这是本次补强的核心判据。`must_not_surface` 探针的绿，只有在「该记录本来会被返回」
成立时才有意义。如果那条记录被相似度下限丢掉、被 ACL 挡掉、或被写侧策略停在
`quarantine`，它产生的绿与「过滤器在工作」产生的绿**完全一样**——
v1 的 `p-taint` / `p-injection` 正是这样通过的。

v2 用两条互补机制封住这个洞，并要求每个安全探针至少用上一条：

- **配对对照**：在 `must_surface` 里放一条与被禁记录**只差被测属性**的对照记录。
  同一个查询因此同时证明「措辞过了相似度下限与 ACL」和「只有那一点毒被拦下」；
- **直接播种**（`MemorySeedStep`）：对写入路径按构造产生不出来的状态
  （例如「ACTIVE 且带污点标签」），直接写进仓库以陈述读侧前提。
  播种记录**不进入写侧评分**，且在报告中逐条列出，见 §3.3。

排序块有对称的洞：均值不会因为名次后退一位而变红。`RankGate` 用**逐探针声明的
`max_rank`** 把它变成红/绿判定，见 §3.2。

## 2. 语料覆盖面

`evaluation/memory/scenarios.v2.json`：58 个有序步骤（48 写入 + 7 播种 + 3 状态转移；
写入中含 2 条支撑情节）+ 45 个探针。**步骤有序**是刻意的：复活与取代必须在写入之前发生转移。

`scenarios.v1.json`（31 步 + 21 探针）**原样保留**：已发布的 v1 报告
（`phase5_memory_latest.json` 的 `corpus` 字段）引用的是它，就地修改会使已发布结论
无法复现。v2 是 v1 的超集，两者的差异只有新增。

### 写侧（v2：48 步）

| 类别 | 覆盖 |
| --- | --- |
| 黄金 | 租户/用户/组/实体/服务五种作用域各至少一条；事实、偏好、情节、程序四种类型 |
| 复述 | 同主题同内容 → 幂等去重，返回原记录 |
| 矛盾 | 同主题异内容 → 强制隔离，并打 `VERSION_CONFLICT` 标记（不得出现第二个存活版本） |
| 复活 | 已吊销主题的同内容重学 → 隔离，不得静默复活 |
| 取代 | 上一版本 `SUPERSEDED` 后新内容成为新版本 |
| 拒绝 | 明文凭据 `SECRET_DETECTED`；低价值 `NOT_WORTH_SAVING` |
| 隔离待审 | 注入标记、无同意的个人数据、未核验证据、置信度不足、未消解污点 |
| 竞争记忆（v2 新增 4 条） | 与目标词法高度重叠但**结论不同**的事实，使排序成为真实决策而非走过场 |
| 对照记忆（v2 新增 11 条） | 与带毒记录仅差被测属性的干净孪生记录 |
| 后运行摘要（v2 新增 1 条） | 提取器产出的 TENANT 作用域情节，携带 `post_run_scope` 声明与实体/组 ACL |

### 读侧（v2：45 探针）

| 类别 | v1 | v2 | 断言 |
| --- | --- | --- | --- |
| retrieval | 8 | 11 | 目标记忆必须进入返回集；新增 3 条**多答案**查询（答案集 > 1） |
| isolation | 7 | 9 | 跨租户、跨用户、未授权实体/组/服务、未迁移的租户作用域后运行摘要，均不得出现；新增后运行摘要在**有 ACL** 时应被服务、**缺实体**时不得被服务的正反一对 |
| staleness | 3 | 3 | 已取代的上一版本、已吊销记录及其复活尝试，均不得出现 |
| taint | 1 | 6 | 带未消解污点标签的记录不得进入提示；含用户/组作用域变体 |
| injection | 1 | 7 | 带提示注入标记的记录不得进入提示；含中英文统一标记变体 |
| as_of | 1 | 9 | 有效期窗口的开/闭边界、TTL 到期边界、生效时点边界，正反两侧都断言 |

`taint` / `injection` / `as_of` 三类各 ≥5 条变体是本次的**设计要求**（不是对结果的读数），
由 `MIN_SAFETY_VARIANTS` 在测试中断言，少一条即红。

## 3. 实测结果

### 3.1 写侧与读侧安全（`evaluation/reports/phase5_memory_latest.{json,md}`）

写侧 48 步：动作精确匹配率 **1.0**、落库状态匹配率 **1.0**、原因码覆盖率 **1.0**，
越权激活 **无**、误拒 **无**；`activate` 36 条、`quarantine` 10 条、`reject` 2 条，
三类精确率/召回率均为 1.0。

读侧 45 探针：泄漏率 **0.0**（0 条记录泄漏），六个类别全部为 0.0，
所有声明了 `must_surface` 的探针均无缺失目标。

### 3.2 排序块与声明式排名门禁

排序块（`lexical` 模式，30 个带金标探针）：

| k | 探针数 | Recall@k | MRR@k | NDCG@k |
| --- | --- | --- | --- | --- |
| 1 | 30 | **0.9444** | 1.0 | 1.0 |
| 3 | 30 | 1.0 | 1.0 | 1.0 |
| 5 | 30 | 1.0 | 1.0 | 1.0 |
| 8 | 30 | 1.0 | 1.0 | 1.0 |

**`Recall@1 = 0.9444 < 1.0` 是本次补强在排序块上留下的可见痕迹**：v1 的该值恒为 1.0，
因为每条查询只有一个正确答案且它总在第 1 位。v2 加入了 3 条**答案集大于 1** 的查询
（三个办公室的 VPN 根因；常规与灾备两个备份窗口；两台结论相反的工资服务器），
这类查询的正确答案本就装不进第 1 位，因此 k 切片之间不再等价。

**但 MRR 与 NDCG 仍为 1.0，不应被读作「排序质量优秀」。** 在每条查询的第一位都命中
相关记录时，MRR 按定义就是 1.0。该块的角色已从「提供质量数值」转为「由门禁承担」：

```text
### 3.1 排名门禁（声明式）
- 受门禁探针数：29
- 判定：通过
```

（上面围栏内是评测报告自身的片段，其中的小节号属于该报告，不是本文档的编号。）

`RankGate` 只统计**显式声明 `max_rank`** 的探针，每个探针各自带一条
`max_rank_rationale`（产品要求），例如「工单助手在作答前必须看到该记忆，
返回列表前 3 位内必须出现」。未声明排名要求的探针不计入门禁——把它们算作通过会虚增覆盖面。

**门禁值必须按产品要求设定，不得按观测名次回填。** 回填的门禁在写下的当天就按构造通过，
此后只能追认检索器的既有行为，不构成任何约束。`max_rank_rationale` 这一必填字段
就是让回填在评审时可见：一条说「观测到第 2 位」而不是「调用方要求」的理由即是征兆。

### 3.3 直接播种的记录（绕过写入策略）

v2 有 7 条记录由语料**直接写入仓库、未经过治理策略**，报告中逐条列出：

`s-taint-settlement`、`s-taint-access-review`、`s-taint-capacity`、`s-taint-user-scope`、
`s-taint-group-scope`、`s-inj-ignore-previous`、`s-inj-bypass-approval`

它们存在的唯一理由是陈述读侧前提，**不构成任何写侧结论**：
`MemoryScenarioCorpus.writes` 按类型排除它们，写侧 48 步中不含任何播种步骤
（有测试断言 `report.seeded` 与播种集合相等且与 `write_outcomes` 不相交）。

为什么必须播种：

- **污点**：写侧策略对任何带 `taint_labels` 的候选一律隔离，`_validate_activation`
  又把 `UNRESOLVED_TAINT` 列为硬阻断，因此「ACTIVE 且带污点」这条状态
  在写入路径上**按构造不可达**；
- **写侧已识别的注入标记**（`ignore previous`、`bypass approval`）：同理，写入策略会隔离它们。

对后一类，测试直接**把同样的候选交给真实写入器**并断言落库为 `quarantine`
（`test_a_write_side_marker_can_only_enter_the_store_by_seeding`），
以证明播种不是为了绕开一条会失败的期望。

### 3.4 判别力证据：每条新机制都要能被证伪

一个不会变红的评测比没有评测更糟。下表是本次对 v2 逐条执行的证伪实验——
**每一行都是「把被断言的东西拿掉，结果必须变红」**，全部作为契约测试固化在
`tests/servicemind/test_phase5_memory_evaluation.py` 中：

| 被拿掉的东西 | 做法（只动语料，不改生产代码） | 结果 |
| --- | --- | --- |
| 5 条播种记录的污点标签 | 从候选里删掉 `taint_labels` | 5 条 `p-taint-*` **全部泄漏**，泄漏率 0.0 → **0.1111**（5/45） |
| 载荷记录的注入短语 | **外科式**只替换标记短语本身 | 7 条注入类探针**全部泄漏**（6 条 `p-inj-*` + v1 原样保留的 `p-injection`），泄漏率 0.0 → **0.1556**（7/45） |
| `valid_to` 关闭边界 | 把该记录的 `valid_to` 后移 300s | `p-asof-close-edge` 泄漏 |
| `expires_at` 到期边界 | 后移 300s | `p-asof-ttl-edge` 泄漏 |
| `valid_from` 生效边界 | 把未来记录提前 86300s | `p-asof-future-just-before` 泄漏 |
| `valid_from` 的含端点语义 | 把该记录 `valid_from` 后移 **1 秒** | `p-asof-open-edge` **丢失必需记录** |
| 排序器 | 子类化 `MemoryRetriever` 反转返回顺序 | 排名门禁 **20 条违规**、判定不通过 |
| 后运行摘要的 `post_run_scope` 声明 | 把该标记**加到**遗留行上 | 遗留行 `p-legacy-middleware-scope` **泄漏** |
| 后运行摘要的 `post_run_scope` 声明 | 把该标记从已声明行上**删掉** | `p-post-run-tenant-episode` **丢失必需记录** |

关于「外科式替换」：第一版证伪实验把整句载荷改成通用句，结果 6 条里只有 2 条变红——
因为那样同时改掉了措辞，记录改由**相似度下限**拦下，看起来仍然「安全」。
只替换标记短语、保留其余措辞（并要求对照记录仍在位、`missing_required == []`），
才是对绊线本身的证伪。这个失败本身说明了 §1.1 的判据为什么必要。

排序门禁的活性证明还包含一条反向断言：把检索器换成「什么都不返回」，
**缺失记录必须报成门禁违规、且 `worst_required_rank` 保持 `None`**——
若把幸存记录的最深名次报出来，一次召回失败就会被粉饰成一次通过的排名结果。

### 3.5 投递层：检索到不等于进了提示（2026-09-16 补强）

前述所有读侧结论都只回答「检索器有没有把该给的记忆取出来」。**取出来不等于送达。**
`MemoryRetriever` 的职责到检索为止；真正决定一条记忆能否进入模型提示的是
`ContextBuilder.build()`（`src/servicemind/context/builder.py`）。本节把这一段补上，
因为它是整条链路上唯一一处**记忆可以在零违规的情况下被静默丢掉**的地方。

#### 载荷是「声明」不是「观测」

`ContextBuilder` 的信封由 `max_input_tokens - system_reserve(256) - output_reserve(1024)`
决定。生产锚点为 `SERVICEMIND_CONTEXT_MAX_INPUT_TOKENS=12000` → **可用信封 10720 token**。
在裁定前的无上限形态中：

| 通道 | 实得 token | 说明 |
| --- | --- | --- |
| `output-schema` | 2170 | `required`，`AnalysisResult` 的 JSON Schema |
| `policy` | 16 | `required` |
| `task` | 约 40 | `required` |
| `evidence` | **8250** | authority 0.95，先于记忆填充 |
| `memory` | **238** | authority 0.7，**只剩零头**（无 evidence cap 的基线） |

原因是确定的，不是调参问题：填充键为 `(not required, -authority, -relevance, ...)`，
记忆的 authority 0.7 低于证据的 0.95，因此记忆**永远排在最后**——前面剩多少，它就最多拿多少。
在未设置 cap 的声明载荷下，前面只剩 238 token，约等于可用信封的 2%。

`scenarios.v2.json` 的 `delivery` 块声明的载荷是
`max_input_tokens=12000`、`evidence_items=3`、`evidence_item_chars=7000`，
其 `rationale` 写明了推导来源（知识包器 `SERVICEMIND_RAG_CONTEXT_TOKENS=8000`、
单条父块上限 `parent_max_chars=7000`），并带一条校验器约束：
**声明了载荷就必须写出依据，且 `evidence_items` 不能配零字符**（否则等于声明了一堆空证据）。

**这个载荷是按配置上限推导出来的，不是对本部署实测值的拟合。** 诚实边界见 §4。

#### 门禁：记忆必须能在该载荷下活下来

四个探针声明了 `must_deliver: true` 与各自的 `delivery_rationale`（产品要求），
未声明者不入门禁——与前两节同样的规则：

| 探针 | 产品要求 |
| --- | --- |
| `p-preference` | 用户偏好必须在提示里，否则助手会按默认习惯而非该用户的习惯作答 |
| `p-procedure` | 已批准程序是作答的主依据 |
| `p-fleet-vpn-causes` | 车队 VPN 根因是判因依据 |
| `p-post-run-tenant-episode` | 后运行情节是本轮唯一可引用的历史结论 |

报告逐条列出每个受门禁探针的「余量 / 实际占用」，两个总量取自这张表
（余量取最紧的一条、占用取最多的一条，**通常是不同的探针**，合并叙述会读成
「某条探针花掉了超过它被允许的量」）：

| 模式 | 探针余量 | 探针实际占用 |
| --- | --- | --- |
| `lexical` | 5738 / 5742 / 5749 | 88 / 137 / 202 / **206** |
| `embedding`(tei) | 5738 / 5742 / 5749 | 370 / 390 / **401** |

启用 cap 后四条门禁都有超过 5700 token 的余量。cap 启用前它们也会因为语料记忆较短
而通过，但仅剩 238–249 token，无法承载真实长度的程序记忆——所以仍需要下面的证伪。

**证伪（把被断言的东西拿掉，必须变红）**：一条测试把 `w-user-preference` 的文本
**重复 50 次**（依据 `_inflate`）：词集合不变 → 相关性不变、名次不变，只有长度变长。
结果 `pruned_required == ['w-user-preference']`、原因 `token_budget_exceeded`、投递门禁 **FAIL**。
门禁因此不是恒绿的摆设。对照测试则证明**未声明** `must_deliver` 的探针即使被裁也不触红。

#### 修复：给批量通道设上限

新增 `source_token_caps`（`ContextBuilder.build` 参数）与新原因码
`source_token_cap_exceeded`，配置项 `SERVICEMIND_CONTEXT_EVIDENCE_TOKEN_CAP`。
规则是**任何批量通道不得占用超过可用信封的一半**，最终值 5000；
`ContextBuilder` 和 Settings 启动校验都会拒绝越过该边界的配置。

| `SERVICEMIND_CONTEXT_EVIDENCE_TOKEN_CAP` | `headroom_tokens` | `pruned_required` | 投递门禁 |
| --- | --- | --- | --- |
| 未设（`None`，出厂默认） | 238 | `['w-procedure-sso']` | **FAIL** |
| `5000` | **5738** | `[]` | PASS |

上表用的是把 `w-procedure-sso` 放大 60 倍后的版本（约 7600 字符 ≈ 3360 token，
即一条**真实长度运行手册**的量级），不是语料中的短句。

三个实现细节值得记下：

- **上限不得把 `required` 控制项变成预算错误。** `output-schema` 等控制项若因上限
  装不下而抛 `ValueError`，会把这一个**通道策略决定**报成一次**超预算运行**，
  把运维引向错误的排查方向。因此 `required` 项直接跳过上限判定。
- **只约束 ANALYSIS。** 第一版接线把 cap 同时传给 ANALYSIS 和 REVIEWER，但 REVIEWER
  不读取 Memory；在它的信封上裁 evidence 没有任何投递收益，反而削弱复核。现已限定为
  `agent is ContextAgent.ANALYSIS`，并用同一条长 evidence 证明 ANALYSIS 因 cap 裁剪、
  REVIEWER 仍完整选择。
- **产品默认与本部署分开。** Settings 默认仍为 `None`，便于其他部署显式决策；本部署
  `.env` 已按裁定设置为 `5000`，`scenarios.v2.json` 同步冻结该值，因此标准报告直接验证
  已启用形态，不再只靠测试中的临时覆盖。
- **「半额」这条规则有三处算术，已收敛为两处 + 一条钉子。** 保留量 `256`/`1024` 原本
  在 `ContextBudget` 字段、`ContextBuilder.build` 参数、`Settings.model_post_init`
  里各写一遍。前两处已改为引用 `context.contracts` 的单一常量定义；第三处**必须保留副本**
  ——`core` 是脚手架层，不得反向 import 产品层。副本的代价是漂移：若保留量变大，
  启动校验会继续按**过期的信封**放行一个 cap，而失败点从启动推迟到一次**真实分析请求**。
  因此新增契约测试 `test_the_startup_cap_bound_is_the_same_number_the_builder_enforces`，
  从两侧断言同一个边界值（不比较常量，而是真的让两侧各自判定边界与边界+1）。
  **证伪**：把 `DEFAULT_SYSTEM_RESERVE` 改成 512，该测试立刻在
  「settings 应当拒绝 5361 却放行」处变红；改回后全绿。

#### 本轮未修的缺陷（记录在案）

离线词法回退对**长文档**存在长度稀释：`_lexical_similarity` 是词集合 Jaccard，
长文档的并集巨大，相似度被摊薄到 `min_lexical_similarity=0.05` 之下。
实测一条 3886 字符的真实运行手册得分为 **0.0340 < 0.05**，**连召回都过不了**。
生产路径用嵌入，因此这**只影响离线/CI 路径**；尝试把该手册加进语料会打破
`lexical` 模式下的既有契约，故语料保持原样，该发现记录于此。

## 4. 诚实边界（不可越界声称）

- 语料由本项目编写，是**回归契约**，不是租户真实记忆分布，也不是生产流量回放。
  它证明「实现符合设计」，不证明「在真实租户数据上质量达标」。
- 排序块在离线模式下用的是词法 Jaccard，**显著弱于**生产的嵌入路径；
  安全块与排序块分开正是为了让前者的结论不依赖后者。
  报告中的 `lexical` / `embedding` 字段标明本次跑的是哪一条。
- **排序块的 MRR@k 与 NDCG@k 仍为 1.0，不代表排序质量优秀**，只代表当前语料下
  每条查询的第一位都命中相关记录。排序质量的**门禁**由 `RankGate` 承担（§3.2），
  数值本身不应被引用为质量证据。
- 配对对照证明的是「被测属性是唯一的差别」，**不是**「该属性在生产流量中总会以这种形式出现」。
  7 条播种记录是**构造出来的前提**，不是观测到的历史数据。
- **投递载荷是「按配置上限推导的声明」，不是对本部署的观测。** 2026-09-16 实测：
  `knowledge_documents` / `knowledge_parent_chunks` / `knowledge_child_chunks` /
  `knowledge_ingestion_jobs` **全部 0 行**，gold 语料每篇约 963 字符。Context artifact 中
  只有 13 条 `phase5-live-verifier` 合成验收记录，排除后真实 ANALYSIS 信封为 **0**。
  因此 §3.5 的无上限基线 238 / 10720 与启用后的 5738 都是「在声明的载荷形态下会发生什么」，
  不是「生产环境现在损失了多少条记忆」。现已新增
  `scripts/report_phase5_context_delivery.py`，直接从 append-only selection manifest 统计
  evidence/Memory 的选择、裁剪、cap 命中及“检索到但全部未投递”比率；它默认排除已知
  验收身份，0 条真实数据返回 `NO_DATA`/退出码 2，**不会把无流量渲染成 PASS**。
- 时间以锚点偏移表达，语料不随真实时间腐化；每次运行记录锚点，指标可复现。
- 断言的严格程度：写侧原因码采用「列出的必须全部出现」而非精确元组相等，
  因为仓储层会追加自己的码（`VERSION_CONFLICT`、`SEMANTIC_DUPLICATE_SUSPECTED`），
  精确相等会让语料在任何无关新码加入时误报。
- 同分顺序已确定化：仓储候选截断和 `MemoryRetriever` 最终排序都以
  `content_hash + idempotency_key` 破同分。前者稳定内容身份，后者进一步区分
  内容相同但 subject/scope/source run 不同的合法记录；随机 `memory_id` 不再参与排序。
  契约测试刻意把 UUID 顺序设成稳定身份顺序的反向，并把候选上限压到 1，证明候选截断
  与最终排序都不会退回 UUID4 顺序。

## 5. Procedural 生产与人工复核路径（已修复）

审计起点是：`MemoryType.PROCEDURAL` 有完整的契约、策略与激活守卫（必须人工复核、必须有两个
**不同来源运行**的可验证情节支撑、作用域必须匹配），`grep` 全仓生产代码却找不到任何
产生 `procedural` 候选的地方：`src/servicemind/orchestration/phase5_governance.py` 的
后运行提取器只产生 `EPISODIC`，且写的是 **USER 作用域**。

这带来两条事实：

1. 程序记忆在真实流量中**永远不会产生**，是一条已建模、已测试、未接线的路径。
2. `_validate_activation` 要求支撑情节「作用域等于该程序记忆、或为 TENANT」，
   而后运行提取器产出的情节是 USER 作用域。**若将来让提取器产出程序记忆
   （其自然作用域是 TENANT），这两个规则互不满足，激活会永远失败。**

### 5.1 修复：提取器改为产出 TENANT 作用域情节（2026-09-16 裁定）

项目方裁定「让提取器产出 TENANT 作用域情节」，已落地：

- `phase5_governance.py` 的后运行提取器改用 `MemoryScope(scope_type=TENANT)`，
  并在 `provenance` 中写入 `post_run_scope: "tenant_episode_v2"`；
- 该标记是**必须的**，不是装饰：`MemoryQuery.allows_record` 与仓储层
  `_read_filters` 对「`created_by == post-run-memory-middleware` 且作用域非 USER」
  的行有一道读取保护（补丁 0010 隔离了 Phase 5.1 之前被无审扩大作用域的遗留行）。
  新写入若不声明，就会与遗留行一起被挡在读路径外——即修复了 procedural 的同时
  打瞎了 episodic。声明式判别器让两者分开：**旧行无标记 → 继续不服务；新行有标记 → 服务**。
- 作用域放宽并未放宽可见性：新行仍带 `required_entity_ids` / `required_group_ids`，
  读侧按调用方的 GLPI 实体与组做子集过滤。

### 5.2 修复的端到端证据

`tests/servicemind/test_phase5_governance.py` 新增三条测试：

| 测试 | 断言 |
| --- | --- |
| `test_extracted_episodes_are_tenant_scoped_and_reach_other_users` | 提取出的情节是 TENANT 作用域且带 `post_run_scope` 声明；**另一个用户**能检索到；同一用户**缺实体 ACL** 时检索不到 |
| `test_a_legacy_tenant_summary_is_still_never_served` | 把同一行的声明标记去掉，它立刻退回到「不服务」——被测的是**声明**，不是写着身份 |
| `test_a_tenant_scoped_procedure_can_cite_extracted_episodes` | 两次真实 `post_run` 产出的两个情节，可以被一条 TENANT 作用域的程序记忆引用并**成功激活** |

**证伪（把修复回退，测试必须变红）**：临时把提取器改回 USER 作用域后重跑，
`test_a_tenant_scoped_procedure_can_cite_extracted_episodes` 与
`test_extracted_episodes_are_tenant_scoped_and_reach_other_users` **双双失败**，
失败点正是 `repository.py:174` 的
`PermissionError: procedural memory requires distinct verified accessible episodes`。
这条错误就是原缺口的位置本身：两个各自合理的规则互不满足，程序记忆不可构造。
恢复修复后三条测试全绿（`git diff` 确认工作树已还原）。

本语料也用「策展情节 + 租户作用域」建模程序记忆，并在
`test_the_procedural_transition_actually_ran` 中断言该路径确实可走通。

### 5.3 接生产者之前必须先知道的约束：同一张工单供不出两个支撑情节

激活守卫要求程序记忆至少有两个**来自不同 run** 的支撑情节。最自然的接法是
「同一张工单的两次成功 Run」，但这条路**走不通**，而且两种结局都走不通
（实测，见 `test_one_ticket_cannot_supply_two_supporting_episodes`）：

| 第二次 Run 的结论 | 落库结果 | 可用支撑情节 |
| --- | --- | --- |
| 与第一次**相同** | 1 条（同 subject_key、同作用域、同内容 → 幂等去重） | **1** |
| 与第一次**不同** | 2 条，第二条 `quarantine`（`VERSION_CONFLICT`） | **1**（`visible_at` 要求 `status is ACTIVE`） |

两条规则各自都对：重复内容本就该幂等，冲突版本本就不该有两个存活。合起来的后果是
**生产者的分组键必须跨越工单**（同一个模式在**多张**工单上重复出现），
而不是一张工单重复多次。

这条与 §5.1 是同一类问题——两条各自合理的规则合起来把一条路堵死——所以这次
**在动手之前先把它变成断言**，而不是等接完生产者再发现它永远激活不了。

### 5.4 生产者接线：只从跨工单重复模式提出候选

后运行中间件现已接入保守的 `cross_ticket_verified_episode_v1` 生产器：

1. 只有 Reviewer 已通过、终态已核验、证据已核验且 `recurring_incident=true` 的 Run
   才能进入模式匹配；
2. 模式键只包含规范化后的 `classification`、`recommended_group`、
   `problem_recommendation` 与 `change_recommendation`。工单号、自由推理、工具参数和资源 ID
   均不进入可复用程序，避免把单工单细节升级为企业做法；
3. 匹配采用精确规范化摘要，优先保证精度。至少两个 **不同 ticket、不同 run** 的 active
   TENANT 情节同时可见，才生成一条 PROCEDURAL 候选；其 ACL 是支撑情节 entity/group
   要求的并集；
4. 候选始终由 `MemoryGovernancePolicy` 放入 `quarantine`，保留 180 天审核窗口，
   `SERVICEMIND_MEMORY_PROCEDURAL_PROPOSALS_ENABLED` 可独立回滚；当前部署已启用；
5. 激活时重新读取全部支撑情节，并再次核对 active、证据、作用域、不同 run、不同 ticket
   与相同模式键。撤销任一支撑证据仍会级联撤销该程序。

正向契约证明两个不同工单会产生且只产生一条待审程序；反向契约证明不同建议不会被合并。
原有同工单两种结局的参数化测试继续作为生产者边界。

### 5.5 待审队列与原子决策

新增两个受 `approver` 角色保护的端点：

- `GET /v1/servicemind/memories/review-queue`：按 `(created_at, memory_id)` 键集分页，可按
  `memory_type` 过滤，只列出当前租户、尚在有效期内且 entity/group ACL 都是审批人授权
  子集的 quarantine 候选；Procedural 与其他因冲突/低置信度进入隔离区的记忆共用该审计面；
- `POST /v1/servicemind/memories/{memory_id}/review`：激活或拒绝，必须提交
  `expected_version`、`expected_content_hash`、`review_ref` 与审核意见。

版本、内容摘要和**期望状态 `quarantine`** 在 PostgreSQL 行锁与租户 advisory lock 内一起
验证。并发证伪最初发现只绑定版本/摘要仍允许“先激活、再由旧页面撤销”，因为状态迁移不会
改变内容版本；增加状态快照后，同一候选的并发相反决策严格只有一个成功。审计事件记录
审批人、决定、引用、意见、被审核版本与摘要。

OIDC 新增 `glpi_group_ids` claim，缺失时按空集合处理；不会为了让队列可用而忽略组 ACL。
Keycloak 已存在的 realm 不会因 `--import-realm` 自动更新，故新增幂等核验/收敛命令
`scripts/reconcile_phase5_memory_review_identity.py`。当前部署已为 `acme-approver` 配置实体 1
下的组 1/2，实际令牌已复验同时含 tenant、entity、group 与 `approver` 角色。

该命令最初只收敛 claim（profile、mapper、user attributes），未覆盖 realm role；而队列以
`require_role("approver")` 为准，realm import 又只在创建期分配角色，因此"事后被撤销 approver"
会让队列稳定返回 403 且不触发任何告警。现已把 seeded reviewer 的 realm role 纳入双向比对
（缺失 = 403，多出的未声明角色 = 越权），并修正状态口径：apply 会实际写回声明的
tenant/entity/group attributes，修改 mapper/profile/attributes/roles 后逐项重读验证；只有后置状态
完全收敛才报告 PASS，无法修复的（identity 缺失、realm 未定义该角色）仍退出 1。实测证伪：撤销 `approver` 后
`--check` 由 PASS 转 FAIL（rc=1），apply 恢复后 `--check` 复绿；授予未声明的 `tenant_admin`
同样被检出并在 apply 中移除。回归测试见
`tests/servicemind/test_phase5_review_identity_reconcile.py`（11 项，使用路由桩，不需要真实 Keycloak）。

控制台入口为 Streamlit 的 **Memory Review** 页面。它通过 Authlib/Streamlit OIDC authorization
code 流程登录已有 Keycloak public client，仅把短期 access token 用于调用上述 API；token 不写入
应用日志、URL、页面状态或错误消息。部署端 `.streamlit/secrets.toml` 权限为 `0600` 且被
`.gitignore` 排除，仓库只保留无凭据示例。页面没有本地角色判断来替代 API：真实授权仍由 API
验证 JWT 的 tenant/entity/group/realm role。每次决定使用页面加载时的不可变快照，服务端返回
409 时要求刷新，不会自动重试一个审批写入。

### 5.6 支撑证据失效联动

原实现只在 `QUARANTINE → ACTIVE` 时调用 `_validate_activation`。Episode 的默认 TTL 为 90 天，
Procedure 为 180 天，所以两个支撑 Episode 全部过期后，Procedure 仍可能继续服务 90 天。
这不是有意的知识耐久策略：它会让 `supporting_episode_ids` 指向不可服务记录，并切断审计论证链。

修复将激活条件抽成唯一的 `_procedural_support_failure` 判定，并同时用于：

- 内存仓储 `candidates()` 与最终 `revalidate()`；
- PostgreSQL 候选读取与最终 `revalidate()`，只锁定本次可能返回的 Procedure，避免每次检索
  扫描并锁住全租户全部 Procedure；
- 人工激活路径，确保写时与读时不会产生两套条件。

失效采用 `ACTIVE → REVOKED`，provenance 记录原因与时间，PostgreSQL 同事务追加
`memory.revoked / PROCEDURAL_SUPPORT_INVALIDATED` 事件。它不删除内容或篡改历史审核，后续若
相同做法再次获得新的跨工单证据，仍必须生成新候选并重新审核。

证伪覆盖两条独立服务入口：把已激活 Procedure 的两个支撑 Episode 改为过期，分别直接调用
`candidates()` 和 `revalidate()`，两条路径都必须不返回 Procedure，并断言 Episode=`EXPIRED`、
Procedure=`REVOKED`。PostgreSQL 在线验收另外在真实事务中缩短两条支撑的窗口，证明普通读取会
产生且只产生一个失效事件。

## 6. 复现

```bash
# 离线评测（无数据库、无模型服务、无网络）
# --check 在以下五种情况任一发生时报错退出：泄漏、越权激活、排名门禁违规、必需记录缺失、
# 投递门禁违规（声明了 must_deliver 的记忆被预算裁掉）
PYTHONPATH=src .venv/bin/python scripts/evaluate_phase5_memory.py --check

# 契约测试（含全部证伪/活性证明，见 §3.4）
PYTHONPATH=src .venv/bin/python -m pytest tests/servicemind/test_phase5_memory_evaluation.py -q

# Procedural 跨工单生产、ACL 待审、陈旧快照与并发双审
PYTHONPATH=src .venv/bin/python -m pytest \
    tests/servicemind/test_phase5_governance.py \
    tests/servicemind/test_phase5_memory_review_api.py -q

# PostgreSQL 支撑寿命联动、append-only 失效事件和其他在线治理门禁
set -a && source .env && set +a
PYTHONPATH=src .venv/bin/python scripts/verify_phase5_governance.py

# Streamlit 审批 client 的 token 隐私、分页与 exact-snapshot 请求
PYTHONPATH=src .venv/bin/python -m pytest tests/servicemind/test_memory_review_ui.py -q

# 已存在 Keycloak realm 的 claim/profile/审批人组授权漂移检查
.venv/bin/python scripts/reconcile_phase5_memory_review_identity.py --check

# 复现已发布的 v1 结论
PYTHONPATH=src .venv/bin/python scripts/evaluate_phase5_memory.py \
    --corpus evaluation/memory/scenarios.v1.json

# 可选：改用生产嵌入提供方跑排序块
PYTHONPATH=src .venv/bin/python scripts/evaluate_phase5_memory.py --embedding tei

# 生产投递观测：0=PASS、1=FAIL、2=NO_DATA/样本不足；不导出提示内容或内容哈希
PYTHONPATH=src .venv/bin/python scripts/report_phase5_context_delivery.py \
    --tenant-id 11111111-1111-4111-8111-111111111111 --since-hours 720 --check \
    --output evaluation/reports/phase5_context_delivery_observed_latest.json

# 当前 Linux 部署：核对 user unit、ExecStart、工作目录、.env 端口和 /health
PYTHONPATH=src .venv/bin/python scripts/verify_servicemind_runtime.py --check \
    --output evaluation/reports/servicemind_runtime_latest.json
```

报告写入 `evaluation/reports/phase5_memory_latest.json` 与 `.md`；
`--embedding tei` 模式写 `phase5_memory_tei_latest.json` 与 `.md`（需 :8085 上的嵌入服务）。

投递门禁与批量通道上限的实测复现（不依赖数据库）：

```bash
# 门禁变红：把一条必需记忆放大到装不下（词集合不变，只变长）
# 门禁变绿：SERVICEMIND_CONTEXT_EVIDENCE_TOKEN_CAP=5000 后重跑，headroom 238 → 5738
PYTHONPATH=src .venv/bin/python -m pytest \
    tests/servicemind/test_phase5_memory_evaluation.py -q -k delivery
PYTHONPATH=src .venv/bin/python -m pytest \
    tests/servicemind/test_phase5_governance.py -q -k cap
```

## 7. 裁定与闭环（2026-09-16）

1. **注入标记统一：已执行。** 原写侧 8 条、读侧 23 条的真子集关系已删除。
   `INJECTION_MARKERS` 现在是唯一规范表，`contains_injection_marker` 是 Memory 写入、
   Memory 读取和工具参数策略的共同判定入口。原来会以 ACTIVE 落库的 `you are now`、
   `disregard previous`、`forget previous`、`忽略系统提示` 四条评测载荷现在全部进入
   `quarantine`；写侧实测因此从 activate 40 / quarantine 6 变为 36 / 10。
   读侧硬阻断继续保留，用直接播种的迁移前 ACTIVE 行证明纵深防御没有因统一而消失。
2. **同分排序确定化：已执行。** 仅换成 `content_hash` 仍会在“相同内容、不同业务身份”
   的记录间碰撞，因此最终键冻结为 `(-score, content_hash, idempotency_key)`；内存仓储与
   PostgreSQL 的候选截断也使用相同的两个稳定身份字段替代 UUID4。候选集合和集合内顺序
   均不再依赖随机标识。

3. **投递层：已裁定并在本部署启用。** `SERVICEMIND_CONTEXT_EVIDENCE_TOKEN_CAP=5000`
   已写入当前 `.env`；产品出厂默认仍为 `None`。cap 只作用于 ANALYSIS，REVIEWER 不读取
   Memory，因此保持完整 evidence 信封。代码同时拒绝任何超过可用信封一半的 cap，
   lexical 与固定 revision TEI 报告均在 5000 下得到最紧余量 5738、4/4 投递通过。

4. **生产观测与运行入口：已接线。** 投递观测器使用租户 RLS 会话读取 append-only
   `context_artifacts`，不复制 prompt、content hash 或 provenance；门禁采用
   `PASS / FAIL / NO_DATA|INSUFFICIENT` 三态，并默认排除合成验收身份。当前报告为
   `NO_DATA`：13 条合成 artifact 已排除，真实 ANALYSIS 信封为 0。
   「不导出」这条由**结构化断言**兜住，而不是靠评审记得：
   `test_the_report_carries_no_item_id_content_hash_or_provenance` 把哨兵值放进
   `item_id` 与 `content_hash`，要求序列化后**一处都不出现**，并递归检查报告的全部字段名
   不得命中 `content / hash / provenance / item_id / memory_id / prompt / text / ticket`。
   理由是这两个字段本身就是反向指回租户数据的把手（`item_id` 内嵌 `memory_id`，
   `content_hash` 是文本的稳定指纹），而**数字层面看不出任何异常**。
   **证伪**：给报告加一个 `sample_content_hash` 字段，该测试立刻以
   `assert not ['content', 'hash']` 变红；移除后全绿。API 的权威入口也已固定为
   user unit `servicemind-api.service` + `127.0.0.1:18080`，机器报告逐项核对 unit scope、
   MainPID、ExecStart、工作目录与健康响应，避免再用系统级 unit 或 8080 误判。

5. **Procedural 生产与人工复核：已接线。** 生产者只聚合不同工单上的相同规范化建议，
   生成 quarantine 候选；审核队列执行角色、租户、entity/group ACL，并把决定原子绑定到
   `version + content_hash + quarantine status`。相反并发决策只有一个能成功，Keycloak
   group claim 的配置漂移有独立 `--check` 门禁；OIDC Streamlit 页面补齐了可操作入口。

6. **Procedural 支撑寿命：已闭合。** Procedure 允许保留 180 天不再表示它可脱离 90 天
   Episode 独立服务；候选读取和最终重校验都会复用激活守卫，在支撑失效时先撤销、留审计，
   再把候选交给模型。

以上六项均有回归契约或机器报告。§4 所述「尚无真实流量样本」仍是明确的证据边界：
路径已经可执行，不等于本部署已观察到真实 procedural 候选或人工审批样本。
