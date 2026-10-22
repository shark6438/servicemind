# P7.6.5 安全与故障场景集 —— 观测报告

> 本文件由 `scripts/gate_phase7_security.py` 依据 `evaluation/security/replays/` 中的观测生成，**不得手写**。修改场景后必须重跑，否则 gate 以退出码 3 拒绝。

- 生成时间：`2026-10-22T11:22:51.488769+00:00`（gate 运行时刻，非观测时刻）
- 被测版本：`19d9a377fb98e1c3f56edc59be40b374013bc08b+patch(9d7e9716f6ac)`
- 观测时间窗：`2026-10-22T11:11:41.064890+00:00` → `2026-10-22T11:11:41.064890+00:00`
- 场景清单摘要：`db3731e1046f66fa21a3e0890d71624d02f3e1f2580ec6d261ae9e9cd2042bea`
- 观测摘要：`23d7c34e6a51ea3d43ea5c63a21d32a0fdcad495c57af3606e17025b5d073d81`
- 判定：**PASS** —— PASS 82 / FAIL 0 / BLOCKED 0

- 有变异背书的场景（`teeth`）：**45 / 82**。其余场景只由通过的测试背书——「测试是绿的」与「删掉这段行为测试会变红」**不是同一个强度的结论**，报告分列，不合并。

## 按类别

| 类别 | 场景数 | PASS | FAIL | BLOCKED |
| --- | --- | --- | --- | --- |
| approval-binding | 6 | 6 | 0 | 0 |
| crash-recovery | 6 | 6 | 0 | 0 |
| credential-handling | 4 | 4 | 0 | 0 |
| data-bounds | 5 | 5 | 0 | 0 |
| entity-and-group-authorization | 11 | 11 | 0 | 0 |
| idempotency | 3 | 3 | 0 | 0 |
| outbox-retention | 4 | 4 | 0 | 0 |
| principal-narrowing | 8 | 8 | 0 | 0 |
| prompt-injection | 4 | 4 | 0 | 0 |
| rate-limit-and-degradation | 7 | 7 | 0 | 0 |
| self-authored-evidence | 3 | 3 | 0 | 0 |
| tenant-isolation | 5 | 5 | 0 | 0 |
| verifier-availability | 6 | 6 | 0 | 0 |
| webhook-authenticity | 3 | 3 | 0 | 0 |
| write-path-authorization | 7 | 7 | 0 | 0 |

## 逐条场景

