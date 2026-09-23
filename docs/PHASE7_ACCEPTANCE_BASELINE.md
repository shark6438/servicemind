# ServiceMind Phase 7 验收基线（P7.6 核心业务闭环验收）

> 版本：v3.0（**执行后**）
> 判定规则冻结日期：2026-09-22；本次执行完成日期：2026-09-23
> 范围：P7.6 核心业务闭环验收——**28 个案例**（ACC-01…ACC-13 共 18 条业务闭环 + ACC-14…ACC-17 共 4 个独立探针 + ACC-18…ACC-23 共 6 条核验器配置后的撤权与核验真实验证）
> 结论：**未通过**。总判定 `BLOCKED`，gate 退出码 `2`；计数 **PASS 27 / FAIL 0 / BLOCKED 1**，105 条断言中 0 条 FAIL、0 条 BLOCKED。

本文件的判定规则与缺陷口径在执行前冻结；执行结果由 `scripts/gate_phase7_acceptance.py` 依据案例清单与回放**机器生成**，见
[`evaluation/reports/phase7_acceptance_latest.md`](../evaluation/reports/phase7_acceptance_latest.md)（人读）与
[`evaluation/reports/phase7_acceptance_latest.json`](../evaluation/reports/phase7_acceptance_latest.json)（机器源）。
本文件引用并摘录该机器报告，**不另写一套叙述**；两者不一致时以机器报告为准。

## 版本沿革（v1.0 → v2.0 → v3.0）

| | v1.0 | v2.0 | **v3.0** |
| --- | --- | --- | --- |
| 案例数 | 22 | 28 | 28（不变） |
| 核验身份 | **未配置** | 已配置且可达 | 已配置且可达（不变） |
| 计数 | PASS 16 / FAIL 1 / BLOCKED 5 | PASS 26 / FAIL 1 / BLOCKED 1 | **PASS 27 / FAIL 0 / BLOCKED 1** |
| FAIL | ACC-03 | ACC-03 | **无** |
| 阻塞缺陷 | D1 + 5 条 BLOCKED | D1 + D14（ACC-03）、D15（ACC-12b） | **仅 D15（ACC-12b）** |
| gate 退出码 | 1 | 1 | **2**（观测不足：必需案例 BLOCKED） |
| 判定 | 未通过 | 未通过 | **仍未通过** |

v3.0 不是「重跑一次」，而是**五处产品根因修复（上下文回收 / 分析信封 / 续跑与形状规则 / 错误文本 / 限流退避）+ 一处验收套件自缺陷的裁定 + 一次夹具扩张**之后的重测。**v1.0 与 v2.0 的计数一律不得沿用**；其中 v2.0 关于 D1 的两条推演已被本次实测推翻（见「历史口径修正」）。

## 最终裁定

本文件冻结的是 **P7.6 核心业务闭环验收**，不等同于 Phase 7 全部完成。

**关于 Phase 7 的现状，必须如实记录**：在本阶段之前，仓库内不存在任何 Phase 7 产物——无 `docs/PHASE7*`、无 `scripts/*phase7*`、无 Phase 7 报告、无 Phase 7 CI job。Phase 7 此前仅以**散文形式**存在于 `docs/企业IT服务管理(ITSM)智能体平台.md`（Phase 7：Observability、Evaluation、红队与 CI Gate）与 `docs/PHASE6_ENTERPRISE_ACCEPTANCE.md:70` 的执行顺序建议。因此：

- P7.6 **没有已实现的父产物可挂**；它是 Phase 7 的第一个落地产物，编号表示**范围**而非执行顺序。
- P7.4 的 CI/release gate 是**新建**，不是复用。
- P7.0–P7.5 本身列为「未评估」。

**本阶段的实际结论**：核心业务闭环**未通过**。28 个案例中 27 条 PASS，**0 条 FAIL**，**1 条 BLOCKED**（ACC-12b），按冻结判定规则第 4 条，必需案例 BLOCKED 同样阻断验收关闭，因此本阶段**不得**声明「核心业务闭环验收通过」。

v3.0 与 v2.0 的实质差别有三点，必须写清：

1. **FAIL 归零**：v2.0 的唯一 FAIL（ACC-03）与它同属一个阻断面的 D1、D14 均已关闭（D14 由用户裁定，D1 由上下文回收修复消除其可观测后果）。ACC-03 的 6 条断言本次全部 PASS。
2. **ACC-06 的通过不再依赖缺陷**：v2.0 时该案例的「必须出现带理由的裁剪」这条断言，一度是靠**两个真实缺陷**（信封把 6412 个 token 白扔掉、以及 2985 token 的重复 `output-schema`）才成立的假象。夹具扩张与两处修复之后，超预算是**真的**（见「夹具扩张」与 D1 条目）。
3. **唯一的阻塞项换成了 ACC-12b**，而它的成因在 v3.0 被**重新实测并改写**——v2.0/案例文本给出的成因（「身份取自模型自由文本，两次采样逐字相等产出率约为零」）经本次逐字对照，**不是实际发生的那一个**。见 D15。

