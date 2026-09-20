# ServiceMind Phase 5 最终架构与验收签署 v1.3

> 复核日期：2026-09-17
> 范围：Governed Memory、Context Engineering、Versioned Skills、Model Gateway
> 结论：**Phase 5 工程验收通过；RAG 业务质量继续保留条件例外**

## 最终架构

Phase 5 冻结为四个独立模块。Memory 属于 Harness 中间件，不是新增 Agent，也没有 Action 权限；LangGraph checkpoint 只保存运行态，长期 Memory 独立治理；正式 Runbook、SOP 和组织事实继续由 RAG 管理。

- **Memory**：Semantic（含已同意 Preference）、Episodic、Procedural 三类；候选需通过 secret、PII/consent、taint、证据、终态、重要度、置信度和冲突门禁。Procedural 生产者只按跨工单重复的规范化建议提出 quarantine 候选；必须引用至少两个不同成功 Run、不同工单的 active Episode，并经 ACL 范围内人工审核才能激活。撤销或过期的支撑证据会在候选读取和最终重校验时撤销依赖 Procedure，并留下 append-only 事件。
- **Context**：每个业务 Agent 有独立来源 allowlist；模型边界前执行 token 预算、确定性选择、secret/PII 脱敏和 unresolved taint 拒绝。审计只保存哈希、选择清单、预算和脱敏计数。
- **Skills**：版本、scope、允许 Agent、所需工具、风险、输入输出 schema、证据要求和 checksum 全部冻结；有效能力始终取 Agent、用户、租户、Skill 与 Policy 的交集。
- **Model Gateway**：所有 ServiceMind structured model call 统一经过 provider/model allowlist、超时、受限重试、schema 验证、成本上限和持久审计；高风险 Reviewer 禁止 fallback；semantic cache 在生产保持关闭。

## 审计中修复的关键缺陷

1. Context Builder 已成为子 Agent 的唯一模型输入边界，原始 evidence 不能绕过脱敏和 token 裁剪。
2. Memory 使用 PostgreSQL 权威记录和固定 revision 的 BGE-M3 派生向量；向量计算完成后重新校验 tenant、scope、状态、TTL 和 taint，关闭撤销竞态。
3. Memory provenance/taint 已迁移为 JSONB，数据库约束、强制 RLS、append-only trigger 和并发 advisory lock 均已落地。
4. RAG 索引原先写入子块哈希却按文档哈希校验，会错误丢弃合法候选；现固定为文档权威哈希并升级 schema v3。
5. 蓝绿索引原先在 refresh 前发布，可能将空新代留在未激活状态；现固定为 write → refresh → publish。
6. 召回漏斗扩大为 100/100/100，OpenSearch fusion depth 与返回 size 分离；重排包含标题，并按 85% cross-encoder + 15% 归一化 RRF 排序。
7. BGE-M3 与 BGE-Reranker 分别绑定独立 RTX 3090；100 候选重排从约 65 秒降到 0.80 秒。
8. Episodic 提取器已改为显式声明策略的 TENANT 作用域并保留 entity/group ACL，关闭了
   Procedural 激活守卫与 USER 作用域情节互斥的死路；遗留未声明行继续不服务。
9. Procedural 生产者只在同一规范化建议出现于两个不同工单和不同 Run 时生成候选；
   同工单幂等/版本冲突不会被错误计为两条支撑，不同建议不会被合并。
10. 新增角色、租户、entity/group ACL 约束的人工待审 API。审批决定在仓储锁内绑定
    版本、内容摘要和 quarantine 状态；并发相反决定只有一个能成功，决定写入 append-only 审计。
11. 修复 Procedure 自身 TTL 长于支撑 Episode 后继续服务的问题。激活、候选读取和最终
    重校验共用同一支撑判定；支撑失效先转 `REVOKED` 并审计，再从模型上下文排除。
12. 待审 API 已接入 Streamlit **Memory Review** 页面；页面使用 Keycloak OIDC access token，
    不复制授权规则，审批仍由后端 `approver`、tenant/entity/group ACL 与 exact-snapshot CAS 裁定。