- **SEC-TENANT-01**（tenant-isolation）本地控制：另一个租户的 checkpoint 不得被续跑 — **PASS**（有变异背书）
- **SEC-TENANT-02**（tenant-isolation）本地控制：组受限文档只对其组可读，无限制文档对租户内所有人可读 — **PASS**（仅由通过的测试背书）
- **SEC-TENANT-03**（tenant-isolation）本地控制：MCP 任务按租户隔离，且终态不可改写 — **PASS**（仅由通过的测试背书）
- **SEC-TENANT-04**（tenant-isolation）本地控制：语义缓存按租户分区 — **PASS**（仅由通过的测试背书）
- **SEC-TENANT-05**（tenant-isolation）本地控制：模型网关的租户白名单取交集，未注册租户被拒 — **PASS**（仅由通过的测试背书）
- **SEC-TENANT-06**（outbox-retention）本地控制：outbox 清理的删除保持在租户范围内，且一个租户的失败不波及其他租户 — **PASS**（有变异背书）
- **SEC-AUTHZ-01**（entity-and-group-authorization）本地控制：起跑必须把调用方的组坐标写进图状态 — **PASS**（有变异背书）
- **SEC-AUTHZ-02**（entity-and-group-authorization）本地控制：缺失的组坐标按「无范围」读，不得读成「不限制」 — **PASS**（有变异背书）
- **SEC-AUTHZ-03**（entity-and-group-authorization）本地控制：范围外的 GLPI 实体不得回退到租户级集成 — **PASS**（有变异背书）
- **SEC-AUTHZ-04**（entity-and-group-authorization）本地控制：绑定实体的工具对该实体之外不可见 — **PASS**（有变异背书）
- **SEC-AUTHZ-05**（entity-and-group-authorization）本地控制：没有任何实体在范围内时写入被拒 — **PASS**（仅由通过的测试背书）
- **SEC-AUTHZ-06**（entity-and-group-authorization）本地控制：身份可携带的 ACL 集合有上限，超限是 401 而不是 500 — **PASS**（仅由通过的测试背书）
- **SEC-AUTHZ-07**（entity-and-group-authorization）本地控制：GraphRAG 图遍历必须过滤整条路径，而不只是过滤种子 — **PASS**（有变异背书）
- **SEC-AUTHZ-08**（entity-and-group-authorization）本地控制：无法执行节点 ACL 的图存储必须被拒绝，而不是降级为空 — **PASS**（有变异背书）
- **SEC-AUTHZ-09**（entity-and-group-authorization）本地控制：图读取拿到的是完整主体，而不是只有租户 — **PASS**（有变异背书）
- **SEC-NARROW-01**（principal-narrowing）本地控制：审批不得把审批员的权限借给运行 — **PASS**（有变异背书）
- **SEC-NARROW-02**（principal-narrowing）本地控制：没有 checkpoint 的运行不得以调用者身份续跑 — **PASS**（有变异背书）
- **SEC-NARROW-03**（principal-narrowing）本地控制：有效主体必须是「记录 ∩ 当前」，且只能收窄 — **PASS**（有变异背书）
- **SEC-NARROW-04**（principal-narrowing）本地控制：崩溃恢复路径同样强制交集，并如实记录主体来源 — **PASS**（有变异背书）
- **SEC-NARROW-05**（principal-narrowing）本地控制：无法确认授权时暂停，而不是收窄后继续 — **PASS**（有变异背书）
- **SEC-NARROW-06**（principal-narrowing）本地控制：收窄必须让在更宽范围下取得的证据失效 — **PASS**（有变异背书）
- **SEC-NARROW-07**（principal-narrowing）本地控制：收窄必须作废待执行动作，并作废到数据行上 — **PASS**（有变异背书）
- **SEC-NARROW-08**（principal-narrowing）本地控制：收窄后该步骤所需角色不再具备时暂停 — **PASS**（仅由通过的测试背书）
- **SEC-APPROVAL-01**（approval-binding）本地控制：暂停不消耗决定、不翻状态、不写任何东西 — **PASS**（有变异背书）
- **SEC-APPROVAL-02**（approval-binding）本地控制：被作废的动作必须重新派生，而不是被既有决定裁决 — **PASS**（有变异背书）
- **SEC-APPROVAL-04**（approval-binding）本地控制：终态不是常备授权：引用未获批意图的调用被拒 — **PASS**（有变异背书）
- **SEC-APPROVAL-05**（approval-binding）本地控制：拒绝不是执行：它能扛过身份服务停摆，也不能被借用去批准 — **PASS**（有变异背书）
- **SEC-APPROVAL-06**（approval-binding）本地控制：发起人必须持有该步骤的角色，才谈得上批准它 — **PASS**（有变异背书）
- **SEC-WRITE-01**（write-path-authorization）本地控制：execute_node 在写入前重新校验，而不是只信审批时的结论 — **PASS**（有变异背书）
- **SEC-WRITE-02**（write-path-authorization）本地控制：写前校验绕过缓存，取当次的授权 — **PASS**（有变异背书）
- **SEC-WRITE-03**（write-path-authorization）本地控制：网关不写审批未覆盖的正文，也不信调用方自选的标记 — **PASS**（有变异背书）
- **SEC-WRITE-04**（write-path-authorization）本地控制：回读要重新问 GLPI，并逐字比对正文 — **PASS**（有变异背书）
- **SEC-WRITE-05**（write-path-authorization）本地控制：原始 GLPI 写入只存在于一个模块，且必须经过网关 — **PASS**（有变异背书）
- **SEC-WRITE-06**（write-path-authorization）本地控制：失败的调用不被留成「未尝试」，崩溃恢复出的重复被记为重复 — **PASS**（有变异背书）
- **SEC-LIMIT-01**（rate-limit-and-degradation）本地控制：只有限流才等待，别的原因直接失败 — **PASS**（有变异背书）
- **SEC-LIMIT-02**（rate-limit-and-degradation）本地控制：Retry-After: 0 是「立刻重试」，不是「这不是限流」 — **PASS**（有变异背书）
- **SEC-LIMIT-03**（rate-limit-and-degradation）本地控制：提供方的提示优先，调度有上限，总等待有上限 — **PASS**（有变异背书）
- **SEC-LIMIT-04**（rate-limit-and-degradation）本地控制：等待尊重运行截止时间，并给后续阶段留余量 — **PASS**（有变异背书）
- **SEC-LIMIT-05**（rate-limit-and-degradation）本地控制：超过等待的限流按「限流」降级，且重试次数有界 — **PASS**（有变异背书）
- **SEC-LIMIT-06**（rate-limit-and-degradation）本地控制：预算为 0 的降级是显式的，且不发明缺失的能力 — **PASS**（仅由通过的测试背书）
- **SEC-INJECT-01**（prompt-injection）本地控制：每个规范注入标记在写入时被隔离，且词表不可悄悄缩小 — **PASS**（仅由通过的测试背书）
- **SEC-INJECT-02**（prompt-injection）本地控制：已存的注入标记在读取侧仍被拦截 — **PASS**（仅由通过的测试背书）
- **SEC-INJECT-03**（prompt-injection）本地控制：注入行被标记、不得到达模型，并从语料中撤下 — **PASS**（仅由通过的测试背书）
- **SEC-INJECT-04**（prompt-injection）本地控制：模型读取的边界声明它来自哪条通道，且记忆带着作者身份 — **PASS**（仅由通过的测试背书）
- **SEC-BOUNDS-01**（data-bounds）本地控制：超大载荷被截断而不是崩溃，且截断被标记 — **PASS**（仅由通过的测试背书）
- **SEC-BOUNDS-02**（data-bounds）本地控制：行数上限成立，且平台自产的 followup 不计入证据 — **PASS**（仅由通过的测试背书）
- **SEC-BOUNDS-03**（data-bounds）本地控制：超长身份是 401 而不是 500 — **PASS**（仅由通过的测试背书）
- **SEC-BOUNDS-04**（data-bounds）本地控制：有界错误保留说明成因的那一端 — **PASS**（仅由通过的测试背书）
- **SEC-SELF-01**（self-authored-evidence）本地控制：平台自产的 followup 不得成为后续运行的证据 — **PASS**（仅由通过的测试背书）
- **SEC-SELF-02**（self-authored-evidence）本地控制：评审器按分析实际看到的证据集判定 — **PASS**（仅由通过的测试背书）
- **SEC-SELF-03**（self-authored-evidence）本地控制：检索查询不得回显进模型可见的证据 — **PASS**（仅由通过的测试背书）
- **SEC-IDEM-01**（idempotency）本地控制：同一个幂等键配不同参数必须被拒绝 — **PASS**（仅由通过的测试背书）
- **SEC-IDEM-02**（idempotency）本地控制：一次运行只撤下一篇文档，干净的运行什么都不撤 — **PASS**（仅由通过的测试背书）
- **SEC-IDEM-03**（idempotency）本地控制：TTL 之内的重试解析到同一个任务 — **PASS**（仅由通过的测试背书）
- **SEC-OUTBOX-01**（outbox-retention）本地控制：发布到出箱流时有界，且载荷只带引用不带正文 — **PASS**（有变异背书）
- **SEC-OUTBOX-02**（outbox-retention）本地控制：清理只退休「已投递且已过龄」的行 — **PASS**（有变异背书）
- **SEC-OUTBOX-03**（outbox-retention）本地控制：清理分页、有上限、有节奏，且可被取消 — **PASS**（有变异背书）
- **SEC-CRASH-01**（crash-recovery）本地控制：暂停的运行既不被恢复也不被标记失败 — **PASS**（有变异背书）
- **SEC-CRASH-02**（crash-recovery）本地控制：崩溃的修订不得发布它正在修复的草稿 — **PASS**（有变异背书）
- **SEC-CRASH-03**（crash-recovery）本地控制：装配不了的上下文、被 schema 拒绝的决定、双重失败都走向 finalize — **PASS**（仅由通过的测试背书）
- **SEC-CRASH-04**（crash-recovery）本地控制：每个终态路径发布同一种证据形状 — **PASS**（仅由通过的测试背书）
- **SEC-CRASH-05**（crash-recovery）本地控制：监督者的历次拒绝在重试后仍然有效 — **PASS**（有变异背书）
- **SEC-VERIFIER-01**（verifier-availability）本地控制：默认不安装任何核验器 — **PASS**（有变异背书）
- **SEC-VERIFIER-02**（verifier-availability）本地控制：不可达的身份服务是「不可用」，不是「空授权集」 — **PASS**（有变异背书）
- **SEC-VERIFIER-03**（verifier-availability）本地控制：realm 读取把每个 HTTP 回答映射到它自己的结果 — **PASS**（有变异背书）
- **SEC-VERIFIER-04**（verifier-availability）本地控制：决定所用的授权时间窗被记录，新回答不记 — **PASS**（有变异背书）
- **SEC-WEBHOOK-01**（webhook-authenticity）本地控制：签名绑定正文、时间戳与密钥 — **PASS**（仅由通过的测试背书）
- **SEC-WEBHOOK-02**（webhook-authenticity）本地控制：畸形签名头是干净的拒绝，不是 500 — **PASS**（仅由通过的测试背书）
- **SEC-WEBHOOK-03**（webhook-authenticity）本地控制：过期投递与超长事件在进入运行之前被拒 — **PASS**（仅由通过的测试背书）
- **SEC-CRED-01**（credential-handling）本地控制：跨租户的调用在解析凭证之前被拒；动作代理不持有凭证 — **PASS**（仅由通过的测试背书）
- **SEC-CRED-02**（credential-handling）本地控制：密钥类内容被网关与记忆硬门禁拦下，不落进证据 — **PASS**（仅由通过的测试背书）
- **LOCAL-RUN-SCOPE**（entity-and-group-authorization）Run 主体、撤权和真实 RLS/分页边界 — **PASS**（有变异背书）
- **LOCAL-RUN-ROLE**（entity-and-group-authorization）详情、事件、时间线统一角色门禁 — **PASS**（仅由通过的测试背书）
- **LOCAL-GENERIC**（write-path-authorization）通用 API 默认关闭 — **PASS**（仅由通过的测试背书）
- **LOCAL-WEBHOOK-BOUNDS**（data-bounds）Webhook 畸形头与数据范围拒绝 — **PASS**（有变异背书）
- **LOCAL-WEBHOOK-RATE**（rate-limit-and-degradation）Webhook 速率/并发有界且释放资源 — **PASS**（仅由通过的测试背书）
- **LOCAL-JWT**（credential-handling）真实 RSA 签名 JWT 的 claim/算法边界 — **PASS**（仅由通过的测试背书）
- **LOCAL-CIPHER**（credential-handling）密文损坏及错误轮换密钥失败关闭 — **PASS**（仅由通过的测试背书）
- **LOCAL-CANCEL**（crash-recovery）真实数据库取消写入屏障及防复活 — **PASS**（有变异背书）
- **LOCAL-GRADING**（verifier-availability）部分执行与变异基础设施失败不伪造通过 — **PASS**（仅由通过的测试背书）
- **LOCAL-PROVENANCE**（verifier-availability）历史验收不能重标为当前版本 — **PASS**（仅由通过的测试背书）
- **LOCAL-MEMORY-REVIEW**（approval-binding）人工记忆审核角色、组、快照与并发绑定 — **PASS**（仅由通过的测试背书）