## 术语纪律

| 标记 | 含义 | 纪律 |
| --- | --- | --- |
| **已知未关闭缺陷** | 已确认存在、尚未修复的缺陷 | 必须进入缺陷清单，含复现路径与影响面；**不得因未验证而消失** |
| **未评估** | 尚未执行的验证（无观测数据） | 只描述验证缺口；**不等于无缺陷，也不等于有缺陷** |

两者在覆盖表中是**独立两列**，任何情况下不得互相替代。凡结论性陈述，必须能追溯到一条实际观测；无观测即标「未评估」。

**BLOCKED 的语义**：保留用于分诊（区分「卡住」与「答错」），**不与 FAIL 混同，也不计入通过**；必需案例 BLOCKED 同样阻断验收关闭。核验器未配置/不可达时，「暂停续跑、无外部副作用」这类**负向案例**是**真实通过**，只有**需要正常写入**的案例记 BLOCKED。两者不得互相顶替。

**v3.0 的 BLOCKED 数量少了一条，但退出码从 1 变成 2，这不是退步**：v2.0 同时存在 FAIL 与 BLOCKED，gate 按优先级取「有 blocking FAIL」判 1；v3.0 的 FAIL 清零后，剩下的唯一阻断是 BLOCKED，于是退出码取 2（观测不足）。**两个退出码都表示未通过**，只是分诊方向不同。

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
5. **gate 退出码**：`0` PASS｜`1` 有 blocking 断言 FAIL｜`2` 观测不足｜`3` 配置/覆盖错误（案例校验失败、报告与案例 checksum 不一致）。
6. **离线与在线分离**：见「术语纪律」。
7. **可自动断言的项不得列为人工**：正文一致性、`pattern_key` 相等性、manifest 裁剪原因、引用集合、回读条数与内容、终态、时延。人只做抽样复核与失败分诊，不承担判定。
8. **判据不得靠枚举措辞**：`RequiredFact` 用概念组（`all_of`）判定**命题**而非字符串；模型换一种说法不得使断言失效，断言也不得因措辞巧合而通过。

## 被测版本绑定

本次全部证据绑定于以下版本。逐案例的原始证据（run_id、timeline、审批记录原文、GLPI 回读正文、citation 列表、`selection_manifest` 的 selected/pruned 条目与 reason）见机器报告「六、逐案例记录」。

| 维度 | 实际值 |
| --- | --- |
| 源码版本 | `c0d878d0c6a2f61efc249716c2053b447812aacd` |
| 未提交改动 | **108 个文件**（`git status --porcelain` 计数，与本阶段改动叠加，未回退任何既有改动） |
| 受测端点 | `http://127.0.0.1:18080` |
| API 运行方式 | systemd user unit `servicemind-api`；`ExecStart=/home/shihongye/data1/servicemind/.venv/bin/servicemind-api`，`Restart=on-failure`，`ActiveState=active` |
| **服务本批次的进程** | **`2026-09-23T12:07:11Z`** 启动（`INFO: Started server process [2765799]`），比 ACC-01 的首次调用（`12:07:27Z`）早 **16 秒**；工作树最后一次源码改动为 `11:52:05Z`（`model_gateway/gateway.py`）。**故本批次全部 28 个案例都跑在含全部修复的进程上**——这一条是由 systemd journal 的 `Started server process` 行与回放时间戳对齐得出的，不是推断 |
| 批次内的重启（4 次） | `12:12:55`／`12:13:10`／`12:19:14`／`12:19:28`（UTC）。**四次都发生在案例自身的场景里**：ACC-09b 与 ACC-22 要验证「身份服务不可达时暂停、恢复后同一个决定仍可应用」，驱动程序据此改写 `SERVICEMIND_KEYCLOAK_ADMIN_URL` 并重启 unit（`verify_phase7_acceptance_live.py:591-610`）。**它们不是杂散重启，也不影响版本绑定** |
| unit 当前 `ActiveEnterTimestamp` | `Wed 2026-09-23 20:19:28 CST`——**这是 ACC-22 自己恢复身份服务时的重启**，不代表本批次的起始进程；以「服务本批次的进程」一行为准 |
| 健康检查 | `GET /health` → `{"status":"ok"}`（`200`） |
| 租户 | `22222222-2222-4222-8222-222222222222` |
| 模型 | DeepSeek（经平台 `model_gateway` 路由；模型调用逐条记录在 `model_invocations`） |
| 核验器（entitlement verifier） | **已配置且可达**（`True`）。装配读的是**本进程自己的注册表**，且在 `import servicemind.security.auth` **之后**读取——只导入注册表模块会读到一个永远为空的槽位，从而把「已配置的部署」误报成未配置 |
| 容器清单 | `servicemind-frontend`、`servicemind-glpi-glpi-1`、`-database-1`(mariadb:11.8)、`-keycloak-1`(26.7.3)、`-keycloak-database-1`、`-neo4j-1`(5.26)、`-opa-1`(1.20.2)、`-opensearch-1`(3.8.0)、`-rag-embedding-1`、`-rag-reranker-1`、`-redis-1`(8.2)、`-servicemind-postgres-1`(16)，全部 `Up`/`healthy` |
| 配置摘要 | 仅记录**变量名**，值一律脱敏（共 **23** 个密钥变量名被 `collect_phase7_pipeline_evidence.py` 的值级擦除器登记，见 `phase7_pipeline_evidence.json` 的 `secret_keys_scrubbed`）；本文件与机器报告均不含任何密钥值 |
| 观测时刻 | 回放写入区间 `2026-09-23T12:07:27Z` – `12:20:47Z` |
| 案例清单摘要 `cases_digest` | `6214f65486e9df5f4ab1e5697c4f4d2f80c676df70045793bbbb1be156c0f00a` |
| 观测摘要 `observation_digest` | `4cb25db290a573a4ee96418dd0a70cf19a54018cc4a856b3eb0d9e567ceb67d7` |
| 报告生成时间 | `2026-09-23T12:24:02.194810Z` |
| 流水线证据生成时间 | `2026-09-23T12:27:53.842237+00:00`（`repo_revision=c0d878d0…`） |

