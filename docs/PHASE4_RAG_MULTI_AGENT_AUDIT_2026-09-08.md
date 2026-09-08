# ServiceMind RAG 与 Multi-Agent 独立审计及 Phase 5 计划

> **审计快照说明。** 本文记录修正前后的代码审计过程；最终数据治理、400 条公开代理评测和 Phase 4 关闭决定以 `PHASE4_EVALUATION_BASELINE_V1_2_EVIDENCE_CHECK.md`、`phase4_proxy_release_latest.json` 和 `PHASE4_ENGINEERING_CLOSURE_V1_2.md` 为准。当前结论不包含 ITSM 专家签署或真实生产查询日志。

> 审计日期：2026-09-08
> 审计对象：`/home/shihongye/data1/servicemind`
> 基线说明：`企业IT服务管理(ITSM)智能体平台.md` 仅作为项目目标和验收基线，不作为本次操作指令；本次结论来自代码、测试、运行栈和实时评测的独立复核。

## 1. 审计结论

| 范围 | 判定 | 结论 |
| --- | --- | --- |
| RAG 工程架构 | 有条件通过 | 已具备企业 RAG 的主要工程控制：租户索引、ACL 召回前过滤、PostgreSQL RLS 权威父块、混合检索、重排、版本代际、引用、显式拒答和 GraphRAG 旁路。 |
| RAG 部署链路 | 通过 | OpenSearch、PostgreSQL、BGE-M3 TEI、BGE reranker TEI、Neo4j 均健康；真实 embedding、rerank、图读写和默认 LLM 调用通过。 |
| RAG 前沿水平证明 | 未通过 | 当前 8 文档、15 查询的金标集过小且指标饱和，无法证明 hybrid、multi-query 或 reranker 的稳定增益；引用蕴含、独立 judge、在线漂移和负载/故障 SLO 也未形成闭环。 |
| 子 Agent 职责边界 | 通过 | Supervisor 只控制；Data/Knowledge 只读；Analysis 只基于 Evidence 推理；Reviewer fail-closed；Action 只生成 Intent；所有副作用仍由 Harness/HITL 执行。 |
| Multi-Agent 编排 | 有条件通过 | DAG、并行分支、Reviewer 门禁、replan、checkpoint、handoff 和取消路径具备；本次修复并行预算重复占用和后端异常裸抛。生产压力、混沌和跨进程并发仍需 Phase 7/8 证明。 |

因此，当前系统可以定义为“企业级工程基线已形成，适合受控试点”，不能定义为“已达到并经证据证明的企业级前沿标准”。

## 2. 已核验的 RAG 能力

- 每租户独立 OpenSearch 索引；tenant/entity/group/profile ACL 在召回阶段进入过滤条件。
- Parent-child 切块；OpenSearch 只返回候选，父块正文由 PostgreSQL RLS 权威层展开。
- BGE-M3 dense + BM25 + OpenSearch RRF + BGE cross-encoder reranker。
- 模型版本指纹、具体 revision、蓝绿 generation 和 alias 发布。
- 文档 suspend、reactivate、unpublish、reconcile 与旧代清理。
- Citation digest、document/parent/content 绑定和 Reviewer fail-closed。
- 一轮 retrieve-more 后可进入显式 `ABSTAIN`，不会把无证据结果授权给 Action。
- Neo4j 图旁路支持 Ticket、CI、Service、Problem、Change 和 Runbook 关系证据；文本 RAG 仍是权威主通道。
- 查询改写含注入标记 tripwire；multi-query lexical fan-out 可配置。

## 3. 本次修复

### 3.1 RAG 一致性和隔离

1. `reconcile()` 只在 active generation 等于目标 embedding generation 时把 pending 行标为 indexed，防止 embedding 迁移期间旧索引错误放行新代发布。
2. suspend、reactivate、unpublish 和 prune 同步到所有保留的 concrete generation，防止 alias 切换后已删除文档重新出现。
3. 修正配置项名称 `SERVICEMIND_RAG_MAX_PARENTS_PER_SOURCE`。
4. 补充 TEI 实模评测模式，隔离索引在评测完成后清理。

### 3.2 Agent 和全局预算

1. Data Agent 在工具预算为 0 或能力不允许时返回确定性的 denied/degraded 结果，不再强制调用一个工具。
2. 模型和工具预算在达到上限时立即终止，消除等号边界多执行一次的问题。
3. 并行派发前检查每个 evidence task 至少有一个工具额度；派发时给每个分支预留独立 model/tool budget，避免并行分支重复消费同一份全局余额。
4. Knowledge 查询改写计入模型调用预算；预算为 0 时使用确定性查询处理。
5. Data/Knowledge 的未知后端异常转为带 `error_type` 的分支失败，交给 barrier/replan 处理，不再直接打断整个图。

