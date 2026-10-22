# 2026-10-22 免费安全测评补全批次

本批已完成。源码 `19d9a377fb98e1c3f56edc59be40b374013bc08b+patch(9d7e9716f6ac)`；模型 API 付费调用 0。完整结果见 [最终报告](../../../docs/SECURITY_ASSESSMENT_RESULTS_2026-10-22.md)，目标与执行步骤见 [方案](../../../docs/SECURITY_ASSESSMENT_EXECUTION_PLAN_2026-10-22.md)。历史两个批次未修改。此前 completion_2026-10-22 候选发现取消响应问题；本 r2 批次为修复后重新采样，旧候选原始观测未重新标版本。

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
.venv/bin/python scripts/gate_phase7_security.py --check \
  --scenarios evaluation/security/scenarios.v1.json \
  --replays evaluation/security/completion_2026-10-22_r2/full-scope-replays \
  --report "$SEC_REVIEW_DIR/security.md" --expect-revision '19d9a377fb98e1c3f56edc59be40b374013bc08b+patch(9d7e9716f6ac)'
.venv/bin/python scripts/gate_phase7_security.py --check \
  --scenarios evaluation/security/scenarios.local.v1.json \
  --replays evaluation/security/completion_2026-10-22_r2/local-replays \
  --report "$SEC_REVIEW_DIR/local.md" --expect-revision '19d9a377fb98e1c3f56edc59be40b374013bc08b+patch(9d7e9716f6ac)'
# 此命令预期退出 1：保留 ACC-03 的 FAIL 和 ACC-12b 的 BLOCKED。
.venv/bin/python scripts/gate_phase7_acceptance.py --check \
  --replays evaluation/security/completion_2026-10-22_r2/acceptance-final-replays \
  --report "$SEC_REVIEW_DIR/acceptance.json" --format json --expect-revision '19d9a377fb98e1c3f56edc59be40b374013bc08b+patch(9d7e9716f6ac)'
```

`assemble_completion.py` 验证阶段、源码/替身摘要、107 项变异及验收原始观测，再建立两个安全 replay 目录；已有目标目录时拒绝覆写。`prepare_case_matrix.py` 从方案规格提取 48 条映射，`grade_case_matrix.py` 校验全部必需测试阶段、现场案例及附件语义。重新执行测评应复制脚本到新批次并改输出目录，重新冻结源码；不要在已封存目录再运行采样器或写入式 assembler。

源码差异、所有改动文件副本及原始 SHA 见 `source.patch`、`source/`、`source-files.json`。它们包含本会话开始前已有的用户改动，不能把全部差异都归为本轮新增修复。私有 .env 和真实密钥没有进入快照。`SHA256SUMS.json` 覆盖本批证据和最终两份文档；`seal_completion.py --check` 只读核验。

## 免费在线复跑前提

实际 API 当前使用本地 llama.cpp CPU 服务，`DEFAULT_MODEL=openai-compatible`，provider=openai 只是兼容协议，model=`security-local-qwen-instruct`，base URL 固定 loopback `http://127.0.0.1:11440/v1`。服务加载真实 Qwen3-30B-A3B-Instruct-2507 权重，不连接商业模型服务。私有本地 API key 位于用户配置目录，不能复制到仓库。权重、模板、运行镜像、非 root/只读/资源界限等完整信息见 `final-model-manifest.json` 与 `llama-runtime-command.json`；[官方模型标签](https://registry.ollama.com/library/qwen3/tags)、[官方服务文档](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)。所有实际模型效用失败与无效观测保留，不以安全通过认定业务推理稳定。

用于现场控制验收的 `simulated_model_server.py` 固定 SHA-256 `ec1a7367151fae8ecee583b186fd91fc43fefa2ee93b91b6d20dae465810c936`，只能在显式测评配置下启动到 loopback 11436。它生成结构化提议，真实鉴权、审批、数据库、状态机及 GLPI 副作用路径仍由生产代码执行。重跑前备份并记录当前实际模型配置，结束恢复 `free-runtime-profile.conf` 指定的免费实际模型；不要将模拟器作为业务模型。`serve_local_model_probe.py` 仅关闭它自己的二次探针 API 的启动恢复，避免重复恢复共享 Run；正常主 API 的恢复行为未改。

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