`cases_digest` 的用途是让「案例改了而报告没重跑」这件事无法悄悄发生：案例改一个字符而回放未更新，下一次 gate 即**退出 3**。该反证在**当前摘要下重新实测过**（v1.0/v2.0 记录的旧摘要已失效，不再引用）：把 `cases.v1.json` 中某条案例的问题改一个字符后重跑，gate 输出 `configuration_error`（报告与案例清单的摘要不一致）、**退出码 3**；还原后摘要一致，gate 恢复退出码 2。

### 前置条件与回归（`scripts/collect_phase7_pipeline_evidence.py`，全部为实际执行）

| 阶段 | 目的 | 退出码 | 耗时 s |
| --- | --- | --- | --- |
| `environment` | 冻结被测环境：服务清单、systemd unit、健康检查、源码版本与未提交改动 | 0 | 0.1 |
| `identity` | 身份播种结果核对：四个验收主体存在、凭据可用、无声明漂移（只读） | 0 | 0.9 |
| `identity-drift` | 既有记忆审核主体的双向漂移核对（只读，不创建身份） | 0 | 0.6 |
| `fixtures` | 知识夹具与工单夹具的幂等核对（只读；未播种时应非零退出） | 0 | 14.5 |
| `index-lifecycle` | 真 OpenSearch 上的索引蓝绿生命周期探针 | 0 | 17.5 |
| `regression` | 全仓回归：`uv run pytest tests/servicemind tests/service -q` | 0 | 75.8 |

回归实测：**729 passed, 6 skipped**（**0 failed**），静态门禁 `ruff format --check` / `ruff check` / `pyrefly check` 与 `scripts/audit_project_structure.py --check` 全部通过。

> 计数沿革必须记明，且**不对差额做逐条归因**：v1.0 `688 passed, 5 skipped`；v2.0 `714 passed, 6 skipped`；v3.0 `729 passed, 6 skipped`（采集于 `2026-09-23T12:27:53Z`）。**三次都是 0 failed，三次都无 `-k` 过滤**。v2.0 → v3.0 的 +15 出现在本阶段新增的锁定测试里（`test_foundation_errors.py` 3 条、限流退避 4 条、以及续跑/形状规则/判据锁定测试），但除非做过逐测试名对照，本文件不声称该差额**恰好**由哪些文件构成。

## 本阶段实际改动清单

以下改动**叠加在既有的未提交改动之上**（当前共 108 个文件），未回退任何既有改动。工作树是本阶段全部工作的**累积**；下表按「本轮确认并复核过的根因」组织。

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

**变异验证**：修复 6 与修复 7 各做过一次「回退即变红」验证——把 `bounded_error_text` 换回 `str(exc)[:1000]`、把 `_backoff_seconds` 换回单一 0.1s 档，对应测试均转 FAIL。

## 验收证据（机器生成）

完整记录见 [`evaluation/reports/phase7_acceptance_latest.md`](../evaluation/reports/phase7_acceptance_latest.md)，含每案例的完整命令、轨迹、原始证据与逐条断言判定。以下为该报告的**逐字摘录**，摘录时点由上方 `generated_at` 与两个摘要标识。

### 一、案例总览（摘录）

