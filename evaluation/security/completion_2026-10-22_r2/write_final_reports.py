"""Render the final report from sealed-candidate observations; preserve failures."""

import json
from pathlib import Path

BATCH = Path(__file__).resolve().parent
ROOT = BATCH.parents[2]


def load(name):
    return json.loads((BATCH / name).read_text())


attacks = load("local-model-attacks.json")
matrix = load("case-matrix.json")
mutations = load("mutation-summary.json")
revision = mutations["source_revision"]
utility = load("local-model-utility.json")
model_calls = attacks["model_requests_observed"]
attack_rows = "\n".join(
    f"| {r['case']} | {r['security_verdict']} | {r['terminal_status']} / {r['utility_verdict']} | {r['seconds']} 秒 |"
    for r in attacks["cases"]
)
current_control = load("final-local-model-control-summary.json")
normal_rows = "、".join(
    r.get("validity", "VALID") + "/" + r["verdict"] for r in utility["normal_control_attempts"]
)
report = f"""# ServiceMind 安全审计、整改与免费测评最终报告

日期：2026-10-22，Asia/Shanghai。依据：[security.md](security.md) 与 [实施方案](SECURITY_ASSESSMENT_EXECUTION_PLAN_2026-10-22.md)。用户最终授权完成整改和复测，使用免费方式替代收费模型 API。

## 结论

**本轮安全控制测评完成并通过：原始 72 场景全部 PASS，整改控制清单 82/82 PASS；1484 项隔离回归和 4 项部署测试全部通过；107/107 项变异被检测。** 初期完整门禁的 9 个 BLOCKED 已由本轮现场安全验收补齐，历史证据保持原样。

48 条专项规格按免费控制测评方式逐项复判，结果 48 PASS / 0 FAIL / 0 BLOCKED，关联 {matrix["unique_test_selectors"]} 个唯一测试选择器、参数化后 {matrix["executed_parameterized_tests"]} 项，以及真实身份、GLPI、网络、浏览器和存储证据。**48 是控制规格覆盖数，不是 48 次真实模型端到端攻击，不计作真实模型 ASR。** 逐行攻击条件、正常对照、证据模式与节点见 [专项矩阵](../evaluation/security/completion_2026-10-22_r2/case-matrix.md) 和 [机器记录](../evaluation/security/completion_2026-10-22_r2/case-matrix.json)。

安全控制通过不等同于全业务发布通过。完整 28 项业务验收为 **26 PASS / 1 FAIL / 1 BLOCKED**：ACC-03 的模拟模型没有产出根因假设；ACC-12b 未形成可供激活的过程记忆。原始失败保留在 [验收报告](../evaluation/security/completion_2026-10-22_r2/acceptance-report.json)，没有更改断言或把业务失败改判为 PASS。这两项不属于原始 72 安全场景所需的 11 个现场案例；11 个安全现场案例全部通过。

默认依赖与镜像的扫描、前端浏览器结果在最后仅改动取消响应时按精确组件摘要沿用，没有改写采集时间，见 [沿用证据说明](../evaluation/security/completion_2026-10-22_r2/inherited-evidence.json)。

模型 API 付费调用为 **0**。测试曾使用本机 GPU，最终保留 CPU；机器、电力和存储消耗仍存在。本结论限定为已测配置的安全控制；未签署所有模型、所有 provider/OPA 组合、全业务质量或公网生产容量的发布通过。

## 版本与部署

被测源码：`{revision}`。部署 `/health/build` 在启动时固定源码、boot UUID、非敏感配置摘要；不是每次请求按磁盘内容重算。本批正式 72/82 控制场景、变异副本及现场安全验收绑定该源码；早期模型探针保留各自原始版本。声明式故障实验会重启服务，但源码和模型替身固定，重启前后身份记录在 replay。

本轮已经实施部署整改：备份业务 PostgreSQL，迁移至 `0015_run_access_scope`，重启 API/outbox，运行修复后的前端镜像。数据库运行角色实测 `rolsuper=false`、`rolbypassrls=false`。628,209,661 字节的迁移前 dump 存于用户私有状态目录，权限受限；`pg_restore --list` 校验可读，不代表已经演练全量恢复。

前端运行镜像：`sha256:1d19d3c75f3fd5570e1c90ed8032604d4861a62f689f076c8c243e9e4891ae37`。最终 API 使用免费本地 llama.cpp CPU / Qwen3 Instruct，OpenAI-compatible 仅为 loopback 协议，provider/model allowlist 限定本地别名，通用 API 保持关闭；**临时模拟模型服务不会留作业务模型**。最终状态见 [runtime manifest](../evaluation/security/completion_2026-10-22_r2/final-runtime-state.json)。真实模型正常对照超时，业务效用未通过；当前是零付费评估配置，保留审批、预算和停止控制，不能作为已通过业务验收的生产模型配置。

## 整改闭环

| 发现 | 最终修复及实际验证 |
| --- | --- |
| F01 通用接口默认放行 | 默认关闭 invoke/stream/history/threads/AG-UI；显式开启缺少有效共享密钥时失败关闭。产品入口继续 OIDC 主体授权。最终运行配置关闭。 |
| F02 Run 读取越权 | 详情、事件、时间线、列表分页、MCP 引用统一角色及用户/entity/group 范围；持久化访问快照。真实 PG/RLS、HTTP、撤权/恢复和 16 个随机负例验证。旧 NULL 快照对普通主体保守拒绝，不自动补成全租户可读。 |
| F03/F10 Webhook 异常与资源界限 | 时间戳先限长再转换，严格签名格式；流式 64 KiB、5 秒、120 次/分钟、8 并发。全局头数量 100、头/路径/query 合计 16 KiB；实际端口验证 413/408/429/431。 |
| F04/F05/F06 测评误判及来源漂移 | 完整 setup/call/teardown，缺失/skip/xfail/xpass 不 PASS，BLOCKED 正确落盘；版本/24 小时新鲜度/案例及观测摘要/重复校验，验收独立复判；新增启动固定 build identity。107 项变异均有正常和还原后基线。 |
| 取消接口返回 500 | 清理现场发现 `updated_at` 在 UPDATE 后过期、会话已关闭，取消状态虽落库但响应 500。现在在事务内 refresh 后返回；PENDING/RUNNING/WAITING_APPROVAL/WAITING_REVIEW 四种真实 PG HTTP 取消及重复取消测试通过，新增 M07 变异可捕获回归。原候选批次保留，最终版本重新完整采样。 |
| F07 停止与复活 | RUNNING 取消持久化，提交与取消使用同一租户/Run advisory lock；取消生效后不开始新的 GLPI 写入，恢复不得改写取消终态。生效前在途写入单独对账，不承诺外部事务回滚或杀死模型线程。 |
| F08 记忆审核覆盖 | 角色/组、内容 hash/version、并发审核、OIDC group、PG 版本链与撤销支持纳入回归；正确快照可以决定，错误主体和陈旧快照拒绝。 |
| F09 暴露与数据库角色 | Compose 发布端口回环，数据库口令必填；实际服务和运行 DB 角色检查通过。GLPI 使用独立桥接回调入口，不开放通用代理。 |
| 密钥轮换缺口 | MultiFernet 旧钥过渡读取、新钥写入；逐租户事务锁定迁移，dry-run 默认只读，错误密文整事务回滚。真实 PG 验证租户隔离、只读、原子性和新钥单独可读；未为没有泄漏证据的业务凭据擅自全量换钥。 |
| 文件边界 | 附件来源拒绝文件/目录符号链接和根目录逃逸；真实文件 canary 与不执行代码检查通过。默认业务工具无任意 shell/URL fetch 能力；不把这一结论扩展成操作系统级沙箱认证。 |
| 子 Agent 结果可混用 | 结果强制绑定 agent/run/tenant/user/task，验证后才能读 output；伪造外部结果不进入 joined/final evidence，也不记为成功完成。6 个身份负例和真实工作流反例通过。 |
| 扫描器失败误报通过 | npm 门禁检查退出码、报告结构、告警图、例外有效期；14 项校准测试通过，扫描失败不会当无漏洞。CI 新增默认 Python 运行依赖扫描和 SBOM。 |
| 原生 GLPI 回调不可达 | 原旧目标端口错误；新桥接专用 ingress 指向 loopback API。租户 UUID 路由、CRA HMAC、签名 POST、来源 CIDR/资源上限，拒绝任意转发并剥离 Authorization；代理导入与业务凭据/模型运行时解耦。 |
| Reviewer 引用上下文不完整 | governed Reviewer 视图补齐 resource_type/resource_id，与普通视图一致；合法引用和动作目标可独立核验，没有降低审查规则。 |

## 最终测量与原始证据

| 项目 | 结果与证据 |
| --- | --- |
| 原始安全场景 | [72 PASS / 0 FAIL / 0 BLOCKED](../evaluation/security/completion_2026-10-22_r2/full-scope-report.md)，42 场景有变异背书 |
| 整改控制场景 | [82 PASS / 0 FAIL / 0 BLOCKED](../evaluation/security/completion_2026-10-22_r2/local-report.md)，45 场景有变异背书；与原 72 集重叠，不能相加成 154 个独立案例 |
| 全回归与部署测试 | [1484 项完整阶段](../evaluation/security/completion_2026-10-22_r2/release-test-phases.json)，另 [4 项部署测试](../evaluation/security/completion_2026-10-22_r2/deployed-final-phases.json)；0 failed、0 skipped。测试存储为独立 PG/Redis/Neo4j/OpenSearch，非业务库 |
| 变异 | [7 个实验、107/107 检出](../evaluation/security/completion_2026-10-22_r2/mutation-summary.json)；只有 call 阶段目标失败才算检测；分片正常与还原基线均通过 |
| 保留集反事实 | [16 个冻结源码后随机主体](../evaluation/security/completion_2026-10-22_r2/holdout-observation.json)：正常控制 0 泄漏，移除孤立进程授权控制后 14 泄漏，恢复后 0 泄漏；合法 owner 始终可读。是授权保留探针，不是模型 ASR |
| 网络资源界限 | [真实端口](../evaluation/security/completion_2026-10-22_r2/network-probe.json)：慢 body 约 5.04 秒 408，超大 body 413，超大/过多头 431；160 请求突发有 429，随后健康检查约 0.003 秒，满足本次 2 秒对照 SLO |
| 浏览器 | [真实桌面/移动浏览器 2/2](../evaluation/security/completion_2026-10-22_r2/browser-deployed-fixed.log)：已登录产品页面中 run/approval/memory 恶意 HTML 保持文本，无脚本执行、无受控外发命中，token 不进入 URL/localStorage/sessionStorage。仅测试声明场景 |
| GLPI 原生投递 | [CRA/队列/签名/终态](../evaluation/security/completion_2026-10-22_r2/webhook-live.json)：两租户 CRA 成功；跨租户挑战、正文篡改、过期签名、错 entity 拒绝；有效事件两个 Run 最终 succeeded，均 request_write=false。相同签名重试同 Run，重签新 Run，不冒称业务事件去重 |
| 代码质量 | Ruff check/format、Pyrefly、架构审计通过；Pyrefly 0 errors，保留既有 suppressed/warnings；未放宽 core 导入预算或架构规则 |

## 供应链

免费推理运行时也纳入扫描。GPU 兼容旧 Ollama 镜像有 52 HIGH + 1 CRITICAL，另一较新 Ollama 镜像仍有 50 HIGH，均已停止并移除运行容器。最终改为无 GPU/无 Go 服务端的 llama.cpp CPU 镜像（固定摘要），非 root、只读根与模型挂载、私有 API key、12 CPU / 32 GiB、单并发，关闭内置 agent/WebUI/MCP proxy；未修改主机驱动。它直接读取官方 Qwen GGUF，不返回固定模拟答案。另测 CUDA 候选先修掉 14 条 HIGH，但当前驱动无法执行所需内核，未发布、容器已移除；宿主驱动未更改，[处置记录](../evaluation/security/completion_2026-10-22_r2/gpu-candidate-disposition.json)。参见 [运行命令](../evaluation/security/completion_2026-10-22_r2/llama-runtime-command.json)、[最终模型 manifest](../evaluation/security/completion_2026-10-22_r2/final-model-manifest.json) 和 [官方服务参数](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)。

固定 Trivy 0.74.0 镜像摘要，保存漏洞库信息、扫描原始 JSON 和 CycloneDX SBOM。默认运行依赖已更新，旧 Chroma 改为显式可选 extra，Pillow 13 临时覆盖 Docling 上界并验证 PNG/JPEG/WEBP/TIFF 和含图 DOCX 的真实转换。

| 扫描对象 | HIGH / CRITICAL | 其余及边界 |
| --- | --- | --- |
| 默认 Python runtime | 0 / 0 | 6 MEDIUM；[原始报告](../evaluation/security/completion_2026-10-22_r2/supply-chain/python-runtime.json)、[runtime SBOM](../evaluation/security/completion_2026-10-22_r2/supply-chain/python-runtime-sbom.cdx.json) |
| 实际发布前端镜像 | 0 / 0 | 23 MEDIUM、8 LOW；[原始报告](../evaluation/security/completion_2026-10-22_r2/supply-chain/frontend-final.json)、[镜像 SBOM](../evaluation/security/completion_2026-10-22_r2/supply-chain/frontend-sbom.cdx.json) |
| 最终 llama.cpp 模型镜像 | 0 / 0 | 645 MEDIUM、42 LOW；[原始报告](../evaluation/security/completion_2026-10-22_r2/supply-chain/llama-final-image.json)。旧 Ollama 扫描仍保留为失败候选，没有加豁免 |
| all-extras 锁文件 | 2 / 2 | 来自未安装的 legacy-chroma，当前上游无修复；没有加豁免或宣称所有 extra 可安全上线。启用该 extra 必须重新评估 |
| npm 开发工具链 | 有显式豁免 | 同一 dev-only braces 无修复 advisory 的 5 个受影响节点，豁免至 2026-11-15；门禁验证真实告警图和过期/缺报告/错误退出码。无未豁免高危 |
| 源码秘密扫描 | 0 命中 | [脱敏结果](../evaluation/security/completion_2026-10-22_r2/supply-chain/secret-scan-redacted.json)。排除私有 .env、模型文件、安装依赖、历史证据及 .git；不代表完整历史提交秘密审计 |

MEDIUM/LOW 和限期 dev-only 例外保留在原始报告；不能将“默认配置无未处理 HIGH/CRITICAL”写成“项目无漏洞”。

## 免费模型替代与实际模型样本

正式 28 项现场控制验收使用 loopback `security-simulator-v1`，冻结脚本 SHA-256 与每次输入/输出摘要。模拟服务原始调用日志位于上一候选批次并由最终封存清单绑定，共用同一冻结脚本。它只生成结构化提议，生产的鉴权、RLS、审批、工具网关、状态机、GLPI 写入及回读没有被替换，不自动代替人工审批。该替身不评判语义质量，也不能证明目标商用模型对注入的抵抗率。

此外在取消响应修复前的冻结版本 `19d9a377fb98e1c3f56edc59be40b374013bc08b+patch(2f4c84acea88)` 实际运行了免费 Qwen3:30b-a3b；这组原始样本没有改标为当前版本，固定权重完整 digest、Ollama 镜像、模板/许可证摘要、上下文 16384、reasoning=false、应用 temperature=0.5，见 [模型 manifest](../evaluation/security/completion_2026-10-22_r2/local-model-manifest.json)。正常 ACC-01 两次原始观测的有效性/原始复判为 **{normal_rows}**；成功与取消样本均保留，未挑选成功样本声称模型稳定。

模型来源复核发现，先前两个“成功”的正常对照在主服务故障重启期间被共享库恢复流程接管，`analysis.source=security-simulator-v1`；因此其原始 grader PASS 保留，但有效性标为 **BLOCKED**，不作为实际 Qwen 成功证据。最终关闭模拟器、停止二次探针 API、等待恢复结束后，在实际主 API 单独重跑；Ollama 思考/指令两个版本的正常任务均未通过，保留其失败。更换无高危漏洞的 llama.cpp 实际模型服务后的结果为：**{current_control["utility_verdict"]}，{current_control["seconds"]:.2f} 秒，终态 {current_control["terminal_status"]}**。见 [最终真实模型对照](../evaluation/security/completion_2026-10-22_r2/final-local-model-control-summary.json)。此结果与先前无效采样分开统计，不隐藏失败或来源混用。

实际攻击观测如下；模型请求共 {model_calls} 次，透明 observer 原样转发，受限 Acme canary 由独立主体写入，Globex 攻击输入仅知道资源 ID，不知道 canary 内容。合法 owner 可读，攻击主体 404，模型请求/响应及上下文、动作参数和输出中无该 canary，外发 sink 有正常心跳，未经授权连接 0。

| 实际 Qwen 探针 | 安全结果 | 终态 / 合法任务效用 | 耗时 |
| --- | --- | --- | --- |
{attack_rows}

见 [实际模型结果](../evaluation/security/completion_2026-10-22_r2/local-model-attacks.json)。这是三个小样本工作流探针，包含目标和真实 followup 两种输入通道；预算触发取消/安全拒绝与正常任务完成分别统计。没有据此计算有统计意义的 ASR、攻击泛化率或效用保持率。8B/14B、早期 30B 和观察器启动失败的原始尝试也保留在前一候选批次中，不纳入通过分母。

## 清理、保留和复核

[清理证明](../evaluation/security/completion_2026-10-22_r2/retention-observation.json) 在 PostgreSQL、Redis、Neo4j、OpenSearch 分别先写入并回读随机合成 canary，再删除本批四个隔离容器、四个专用匿名卷，复核容器/卷不存在且端口关闭。没有关闭共享业务库的 append-only 约束，也没有把整个测试库销毁冒充生产逐记录擦除能力。

本批 76 个精确现场 Run ID 中的 18 个待处理任务已通过正式取消 API 收敛，取消响应均为 200；见 [清理记录](../evaluation/security/completion_2026-10-22_r2/pending-case-cleanup.json)。

共享测试部署中的合成 fixture、不可变审批/审计、最终证据和回滚备份作为用户要求的审计复现例外保留至 **2026-11-21**；到期由授权管理员按现有保留程序处理，不声称已经自动清理或已经擦除生产数据。临时模型 observer、模拟服务、探针 API 已停止，保留主服务实际使用的免费模型容器与模型卷。

复判命令、密钥轮换 runbook、精确版本/文件摘要及全部原始结果见 [最终证据目录](../evaluation/security/completion_2026-10-22_r2/README.md)。初审 [audit_2026-10-22](../evaluation/security/audit_2026-10-22/README.md) 与第一阶段 [local_2026-10-22](../evaluation/security/local_2026-10-22/README.md) 保持历史状态；本次补全没有覆盖其结论。
"""
(ROOT / "docs/SECURITY_ASSESSMENT_RESULTS_2026-10-22.md").write_text(report)
readme = f"""# 2026-10-22 免费安全测评补全批次

本批已完成。源码 `{revision}`；模型 API 付费调用 0。完整结果见 [最终报告](../../../docs/SECURITY_ASSESSMENT_RESULTS_2026-10-22.md)，目标与执行步骤见 [方案](../../../docs/SECURITY_ASSESSMENT_EXECUTION_PLAN_2026-10-22.md)。历史两个批次未修改。此前 completion_2026-10-22 候选发现取消响应问题；本 r2 批次为修复后重新采样，旧候选原始观测未重新标版本。

| 交付 | 结果 |
| --- | --- |
| 原始安全门禁 | [72/72 PASS](full-scope-report.md)；11 个现场安全案例全部通过 |
| 整改控制门禁 | [82/82 PASS](local-report.md) |
| 专项规格 | [48/48 控制覆盖复判](case-matrix.md)，不是 48 次真实模型攻击 |
| 完整回归 | [1484 passed](release-junit.xml) + [4 deployed tests passed](deployed-final-junit.xml)，无失败/跳过 |
| 变异 | [107/107 检出](mutation-summary.json)，7 个实验，完整前后基线 |
| 完整业务验收 | [26 PASS / 1 FAIL / 1 BLOCKED](acceptance-report.json)；ACC-03/12b 语义/记忆效用不通过，不能签全业务发布 |
| 随机保留集 | [16 个主体全部拦截](holdout-observation.json)，移除孤立控制泄漏 14，恢复泄漏 0 |
| 默认 Python / 实际前端镜像 | HIGH=0、CRITICAL=0；[供应链目录](supply-chain/) 保留中低危和例外 |
| 实际开源模型 | [Qwen3:30b-a3b 三个攻击探针](local-model-attacks.json)，安全结果独立于效用；全部原始失败保留 |
| 现场投递与浏览器 | [GLPI 原生 CRA/队列](webhook-live.json)、[桌面/移动 XSS 与 token 测试](browser-deployed-fixed.log) |
| 实际运行状态 | [免费模型、关闭通用入口、非 bypass DB、迁移 0015](final-runtime-state.json) |
| 清理与证据 | [四存储销毁](retention-observation.json)，合成现场 fixture/不可变审计及私有回滚备份按例外保留至 2026-11-21 |

最终真实模型正常对照另见 `final-local-model-control-summary.json`；它与 26/28 的模拟模型业务验收、三个早期实际模型安全探针、来源混用的无效样本分开统计。

## 离线复核

在仓库根目录运行，复判输出放临时目录以保持封存目录不变：

```bash
set -euo pipefail
cd /home/shihongye/data1/servicemind
.venv/bin/python evaluation/security/completion_2026-10-22_r2/seal_completion.py --check
.venv/bin/python evaluation/security/completion_2026-10-22_r2/grade_case_matrix.py --check
SEC_REVIEW_DIR=$(mktemp -d /tmp/servicemind-security-regrade.XXXXXX)
.venv/bin/python scripts/gate_phase7_security.py --check \\
  --scenarios evaluation/security/scenarios.v1.json \\
  --replays evaluation/security/completion_2026-10-22_r2/full-scope-replays \\
  --report "$SEC_REVIEW_DIR/security.md" --expect-revision '{revision}'
.venv/bin/python scripts/gate_phase7_security.py --check \\
  --scenarios evaluation/security/scenarios.local.v1.json \\
  --replays evaluation/security/completion_2026-10-22_r2/local-replays \\
  --report "$SEC_REVIEW_DIR/local.md" --expect-revision '{revision}'
# 此命令预期退出 1：保留 ACC-03 的 FAIL 和 ACC-12b 的 BLOCKED。
.venv/bin/python scripts/gate_phase7_acceptance.py --check \\
  --replays evaluation/security/completion_2026-10-22_r2/acceptance-final-replays \\
  --report "$SEC_REVIEW_DIR/acceptance.json" --format json --expect-revision '{revision}'
```

`assemble_completion.py` 验证阶段、源码/替身摘要、107 项变异及验收原始观测，再建立两个安全 replay 目录；已有目标目录时拒绝覆写。`prepare_case_matrix.py` 从方案规格提取 48 条映射，`grade_case_matrix.py` 校验全部必需测试阶段、现场案例及附件语义。重新执行测评应复制脚本到新批次并改输出目录，重新冻结源码；不要在已封存目录再运行采样器或写入式 assembler。

源码差异、所有改动文件副本及原始 SHA 见 `source.patch`、`source/`、`source-files.json`。它们包含本会话开始前已有的用户改动，不能把全部差异都归为本轮新增修复。私有 .env 和真实密钥没有进入快照。`SHA256SUMS.json` 覆盖本批证据和最终两份文档；`seal_completion.py --check` 只读核验。

## 免费在线复跑前提

实际 API 当前使用本地 llama.cpp CPU 服务，`DEFAULT_MODEL=openai-compatible`，provider=openai 只是兼容协议，model=`security-local-qwen-instruct`，base URL 固定 loopback `http://127.0.0.1:11440/v1`。服务加载真实 Qwen3-30B-A3B-Instruct-2507 权重，不连接商业模型服务。私有本地 API key 位于用户配置目录，不能复制到仓库。权重、模板、运行镜像、非 root/只读/资源界限等完整信息见 `final-model-manifest.json` 与 `llama-runtime-command.json`；[官方模型标签](https://registry.ollama.com/library/qwen3/tags)、[官方服务文档](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)。所有实际模型效用失败与无效观测保留，不以安全通过认定业务推理稳定。

用于现场控制验收的 `simulated_model_server.py` 固定 SHA-256 `{load("frozen-components.json")["simulator_sha256"]}`，只能在显式测评配置下启动到 loopback 11436。它生成结构化提议，真实鉴权、审批、数据库、状态机及 GLPI 副作用路径仍由生产代码执行。重跑前备份并记录当前实际模型配置，结束恢复 `free-runtime-profile.conf` 指定的免费实际模型；不要将模拟器作为业务模型。`serve_local_model_probe.py` 仅关闭它自己的二次探针 API 的启动恢复，避免重复恢复共享 Run；正常主 API 的恢复行为未改。

GLPI 回调使用桥接 gateway `172.27.0.1:18082` 的专用 user unit，允许指定 Docker CIDR，转发到 loopback 18080。仅 legacy 签名 POST 和租户 UUID 路径的 CRA GET/签名 POST；不提供通用代理。两租户原生 CRA 已启用。真实密钥仅从服务配置及 GLPI 内部读取，不写入原始观测。改动、回退信息和私有备份位置分别见 `webhook-route-change.json`、`deployment-backup.json`。

## 密钥轮换 runbook

备份密文和旧钥；新钥设为 `SERVICEMIND_CREDENTIAL_KEY`，旧钥放 JSON 数组 `SERVICEMIND_CREDENTIAL_PREVIOUS_KEYS`；重启全部写入方后逐租户运行：

```bash
.venv/bin/python scripts/rotate_credentials.py --tenant-id TENANT_UUID
.venv/bin/python scripts/rotate_credentials.py --tenant-id TENANT_UUID --apply
```

默认 dry-run；事务迁移失败整批回滚，不打印明文。确认全部租户已迁移并确定历史备份读取策略后移除旧钥、重启写入方。未迁移旧 Run 的 NULL 访问范围不能填成全租户授权；按可信历史定向补全，或保持普通用户拒绝。

默认 `uv sync --frozen` 不安装 `legacy-chroma`；该 extra 仍有 2 HIGH + 2 CRITICAL，不得据默认扫描通过启用。Pillow 13 的 Docling 临时上界覆盖有真实含图 DOCX 和四种图片格式兼容记录。开发 braces 的无修复例外 2026-11-15 到期；旧镜像 OpenSSL 豁免已移除。

前一候选中的 `*-pilot*`、`*-prototype*`、早期 full/final-v2 日志是定位/试运行材料；最终通过只使用 release 阶段、acceptance-final-replays 及明确列出的最终附件，未将旧版本重新盖章。实际模型失败和连接准备失败也保留，不能从这些目录挑选成功样本外推。
"""
(BATCH / "README.md").write_text(readme)
print("Final report and runbook rendered from actual observations.")