## 无变异背书的场景（不是缺陷，是覆盖强度的说明）

- SEC-TENANT-02 — 本地控制：组受限文档只对其组可读，无限制文档对租户内所有人可读
- SEC-TENANT-03 — 本地控制：MCP 任务按租户隔离，且终态不可改写
- SEC-TENANT-04 — 本地控制：语义缓存按租户分区
- SEC-TENANT-05 — 本地控制：模型网关的租户白名单取交集，未注册租户被拒
- SEC-AUTHZ-05 — 本地控制：没有任何实体在范围内时写入被拒
- SEC-AUTHZ-06 — 本地控制：身份可携带的 ACL 集合有上限，超限是 401 而不是 500
- SEC-NARROW-08 — 本地控制：收窄后该步骤所需角色不再具备时暂停
- SEC-LIMIT-06 — 本地控制：预算为 0 的降级是显式的，且不发明缺失的能力
- SEC-INJECT-01 — 本地控制：每个规范注入标记在写入时被隔离，且词表不可悄悄缩小
- SEC-INJECT-02 — 本地控制：已存的注入标记在读取侧仍被拦截
- SEC-INJECT-03 — 本地控制：注入行被标记、不得到达模型，并从语料中撤下
- SEC-INJECT-04 — 本地控制：模型读取的边界声明它来自哪条通道，且记忆带着作者身份
- SEC-BOUNDS-01 — 本地控制：超大载荷被截断而不是崩溃，且截断被标记
- SEC-BOUNDS-02 — 本地控制：行数上限成立，且平台自产的 followup 不计入证据
- SEC-BOUNDS-03 — 本地控制：超长身份是 401 而不是 500
- SEC-BOUNDS-04 — 本地控制：有界错误保留说明成因的那一端
- SEC-SELF-01 — 本地控制：平台自产的 followup 不得成为后续运行的证据
- SEC-SELF-02 — 本地控制：评审器按分析实际看到的证据集判定
- SEC-SELF-03 — 本地控制：检索查询不得回显进模型可见的证据
- SEC-IDEM-01 — 本地控制：同一个幂等键配不同参数必须被拒绝
- SEC-IDEM-02 — 本地控制：一次运行只撤下一篇文档，干净的运行什么都不撤
- SEC-IDEM-03 — 本地控制：TTL 之内的重试解析到同一个任务
- SEC-CRASH-03 — 本地控制：装配不了的上下文、被 schema 拒绝的决定、双重失败都走向 finalize
- SEC-CRASH-04 — 本地控制：每个终态路径发布同一种证据形状
- SEC-WEBHOOK-01 — 本地控制：签名绑定正文、时间戳与密钥
- SEC-WEBHOOK-02 — 本地控制：畸形签名头是干净的拒绝，不是 500
- SEC-WEBHOOK-03 — 本地控制：过期投递与超长事件在进入运行之前被拒
- SEC-CRED-01 — 本地控制：跨租户的调用在解析凭证之前被拒；动作代理不持有凭证
- SEC-CRED-02 — 本地控制：密钥类内容被网关与记忆硬门禁拦下，不落进证据
- LOCAL-RUN-ROLE — 详情、事件、时间线统一角色门禁
- LOCAL-GENERIC — 通用 API 默认关闭
- LOCAL-WEBHOOK-RATE — Webhook 速率/并发有界且释放资源
- LOCAL-JWT — 真实 RSA 签名 JWT 的 claim/算法边界
- LOCAL-CIPHER — 密文损坏及错误轮换密钥失败关闭
- LOCAL-GRADING — 部分执行与变异基础设施失败不伪造通过
- LOCAL-PROVENANCE — 历史验收不能重标为当前版本
- LOCAL-MEMORY-REVIEW — 人工记忆审核角色、组、快照与并发绑定