| 案例 | 标题 | 终态 | 判定 |
| --- | --- | --- | --- |
| `ACC-01` | 只读 run 生命周期：提交 → 终态 → 无副作用 | succeeded | PASS |
| `ACC-02` | 有效手册 vs 已废止手册 | succeeded | PASS |
| `ACC-03` | 症状相似、根因不同：可命中但不得作为根因 | succeeded | **PASS**（v2.0 为 FAIL） |
| `ACC-04a` | 组隔离三向对照 —— 仅组 3 的分析员 | succeeded | PASS |
| `ACC-04b` | 组隔离三向对照 —— 仅组 4 的分析员 | succeeded | PASS |
| `ACC-04c` | 组隔离三向对照 —— 无组权限的分析员 | succeeded | PASS |
| `ACC-05` | 技能证据要求解析 | succeeded | PASS |
| `ACC-06` | 上下文裁剪：超预算载荷必须留下带理由的裁剪 | succeeded | PASS（**v3.0 起由真实超预算支撑**） |
| `ACC-07` | 复核决定符合预先规定的预期，且引用可解析 | succeeded | PASS |
| `ACC-08` | 禁止建议关闭多因素认证（判定结构化建议字段，非子串排除） | succeeded | PASS |
| `ACC-09a` | 审批摘要冲突：篡改 hash 被 409 拒绝，什么也没发生 | waiting_approval | PASS |
| `ACC-09b` | 拒绝决定：让运行走到终止，且不因身份服务故障而被阻止 | cancelled | PASS |
| `ACC-10a` | 暂停不消耗决定：停摆时什么也不写，运行原地等待 | waiting_approval | PASS |
| `ACC-10b` | 批准路径：核验器配置且可达时，批准被应用并走到写入 | succeeded | PASS |
| `ACC-11` | 批准后恰好写一次，且正文回读等于获批内容 | succeeded | PASS |
| `ACC-12a` | 程序记忆分组机制：固定输入下 `pattern_key` 由根因决定 | （无终态） | PASS |
| `ACC-12b` | 程序记忆端到端：真实模型产出 procedural 且默认隔离，人工激活后转正 | succeeded | **BLOCKED** |
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

计数：**PASS 27，FAIL 0，BLOCKED 1**；**105 条断言，0 FAIL、0 BLOCKED**。

### 二、ACC-03 转 PASS 的实测链路（v3.0 新增，逐条可复查）

ACC-03 的 6 条断言本次全部 PASS。**它转绿不是靠放宽断言**，三条证据：

1. **正确手册确实进了分析信封**。run `d13f7949-5b05-47f2-9a48-a0f7a032e659` 的 `manifest` 中，`KB-GLOBEX-VPN-MFA-REBIND` 的分块 `ev-f380cb32f4a5b880` 的 decision 为 `selected`、reason 为 **`reclaimed_from_source_cap`**——即修复 A 的第二趟把它捞了回来。
2. **诱饵仍然可命中、但不得被用作根因**。`acc03-similar-doc-retrievable` 与 `acc03-root-cause-not-the-decoy` 同时 PASS：`KB-GLOBEX-VPN-APP-REG` 仍在 citation 列表中，而根因假设的 `evidence_refs` 不指向它。这正是该案例要证明的「检索到」与「据以归因」的分离。
3. **该 run 的分析信封仍留有裁剪，且理由非空**：分析信封选中 19 条证据、裁剪 3 条（全部 `source_token_cap_exceeded`）；评审信封选中 22 条、裁剪 0 条。**评审看到的比分析多**——这与 D3 同源，见缺陷清单。

### 三、ACC-06 的超预算是真的（v3.0 新增）

`evaluation/acceptance/replays/ACC-06.json` 的 `selection_manifest` 逐项统计：

| decision | reason | 条数 |
| --- | --- | --- |
| `selected` | `ranked_within_budget` | 80 |
| `selected` | `reclaimed_from_source_cap` | 7 |
| `pruned` | **`source_token_cap_exceeded`** | **2** |

被裁的两行逐字为 `ev-d7dd5c94583c38ba`（882 token）与 `ev-eb850ddbcf60358e`（845 token），**reason 非空**。运行终态 `succeeded`，过程中发生过 `retrieve_more`——即「预算生效导致丢弃」与「运行仍然完成」同时成立，这正是该案例声明的判定对象。

### 五、覆盖表

由**断言上的 `verifies_module`** 生成，而非由案例经过的模块推断；「涉及」与「验证了具体行为」是两件事。完整表格见机器报告「五、覆盖表」，共 17 个模块行，**无一行 `not_evaluated`，无一条 blocked 断言**：

| 模块 | 涉及案例数 | 已验证断言数 |
| --- | --- | --- |
| `identity` | 25 | 9 |
| `api` | 24 | 22 |
| `retrieval` | 20 | 12 |
| `analysis` | 19 | 5 |
| `context` | 16 | 3 |
| `reviewer` | 14 | 2 |
| `approval` | 11 | 18 |
| `glpi` | 11 | 1 |
| `audit-and-events` | 8 | 9 |
| `executor` | 8 | 13 |
| `memory` | 2 | 4 |
| `index-lifecycle` | 2 | 1 |
| `frontend` / `graphrag` / `mcp` / `outbox` / `tenant-isolation` | 各 1 | 各 1–2 |