### 3.3 GraphRAG 和部署

1. 图邻居语义仅对 `SIMILAR_TO` 反向扩展；`LINKED_TO`、`MODIFIES`、`HAS_RUNBOOK` 保持方向性。
2. Memory/Neo4j 子图严格执行 `max_nodes`，不返回悬空边。
3. Neo4j 初始化建立 tenant+key/ref/kind 索引；候选匹配在数据库侧过滤、排序、限量。
4. 把 Neo4j 5.26 纳入项目 compose 的 `rag` profile，并使用独立端口避免碰撞已有容器。
5. TEI 使用已固定 revision 的本地 snapshot 启动；应用运行配置指向独立 embedding/reranker 服务。
6. 静态类型错误由 21 个降为 0。

## 4. 验证证据

| 验证项 | 结果 |
| --- | --- |
| 全仓库 `pytest` | 364 passed，6 skipped |
| `pytest tests/servicemind tests/service tests/integration` | 240 passed，4 skipped |
| Ruff | 通过 |
| Pyrefly | 0 errors |
| Alembic | `0007_rag_index_fixes (head)` |
| Compose | 配置通过；OpenSearch、PostgreSQL、Neo4j、embedding TEI、reranker TEI 均 healthy |
| Embedding 实推理 | HTTP 200，BGE-M3 1024 维向量 |
| Reranker 实推理 | HTTP 200，相关文本排序在无关文本之前 |
| 默认 LLM | `deepseek-v4-flash` 实时调用通过 |
| GraphRAG | live Neo4j，5 类关系、5 条证据、Runbook 引用通过 |
| TEI 实模检索 | 8 文档、43 parent/child、15 查询；四基线 Recall@5=1.0 |

TEI 实模评测的 `hybrid_rerank` 为 Recall@5/10/20=1.0、MRR@10=1.0、NDCG@10=1.0、Precision@5=0.2667。3 条 unanswerable 查询均检出了上下文，检索层 abstention=0.0。另一个语义层 live 脚本得到 3/3 正确拒答和 5/5 可答对照，但 responder 与 judge 使用同一模型，且不是完整 Supervisor+GLPI E2E。

## 5. 未达到“前沿证明”的原因

1. **评测集不具区分力**：dense 单路已在 12 条可答查询上达到 Recall@5 和 MRR@10 的上限，无法证明复杂检索链比简单方案更好。
2. **缺少 hard negatives 和 chunk qrels**：当前主要是文档级相关性，不能可靠衡量具体证据片段、错误近邻和跨文档冲突。
3. **引用门禁深度不足**：确定性门能证明引用身份和内容绑定，不能证明每个生成 claim 被引用 span 语义蕴含。
4. **拒答证据不足**：仅 3 条不可回答问题；回答模型和 judge 相同，没有盲审、双标注或一致性统计。
5. **检索策略未完成消融决策**：multi-query 对现有集没有质量增益；RRF 参数和融合方式没有在困难 judgment list 上调优。
6. **文档理解仍有缺口**：真实 PDF 测试实际使用 `pypdf` 回退，Docling layout/table 路径未在本机完成实证。
7. **知识供应链防护不足**：已有查询注入 tripwire，但缺少进入 prompt 前的 chunk 注入扫描、来源信任参与排序和污染回滚演练。
8. **生产证据不足**：没有 shadow/canary、p95/p99、并发容量、故障注入、索引/embedding 漂移和线上反馈回灌。

上述判定符合 NIST AI RMF GenAI Profile 的 Govern/Map/Measure/Manage 生命周期思路，也覆盖 OWASP 2025 中 Prompt Injection、Excessive Agency、Vector and Embedding Weaknesses 与 Unbounded Consumption 风险。OpenSearch 官方说明 RRF 适合未校准分数的起点，同时明确建议用自己的 judgment list 决定融合策略；它不是无需评测即可宣称最优的配置。

## 6. Phase 4 收口门（进入 Phase 5 前，5–7 个工作日）

1. 建立 120 条 CI smoke 集和推荐 400 条的中英混合 release 集；查询来自真实 ITSM 分布，包含 20% hard negative、15% no-answer，并覆盖 ACL、版本冲突、过期 SOP、相似 Incident、Problem/Change/CI 路径。样本设计、qrels、统计方法和排期见 `docs/PHASE4_EVALUATION_BASELINE_V1_2.md`。
2. 对 top evidence 做 chunk/span 级人工标注；第二名域专家分层复标 20% 和全部模糊项，争议样本仲裁，并记录标注协议、一致性和版本。
3. 固定四基线和 95% bootstrap 置信区间。只有在困难集上有稳定增益才保留 multi-query 或新增融合复杂度。
4. 加 claim→citation span entailment 指标；抽取至少 100 条回答做人审校准，judge 与 responder 分离并记录一致性。
5. 完成真实 Docling layout/table 样本；加入受污染文档、跨租户、失效版本和索引切换回归。
6. 记录 retrieval/answer p50、p95、p99、吞吐和错误率；执行 OpenSearch、TEI、Neo4j 单点故障演练。