## 本场景集**不**覆盖的内容（手写，非机器生成）

下面是本表**没有**背书的威胁。它按清单作者的判断写就，因此它可能不完整——把「表有多长」读成「威胁面有多大」是这张表最容易造成的误读，这一节就是用来挡住它的。

- 对抗性投喂的注入词表：本集只验证「已列入词表的标记」被处理，没有任何一条场景向平台投喂过词表之外的新注入变体。
- 长期或跨 checkpoint 重启的撤权：六类撤权（撤组/撤实体/撤角色/禁用用户/查询故障/写前撤权）已由 ACC-18…ACC-23 实跑覆盖，但「撤权发生在服务重启之后」与「撤权与限流叠加」未测。
- 依赖组件的供应链与镜像漏洞：由 CI 的 trivy 扫描与 SBOM 承担，不在本场景集内。
- 拒绝服务与容量：并发压测、长稳与故障注入属于 P7.6.7，不在本集。
- 审计日志的篡改检测：本集只验证事件被写下及关联到运行，不验证已写下的审计行不可被有权限的操作者改写。
- 不调用真实模型；公开攻击基准、多轮真实模型ASR、生产TLS/网关/镜像扫描、真实GLPI/Keycloak部署验收未执行。
- CredentialCipher 已验证错误密钥失败关闭，但未实施旧密文迁移或密钥轮换流程。
- JWT 使用真实 RSA 签名，JWKS HTTP 返回模拟；不证明现场 Keycloak 配置。
- 历史验收引用在 local-scope.json 中逐项记录为本轮未执行，不能据本地通过宣称这些验收已通过。
- Webhook 限流按单工作进程实施；多副本共享限流和长稳容量尚未验证。
- 仅含现场验收、没有本地证据的场景不纳入本地分母：SEC-APPROVAL-03
