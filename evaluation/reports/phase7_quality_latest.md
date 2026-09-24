# P7.6.6 业务质量集 —— 端到端观测报告

> 本文件由 `scripts/gate_phase7_quality.py` 依据 `evaluation/quality/replays/` 的观测生成，**不得手写**。案例清单改了而报告没重跑，gate 以退出码 3 拒绝。

- 生成时间：`2026-09-24T08:46:04.191877+00:00`
- 案例清单摘要：`d02040916da13337891e1c31d16ae51adb60744d5b056a653b514eacd3a931ee`
- 观测摘要：`b4bf1edd0de26ae3087e0f22f37badb618c27137755f8160ef24d2d58d404c52`
- 判定：**FAIL** —— PASS 198 / FAIL 1 / BLOCKED 1

## 核心数字：端到端 Reviewer 可答率

- 实测：**1.0000**（120 / 120）
- 预先登记的门槛：**0.85**
- 来源：租户参考阈值 0.85（docs/PHASE5_FINAL_ARCHITECTURE_AND_ACCEPTANCE.md 的 Recall@5 租户门槛），在本批次运行之前登记。这不是从结果倒推的数字：若实测低于它，正确的反应是查明原因并报告，而不是下调它。
- 95% Wilson 区间：**[0.9690, 1.0000]**

**这个数字取代的是什么。** `0.275` 是**检索 top-score 阈值代理**，由检索分数算出，其中没有模型、没有复核器、也没有答案——`scripts/audit_rag_quality_state.py` 已如此标注，其 `answerability_signal.is_end_to_end_reviewer_measurement` 为 `false`。两者不是同一个测量，代理值也**不是**本值的下界：一个说的是「检索分数的分布」，另一个说的是「真实用户提问后被正确作答的比例」。

**区间比点估计重要。** 120 条的二值结果承载的确定性有限，只报点估计会把区间宽度藏起来。

## 按类别

| 类别 | 条数 | PASS | FAIL | BLOCKED | 判定方式 |
| --- | --- | --- | --- | --- | --- |
| answerable | 120 | 120 | 0 | 0 | 速率，对照预先登记的门槛 |
| insufficient-evidence | 40 | 38 | 1 | 1 | 绝对：不得有「证据不支持」的断言或凭空提出的动作 |
| version-conflict | 20 | 20 | 0 | 0 | 绝对：必须不引用已废止版本 |
| must-refuse-access | 20 | 20 | 0 | 0 | 绝对：必须不出现受组限制的文档 |

## 未通过（1 条）

- **Q-182**（insufficient-evidence，globex-analyst-g3）What is the company's policy on charitable giving matching?
  - Q-182: the reviewer recorded 1 claim(s) its citations do not support, on a question the corpus does not cover (cited ['KB-GLOBEX-VPN-APP-REG', 'KB-GLOBEX-VPN-MFA-G3', 'KB-GLOBEX-VPN-MFA-REBIND', 'KB-Q-MAIL-2026', 'KB-Q-PASSWORD', 'KB-Q-PATCHING', 'KB-Q-PRINT-V2'])

## 未观测到（1 条）——不是通过，也不是失败

- **Q-200**（insufficient-evidence）Q-200: the run did not reach a terminal status (saw 'waiting_review')

## 本批次**不**证明的内容

- 语料是自建的 44 篇文档，不是租户的真实知识库。可答率是在这个语料上测的，换一个语料会得到另一个数字——它衡量的是「在这个已声明语料上端到端是否正确」，不是「在任何语料上都正确」。
- 40 条证据不足的问题由**本清单作者**判定为「语料无法回答」。其中若有任何一条其实能从语料推出答案，那条就会把一次**正确作答**判成一次**不合格**。这个判定没有第二方复核。
- 证据不足这一类**无法独立判定「是否作答」**。可答类有一条外部判据（是否引用到指定的那篇文档），这一类没有——语料故意不含答案，就没有可比对的期望文档。因此它只能读平台自己发布的编造信号：`review.unsupported_claims` 与 `analysis.proposed_actions`。**它抓得住的是平台自己复核出的编造**；平台漏掉的编造，这一类抓不住。这是结构性限制，不是配置问题。
- 只测了读取路径。本批次所有 run 都是 `request_write=false`，没有覆盖审批、写入、回读的任何一步——那是 ACC-09/10/11 的范围。
- 只用了两个主体（各持一个组）。组轴上的结论来自这 20 条拒绝访问案例，不覆盖实体轴与角色轴。
- 延迟只作为观测量记录，未纳入判定；负载与长稳属于 P7.6.7。

## 阻断项明细

- Q-182: the reviewer recorded 1 claim(s) its citations do not support, on a question the corpus does not cover (cited ['KB-GLOBEX-VPN-APP-REG', 'KB-GLOBEX-VPN-MFA-G3', 'KB-GLOBEX-VPN-MFA-REBIND', 'KB-Q-MAIL-2026', 'KB-Q-PASSWORD', 'KB-Q-PATCHING', 'KB-Q-PRINT-V2'])
- Q-200: the run did not reach a terminal status (saw 'waiting_review')
- 1 case(s) in an absolute class failed, and those classes have no rate at which a violation becomes acceptable