13. Model Gateway 的估算计数不再在冷启动时下载 `cl100k_base`。无供应商 usage 时使用确定性、
    无网络的 UTF-8 byte 上界，成功响应仍以供应商 token 为准；代理或公网故障不再阻断模型治理。

## 2026-09-14 验收证据

| 门禁 | 结果 | 证据 |
| --- | --- | --- |
| 全仓自动化 | PASS | 422 passed，6 skipped，0 failed，38 条第三方弃用 warning |
| Phase 5 专项 | PASS | 32 passed，0 failed |
| Phase 4–6 联合专项 | PASS | 89 passed，1 skipped，0 failed |
| 静态与 schema | PASS | Ruff 0 error；ServiceMind 生产路径 Pyrefly 0 error；Alembic drift 0 |
| 当前数据库 | PASS | head=`0013_phase6_hash_guards`；四张 Phase 5 表强制 RLS、append-only |
| 全新数据库 | PASS | upgrade → full downgrade → upgrade；Phase 5 四表保留完整 |
| Memory 在线治理 | PASS | zero-context denial、跨租户隔离、并发幂等、BGE-M3 排序、撤销和审计 |
| 真实模型 | PASS | DeepSeek structured result、显式 tenant route、provider token、cost provenance |
| 真实工作流 | PASS | Router → governed Context → Knowledge/RAG → Model Gateway → tenant audit |
| RAG 在线验证 | PASS | 5 条证据；顶部 `runbook://rb-vpn-mfa`；内容绑定 citation；5.54 秒 |
| 常驻服务 | PASS | API、Streamlit、独立 outbox worker 均 active/healthy |

本轮全仓 `pyrefly check` 为 **0 errors**（按项目配置有 17 项 suppression，另有 14 条 warning
未展开）。该结果是类型门禁，不用于评价项目外实例 Agent 的业务质量。

### 2026-09-16 至 2026-09-17 运行入口复验

本服务器的 API 由用户级 unit `servicemind-api.service` 托管，监听
`127.0.0.1:18080`；系统级 `systemctl` 与 8080 均不是该 API 的权威检查入口。
`scripts/verify_servicemind_runtime.py --check` 会联合校验 unit scope、MainPID、
ExecStart、工作目录、`.env` 端口及 `/health`，并显式绕过环境代理。机器证据写入
`evaluation/reports/servicemind_runtime_latest.json`。生产 Context/Memory 投递观测写入
`evaluation/reports/phase5_context_delivery_observed_latest.json`；当前排除 13 条合成验收
artifact 后为 `NO_DATA`，不得表述成生产质量 PASS。

### 2026-09-16 Procedural 生产与复核闭环

`SERVICEMIND_MEMORY_PROCEDURAL_PROPOSALS_ENABLED=true` 已在本部署启用。生产者只生成
quarantine 候选，无法绕过人工审核。统一待审队列可按 memory type 过滤，既承载 Procedural，
也承载其他冲突/低置信度记忆。`GET /v1/servicemind/memories/review-queue` 与
`POST /v1/servicemind/memories/{id}/review` 使用现有 `approver` 角色，并同时检查 tenant、
entity 与 OIDC `glpi_group_ids`。Keycloak 已有 realm 的 mapper/profile/user attributes 与 seeded reviewer 的 realm role 由
`scripts/reconcile_phase5_memory_review_identity.py --check` 作为漂移门禁；缺少组 claim 时
按空权限处理。roles 属于同一身份契约：队列以 `require_role("approver")` 为准，而 realm import
只在创建期分配角色，因此门禁双向比对声明角色（缺失会使队列返回 403，多出的未声明角色即越权），
apply 会写回声明 attributes，并在修改 mapper/profile/attributes/roles 后重读验证；只有后置状态
完全收敛才报告 PASS。当前无真实候选和审核样本，因此这里签署的是路径、隔离和并发正确性，
不声称生产内容质量已被观察验证。