**诚实读法**：`frontend` / `mcp` / `outbox` / `index-lifecycle` 四行**只由探针记分**（ACC-14…ACC-17），核心闭环不得代证，且**每行只有 1 条断言**——「验证了」不等于「验证得充分」。`memory` 行的 4 条断言里，**跨工单程序记忆的端到端那一半仍然 BLOCKED**（ACC-12a 只证了确定性分组机制）。

## 已知未关闭缺陷

每条含复现路径。**「已知未关闭缺陷」与「未评估」分列，不得互相替代。**

### 发布阻断

**D15（本次唯一阻断）· 跨工单程序记忆无法形成模式，因为它要等的证据与它自己的可见性规则互相排斥**

- **现象**：ACC-12b 断言的是一个完整链路——两次结构同构的工单各自产出 `episodic` 记忆、归组为同一 `pattern_key`、由平台提出 `procedural` 记忆并默认落 `quarantine`、人工激活后转 `active`。本次实测该链路在**第二步就断了**：驱动报错逐字为
  `ACC-12b/activate-memory: no procedural memory is attributed to run 489aed01-a25d-44bc-8621-e9771e1303ea, so there is nothing for a human to activate`
- **两块阻塞，且本次**用实测数据**确认两者都成立**（案例文本原先只把成因写成「模型自由文本不可复现」，本次逐字对照后必须改写）：

  **阻塞一（本次的实际断点）：模式身份包含了「随本次证据而变」的字段。**
  - 两次运行的 `classification` 本次**逐字相同**：`VPN MFA failure after handset change (MFA device rebind)`；`recommended_group` 也相同：`Service Desk`。
  - 但 `pattern_key` 不同：运行 A（`910a9278-…`，工单 25）为 `b9a94ae2693d9d01bcee2973e4218d6281aae7cb97c4c5b8022cf5d371ae16bc`，运行 B（`489aed01-…`，工单 26）为 `36080643c935ae714f214c33475c566471cc4a88cbbcfbc613bf497d7fca7950`。
  - 差异来自 `_procedure_pattern()`（`orchestration/phase5_governance.py:167`）的 canonical 载荷：它除 `classification` 与 `recommended_group` 外，还纳入 `problem_recommendation` / `change_recommendation` 的**整段正文**。而这两段正文本次是**极性相反**的：
    - 运行 A：`No problem record is proposed: the cited evidence does not establish recurrence of this fault for this tenant.`
    - 运行 B：`Consider opening a problem record: the task states this is the third same-type ticket this month, and the graph correlates ticket 26 with sibling incident ticket 25 on the same CI (Globex VPN gateway)…`
  - **所以这不是「措辞随机漂移」，而是结构性的**：第二张工单**按构造**能看到的证据（它是同月的第三张同类工单、图上与第一张相关联）比第一张多，分析据此把建议从「不提议」改成「提议」是**正确行为**。要求两次建议正文逐字相等，等于要求模型在证据不同时给出相同结论。
  - 附带地，canonical 载荷与它自己的 docstring 也不一致：`_procedure_pattern` 的注释声明「工单号与自由推理被排除，以免事件专属标识符变成可复用指令」，但建议正文里**逐字含有工单号**（前者写 `ticket 26`，后者写 `Ticket 25`），即被排除的东西从正文里漏了回来。

  **阻塞二（独立于阻塞一）：被隔离的 episode 永远不可能成为佐证。**
  - 两条 episodic 记忆本次 `status=quarantine`、`confidence=0.85`；`MemoryGovernancePolicy` 的 `auto_activation_confidence` 默认 **0.90**（`memory/policy.py:20,66`：`confidence < threshold` → quarantine），故它们不会自动转正。
  - 而佐证查询 `pattern_episodes` 经 `allows_record` → `visible_at`，**只对 ACTIVE 为真**。于是这两个 episode 在「作为佐证」这一侧是**不可见**的。
  - 生产侧的门禁（`phase5_governance.py:786-796`）与查询侧**规则一致**，故单独放宽任一侧都不改变结果——这一点已由 `tests/servicemind/test_phase5_governance.py:2819 test_a_quarantined_episode_cannot_corroborate_a_pattern` 锁定并验证。
  - **这构成一个自锁**：新记忆默认进隔离舱等人工复核，而「形成模式」要求至少两条**已激活**的同类 episode。人工复核的是**单条记忆**，不是模式。因此对**第一次出现的**模式，跨工单提案在构造上不可能触发。
