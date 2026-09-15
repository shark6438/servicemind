# Phase 5 acceptance report v1.1

> 复核日期：2026-09-14
> 范围：Governed Memory、Context Engineering、Versioned Skills、Model Gateway
> 结论：**Phase 5 工程验收通过；RAG 业务质量继续保留条件例外**

## 最终架构

Phase 5 冻结为四个独立模块。Memory 属于 Harness 中间件，不是新增 Agent，也没有 Action 权限；LangGraph checkpoint 只保存运行态，长期 Memory 独立治理；正式 Runbook、SOP 和组织事实继续由 RAG 管理。

- **Memory**：Semantic（含已同意 Preference）、Episodic、Procedural 三类；候选需通过 secret、PII/consent、taint、证据、终态、重要度、置信度和冲突门禁。Procedural 必须引用至少两个不同成功 Run 的 active Episode，并经人工审核才能激活。撤销证据会级联撤销依赖 Memory。
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

Pyrefly 的全仓扫描还会报告通用 starter 模板、可选 Playwright 脚本、假测试对象和无关实例 Agent 的既存类型债务。本次依照项目边界，没有改动这些非 ServiceMind 业务链路；ServiceMind 生产代码和 Phase 5/6 验证器已作为零错误门禁。

## RAG 质量边界

最新固定 TechQA 外部 silver 结果为：Recall@5 0.6821、Recall@10 0.7286、MRR@10 0.5699、NDCG@10 0.6087、不可回答拒答率 0.78、可回答作答率 0.3333，未达到预定 0.85/0.90/0.75/0.80/0.90/0.90 门禁。它证明当前公开分布上的差距，也不能替代租户 ITSM 专家 gold set。

因此 Phase 4 继续标记 `QUALITY_EXCEPTION_ACCEPTED`。Phase 5 的隔离、治理、工程正确性和在线链路已通过；在无法取得专家签署和真实生产查询日志的条件下，不把它表述为“RAG 业务质量已全面认证”。

## 签署

Phase 5 在 ServiceMind 业务范围内达到当前工程冻结标准，验收范围内没有已知未修复缺陷。该结论基于上述固定测试和在线环境，不构成对未来输入、供应商故障或未执行负载形态的绝对无缺陷保证。机器可读证据见 `evaluation/reports/phase5_acceptance_latest.json`。