本轮补齐 UI 最后一公里：`src/pages/1_Memory_Review.py` 提供队列、证据/provenance 查看、
批准/拒绝、分页和显式确认；`.streamlit/secrets.toml` 仅在本机以 0600 保存，仓库只提交示例。
`servicemind-streamlit.service` 已重启并通过 `/_stcore/health`。这关闭“只有 API、审批人只能自行
写调用脚本”的操作缺口；是否有人实际处理候选仍需生产队列/审批观测，当前继续为 `NO_DATA`。

Procedure 的支撑关系也改为持续不变量：激活时、候选读取时和最终重校验时复用同一判定。
Episode 过期或失效后，依赖 Procedure 在服务前转为 `REVOKED`，并追加
`PROCEDURAL_SUPPORT_INVALIDATED` 审计事件；PostgreSQL 在线验收已覆盖真实事务路径。

本次增量与全量验收结果：

| 门禁 | 结果 | 证据 |
| --- | --- | --- |
| 全仓自动化 | PASS | **573 passed / 6 skipped / 0 failed**，38 条第三方弃用 warning |
| Procedural + review 专项 | PASS | **88 passed / 0 failed** |
| 复核身份漂移 | PASS | **11 passed / 0 failed**；attributes/roles 实写与后置重读均有证伪测试 |
| 静态检查 | PASS | Ruff 全绿；Pyrefly 0 error；`git diff --check` 0 error |
| Memory 评测 | PASS | lexical / TEI `--check` 均 rc=0；泄漏率 0.0；排名 29/29；投递 4/4 |
| PostgreSQL 在线治理 | PASS | RLS、跨租户、模式情节查询、ACL 待审、错误摘要拒绝、激活、支撑失效撤销、append-only 审计 |
| Keycloak | PASS | mapper/profile/user grant 漂移检查 rc=0；真实 approver token 含 entity/group/role |
| 常驻服务 | PASS | API / Streamlit / outbox user unit active；健康检查通过；真实 token 查询待审队列 HTTP 200 |
| 生产样本 | **NO_DATA** | 13 条合成 artifact 排除后，真实 ANALYSIS 与 Memory 候选信封均为 0 |

## RAG 质量边界

最新固定 TechQA 外部 silver 结果为：生产形态臂（top-100 池 + `0.85×重排分 + 0.15×minmax(检索分)` 融合，生产重排窗口 8192）Recall@5 0.6857、Recall@10 0.7643、Recall@20 0.8071、MRR@10 0.5848、NDCG@10 0.6279，四个点估计都低于 0.85/0.90/0.75/0.80 租户参考阈值；由于数据是外部 silver，这只能记为 `BELOW_TARGET_DIAGNOSTIC`，租户六门禁仍为 `NOT_EVALUATED`。该代理集的 0.87 / 0.275 是留出集上的**检索 top-score 阈值代理**，不是 Reviewer 端到端拒答/作答率；ROC-AUC 0.6035、所有切点的最佳 balanced accuracy 0.5979 证明这个数值信号很弱，Reviewer 语义判准率仍为 `NOT_EVALUATED`。8 文档/15 query 的自建 gold 四臂已饱和，只允许作为 smoke regression。机器真值见 `evaluation/reports/rag_quality_status_latest.{json,md}`。

因此 Phase 4 继续标记 `QUALITY_EXCEPTION_ACCEPTED`。Phase 5 的隔离、治理、工程正确性和在线链路已通过；在无法取得专家签署和真实生产查询日志的条件下，不把它表述为“RAG 业务质量已全面认证”。

## 签署

Phase 5 在 ServiceMind 业务范围内达到当前工程冻结标准，验收范围内没有已知未修复缺陷。该结论基于上述固定测试和在线环境，不构成对未来输入、供应商故障或未执行负载形态的绝对无缺陷保证。机器可读证据见 `evaluation/reports/phase5_acceptance_latest.json`。