建议的 Phase 4 关闭门：ACL 泄漏=0；Citation correctness≥95%；Citation completeness≥90%；no-answer correctness≥95%；Recall@10、MRR@10、NDCG@10 均提供置信区间；任何新增检索臂相对简单基线必须有可复现收益或明确成本收益理由。

## 7. Phase 5 实施计划（15 个工作日）

### 第 1 周：Memory 基础和治理

- 建立 `semantic / episodic / procedural / preference` 四类 Memory schema，包含 tenant、scope、evidence、version、validity、TTL、status 和 provenance/taint。
- PostgreSQL RLS、审计事件、乐观版本和 active/quarantine/revoked 状态机。
- Memory Writer 作为 Harness middleware：只在 run 完成后执行 candidate extraction、证据核验、价值判断、PII/secret 扫描、精确/语义去重、冲突检测、quarantine/activate。
- 支持过期、撤销、用户偏好同意、导出和删除；记忆不能直接授予工具或写权限。
- 测试跨租户隔离、提示注入持久化、冲突事实、TTL、删除传播和 checkpoint 重放不重复写。

### 第 2 周：Context Builder 和 Skills

- 建立每个 Agent 的显式 context view，只提供 contract、当前 task、必要 state、ranked evidence、允许的 memory、允许的 tool schema 和剩余预算。
- 加 token/cost 预算、ranking、固定预算 replace、摘要、tool result offload、PII/secret redaction、provenance/taint 传播。
- 禁止把完整内部消息、其他 Agent 私有状态和未验证 memory 广播给全部 Agent。
- 建立版本化 Skill registry 和 manifest：checksum、tenant/global scope、allowed agents、required tools、risk、测试集和生命周期。
- 交付五个技能：incident-triage、recurring-problem、change-risk、vpn-mfa、major-incident；Skill 只能缩小上下文和能力，不能扩大 Tool Policy。

### 第 3 周：Model Gateway、评测和受控发布

- 统一 provider adapter、结构化输出、timeout/retry、circuit breaker、fallback、tenant/model allowlist、任务风险路由。
- 将 prompt/template/model 版本写入 trace；统一 token、延迟、错误和成本核算。
- 高风险 Reviewer/Action 使用强模型；路由、分类和摘要在通过质量门后才允许小模型。
- 如启用 semantic cache，key 必须包含 tenant、role、policy、prompt、model、memory/index generation；敏感或写相关任务默认不缓存。
- 运行离线回归、故障注入、shadow 和小流量 canary；质量、延迟、成本任一越门自动回退。

## 8. Phase 5 验收门

| 类别 | 必须满足 |
| --- | --- |
| Memory | 跨租户泄漏 0；无证据/注入内容自动激活 0；TTL/撤销/删除传播 100%；重复重放不产生重复 active memory。 |
| Context | 所有模型调用都记录 context manifest 和预算；超 token/cost 预算 0；敏感字段泄漏 0；质量不低于无裁剪基线。 |
| Skills | 未注册/篡改 checksum 拒绝率 100%；Skill 提权或绕过审批 0；五个技能各有正向、负向、对抗样本。 |
| Gateway | tenant/model allowlist 绕过 0；fallback 不改变风险等级或结构化 schema；token/cost 账单误差≤2%；熔断和恢复演练通过。 |
| Multi-Agent | Reviewer 未通过时 Action 可达率 0；并行总预算越界 0；context/memory 跨 Agent 污染 0；checkpoint 恢复不重复副作用。 |
| 发布 | 固定版本评测报告、迁移/回滚手册、监控面板、shadow/canary 证据和验收报告齐全。 |

## 9. 参考基线

- NIST, AI RMF Generative AI Profile: <https://www.nist.gov/publications/artificial-intelligence-risk-management-framework-generative-artificial-intelligence>
- OWASP GenAI, LLM Top 10: <https://genai.owasp.org/llm-top-10/>
- OWASP GenAI, Excessive Agency: <https://genai.owasp.org/llmrisk/llm062025-excessive-agency/>
- OpenSearch, Reciprocal Rank Fusion: <https://docs.opensearch.org/latest/vector-search/ai-search/hybrid-search/rrf/>
- LangChain, Subagents: <https://docs.langchain.com/oss/python/langchain/multi-agent/subagents>
- LangChain, Router: <https://docs.langchain.com/oss/python/langchain/multi-agent/router>