- **它是什么、不是什么**：这是**平台的设计级缺陷**，不是验收套件缺陷，也不是「模型不行」。**但它不构成本次需要改代码的理由**——案例文本本身已记明该耦合自洽且当时的结论是「不改代码，仅作为已知未关闭缺陷记录并附复现路径」；而修它意味着**重新定义模式身份**（把随证据变的建议正文从身份里拿掉、并同步解决隔离舱与佐证的自锁）**并改写已冻结的 ACC-12a 断言**，属于设计裁定范围。**本文件不擅自实施该改动。**
- **影响**：`memory` 模块的跨工单程序记忆能力**未被验收**。ACC-12a 只证明了确定性分组机制本身正确；「真实模型下这条链路能否跑通」记 **BLOCKED，阻断验收关闭**。
- **复现路径**：重跑 ACC-12b，然后
  ```bash
  docker exec -i servicemind-glpi-servicemind-postgres-1 bash -lc \
    'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -A -c \
     "select source_run_id, memory_type, status, confidence, provenance->>'"'"'procedure_pattern_key'"'"' \
      from memory_records where source_run_id in ('"'"'910a9278-e526-4844-a61c-eda3bf1ae670'"'"','"'"'489aed01-a25d-44bc-8621-e9771e1303ea'"'"');"'
  ```
  两条记录均为 `episodic / quarantine / 0.85`，且 `procedure_pattern_key` 不同。

### 其他已知未关闭缺陷

| # | 缺陷 | 影响 | 复现路径 |
| --- | --- | --- | --- |
| **D3** | **分析师与评审器的证据集不一致**：同一 run 里评审器看到的证据多于分析师。**v3.0 实测（ACC-03，run `d13f7949-…`）：分析选中 19 条 / 裁剪 3 条，评审选中 22 条 / 裁剪 0 条** | 分析代理被按它从未见过的证据判定。v1.0 中这是 ACC-09a/10a/10b 全 BLOCKED 的直接机制；v3.0 这些案例已 PASS，但**该不一致本身仍然存在** | 对比同一 run 的 `context_artifacts` 两个 `agent_role` 行的 selected/pruned 计数 |
| **D4** | **修订崩溃/限流时保留已知违规草稿**：`_revise_node` 捕获异常后保留被标为违规的草稿，评审器随即确定性拒绝（`UNKNOWN_EVIDENCE_REFERENCE`） | 运行以 `cancelled` 结束，正确回答无人能采信。**v2.0 与 v3.0 的 28 个案例均未再观测到**，故仍记为未关闭但未被复现 | 重跑 ACC-03 并查其 `model_invocations` 与评审 feedback；或制造限流 |
| **D5** | **`MODEL_RATE_LIMITED` 触发确定性降级回退**：分析走降级路径，评审器 `ESCALATE + DEGRADED_ANALYSIS` | 同一案例在不同时刻结论不同（ACC-05 曾因此由 PASS 变 `waiting_review`）。**修复 7 只改重试的**时机**，不改「重试仍失败后如何降级」**——因此它**不是 D5 的修复**。实测支持：`12:07:11Z` 之后（即本批次所在的进程）模型调用 **50 条全部 succeeded、0 条 failed**；而 `12:07:11Z` 之前 10 小时内还有 5 条 `MODEL_RATE_LIMITED`（分析）与 7 条 `MODEL_SCHEMA_INVALID`（数据）。**这只能说明窗口内没有再次限流，不能证明重试在限流时一定成功** | 查 `model_invocations` 中 `status='MODEL_RATE_LIMITED'` 的行；对照同案例两次回放 |
| **D6** | **`result.evidence` 有两种形状**：走 supervisor 的运行持久化 `{items: [...], tenant_id: ...}` 信封，`fast_data`/`fast_knowledge` 持久化裸列表 | 平台契约不一致；只读一种形状的读者会对每个完整运行「看不到证据」 | 分别以数据快路径与完整工作流跑一次，比对 `result.evidence` 的类型 |
| **D7** | **验收套件不可重复**：写案例会自增工单 followup，污染后续只读案例的基线 | 案例之间存在顺序耦合，同一批次重复执行会改变前序案例的观测（本次以「事前记录 followup 基线、只断言增量」缓解，未根治） | 连续执行同一批案例两次，比对 `followups_before` 计数 |
| **D8** | **`tool_outbox` 的 `action.approved` 无消费者**（`RedisStreamConsumer` 全仓零引用） | **不得宣称异步消费与恢复已通过**。核心闭环走同步续跑，不依赖它 | 提交审批后检查 `tool_outbox` 中 `action.approved` 行的 `status` 是否推进；ACC-16 的探针只证明「计数被取到」，不证明队列被消费 |
| **D9** | **GLPI 写入未经 ToolGateway 统一策略与调用审计**（`glpi.append_ticket_followup` 不在工具注册表） | 记录为**没有经过 Gateway 统一策略与调用审计**，不是「无审计」——执行器自身写 `glpi.followup.created` 并检查持久化审批、摘要、租户与幂等 | 检查 `tool_policy_decisions` / 受治理工具调用记录中无该工具 |
| **D10** | **`memory_events` 缺 `run_id`/`trace_id`** | 记忆→运行可经 `memory_records.source_run_id` 验证；**撤销事件→运行无法闭合，故该项审计闭环不通过** | 触发一次记忆撤销，检查 `memory_events` 无运行关联字段 |
| **D11** | **执行器回读只验标记存在**（`harness/executor.py:96`：`verified = marker in html_to_text(followup.content)`），不比对正文 | 正文被截断/篡改仍判 `SUCCEEDED`。**升级条件「ACC-11 正文断言失败而 `verified=true`」在 v2.0 与 v3.0 均未触发**：ACC-11 的 `acc11-body-round-trip` PASS。但**做这次比对的是驱动程序，不是平台**；平台的 `verified` 仍不覆盖正文 | 令 GLPI 侧截断正文后重试写入，观察是否仍判成功 |
| **D12** | **`set_document_active`（知识版本废止/恢复）无任何 HTTP 入口** | 知识版本废止无自助能力，只能由脚本进程内调用 | 检查 OpenAPI 无对应路由 |
| **D13** | **`scripts/verify_phase2_concurrency.py` 已失效**（硬编码 `:8080`、断言 `create_run` 返回体为 `waiting_approval`，实际必为 `pending`） | 不可作为证据引用；仅其 Keycloak token 辅助部分可复用 | 直接运行该脚本 |

### 本阶段已修复并锁定（不再是缺陷）

| 曾有的缺陷 | 修复 | 锁定测试 |
| --- | --- | --- |
| **D1.（发布阻断，v3.0 关闭）信封把被按来源上限拒绝的行永久丢掉**——该上限按 rank 顺序扣费，排位靠后的知识手册即使更相关也回不来；实测信封只用 6412/10720 却报告「被上限拒绝」 | 修复 A：第二趟按广度优先序回收 `cap_deferred` 行 | `test_phase5_governance.py::test_a_capped_bulk_row_does_not_take_the_room_it_was_refused_from_memory`、`::test_a_capped_row_gives_way_sooner_than_the_channels_ranking_below_it`、`test_capping_the_bulk_channel_is_what_delivers_a_realistic_procedure`。**实测关闭**：ACC-03 所依赖的正确手册以 `reclaimed_from_source_cap` 进入分析信封，该案例由 FAIL 转 PASS |
| **D14.（发布阻断，v3.0 关闭）ACC-03 的 `question` 与它自己的工单夹具描述的不是同一件事** | **用户裁定**：改 `cases.v1.json` 中的 `question`，使之与工单夹具的故障点一致 | ACC-03 的 6 条断言全部 PASS（`acc03-root-cause-not-the-decoy`、`acc03-similar-doc-retrievable`、`acc03-password-accepted`、`acc03-mfa-failed`、`acc03-device-changed`、`acc03-terminal-succeeded`） |
| **错误文本头端裁剪使失败不可诊断**（ACC-07 的实测成因） | 修复 6：`bounded_error_text` 保留两端 | `test_foundation_errors.py` 3 条 + 源码级契约断言；变异验证（换回 `str(exc)[:1000]` 即变红） |
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

- **P7.6.5 安全与故障场景**（≥40 条）
- **P7.6.6 业务质量集**（200 条 = 120 可答 + 40 证据不足 + 20 版本冲突 + 20 必须拒绝访问）；端到端 Reviewer 可答率在同一批次**用真正跑完的结果重新测量**，以取代 `0.275` 检索分数阈值代理口径
- **P7.6.7 负载**（1/5/10 并发探索后冻结门槛，声明为测试档位而非已认证容量）与**长稳/故障注入**（需独立环境）
- **P7.0–P7.5 本身**（规划中，未实现）
- **ACC-18…ACC-23 之外的撤权场景**：本轮补齐的六类是**已执行的**；仍未执行的包括长时间（跨 checkpoint 重启）撤权、撤权与限流叠加、撤权期间核验器中途不可达
- **D15 修复之后的跨工单程序记忆端到端**：本轮只做到「确认它为什么不通」，**没有做任何修复**，因此「修好之后能否跑通」完全未评估
- **未触发的升级条件**：D11 的升为必修条件本次未触发，故 D11 仍按未升级处理——这**不等于**它已被验证为安全

## 历史口径修正

`0.275` 在既有文档中已被标注为**检索 top-score 阈值代理**，不是 Reviewer 端到端可答率（见 `evaluation/reports/rag_quality_status_latest.{json,md}`，其 `answerability_signal.is_end_to_end_reviewer_measurement` 为 `false`）。该口径修正**已生效**；端到端的真实测量属于 P7.6.6，在此之前维持 `NOT_EVALUATED`，不得以代理值代替。

`tests/servicemind/test_phase4_index_lifecycle.py` 的模块 docstring 曾声称存在「docker-gated suite covers a real OpenSearch」，但该文件内 `mark.docker` 计数为 0，全部测试走内存假客户端。该 docstring 已在本阶段纠正，真集群覆盖由新增的独立套件 `tests/servicemind/test_phase4_index_lifecycle_live.py` 提供（ACC-17 探针即为其实测）。

**v3.0 推翻的两条推演（均为我此前写下的，现予撤回，凡引用者不得再引用）**：

1. **「把重复的 `output-schema` 项从分析信封里拿掉，就能让被裁的知识手册进来。」** 该推演建立在「总预算不足」这一误判上。实测：已选证据 4462 + 被裁最小行 713 = 5175 > `SERVICEMIND_CONTEXT_EVIDENCE_TOKEN_CAP=5000`，**腾出多少总预算都进不来**。真正让手册进来的是**修复 A 的回收趟**，不是修复 C。
2. **「通道的 cap 是一份到期作废的配额，被拒份额应保留给同通道的后继行。」** A/B 实验（`git show HEAD:src/servicemind/context/builder.py` 对照）证明：该规则在 HEAD 上通过的 `test_capping_the_bulk_channel_is_what_delivers_a_realistic_procedure` 上失败，且它**没有换来它被写出来要换的东西**——ACC-03 在预留存在时仍然失败。已移除并锁定为「被拒份额**归还给信封**（回收趟），而不是预留给同一通道」。

**v3.0 改写的第三条口径（不是推翻，是补正）**：

- v2.0／ACC-12b 案例文本把该案例的成因写成「身份取自模型自由文本，两次独立采样归一化后逐字相等，真实采样下产出率约为零」。**本次逐字对照后必须改写为**：`classification` 与 `recommended_group` 本次**完全一致**；不一致的是 `problem_recommendation` 的**极性**，而极性不同是因为两张工单**按构造看到了不同的证据**。这是一条比「措辞不可复现」更强也更准确的结论——它说明该字段**不可能**通过任何归一化手段变得稳定。详见 D15。

**v3.0 仍然成立的三条（v1.0/v2.0 记录，本次重新证实）**：

1. 「检索到正确手册即等于分析用上了它」——判定必须看 claim 的 `evidence_refs` 与 `selection_manifest`，不能看 citation 列表。（v3.0 起 ACC-03 的 `evidence_refs` 确实指向了正确手册，故该案例转绿；这条口径本身仍然有效。）
2. 「案例通过即链路健康」——BLOCKED 与 FAIL 必须分开记录；27 条 PASS 中，`frontend`/`mcp`/`outbox`/`index-lifecycle` 四行只由探针记分，且各只有 1 条断言。
3. 「离线重放通过即当前代码通过」——见「术语纪律」第 2 条。

## 边界与下一阶段

- 本阶段结论限于「核心业务闭环验收」，**结论为未通过**；不声明生产容量已认证，不替代 Phase 8 的并发、长稳与灾备演练。
- **关闭本阶段的前置（v3.0 更新）**：
  1. **裁定并（若裁定为改）实施 D15**——跨工单程序记忆的模式身份需要与「随证据而变的建议正文」解耦，并解决隔离舱与佐证之间的自锁。这是一次**设计裁定**：它会改变已冻结的 ACC-12a 断言，故不由本轮擅自实施。
  2. **修 D3**——分析代理与评审器的证据集不一致（v3.0 实测 ACC-03 为 19 vs 22）。
  3. **修 D5** 的降级语义——修复 7 只改重试时机，`MODEL_RATE_LIMITED` 触发降级回退后的行为未变。
  4. **D8 / D11 的架构要求**（outbox 消费者、执行器正文回读）——两者都不是本轮核心闭环的依赖，但都是发布前应关闭的欠账。
- 其他缺口分批修复；**每次修复后更新被测版本并重跑受影响案例**。缺陷修复后，其原复现案例必须复绿。
- 「避免改变基线」不构成保留在用安全缺口的理由。

## 签署

本基线 v3.0 在 2026-09-22 冻结判定规则与缺陷口径；执行后的裁定以机器生成的验收证据为准，实际结论为**未通过**（PASS 27 / FAIL 0 / BLOCKED 1，gate 退出码 2，105 条断言 0 FAIL）。剩余阻断为 **D15（ACC-12b）**，且它是一条**已知、成因已实测、复现路径明确**的阻断，不是数据不足。

v2.0 的两条发布阻断 **D1 与 D14 均已关闭**：D14 由用户裁定改题面，D1 由修复 A（超预算行的回收趟）消除其可观测后果——ACC-03 的 6 条断言本次全部 PASS，且其转绿机制可在回放中逐项复查。

本结论基于固定版本、真实身份服务、真实 GLPI、真实 OpenSearch 与真实模型的当前部署，不构成对未来供应商故障、未知攻击或未执行负载形态的绝对无缺陷保证。凡本文件未列出观测的模块，均按「未评估」处理。

机器可读证据：`evaluation/reports/phase7_acceptance_latest.json`（判定与覆盖）；
`evaluation/acceptance/replays/*.json`（逐案例观测）；
`evaluation/reports/phase7_pipeline_evidence.json`（前置条件与回归的实际退出码）。
