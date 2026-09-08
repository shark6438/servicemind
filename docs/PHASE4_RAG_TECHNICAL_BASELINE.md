# ServiceMind Phase 4 RAG 技术基线

> **冻结设计基线说明。** 本文保留 2026-09-06 的设计与历史环境信息。当前数据治理、代理评测和最终关闭状态以 `PHASE4_ENGINEERING_CLOSURE_V1_2.md` 为准；Mendeley 已退出生产，Phase 4 为工程关闭且未签发企业前沿质量认证。

状态：主干冻结

冻结日期：2026-09-06

## 1. 不再变更的主链

```text
GLPI KB / Internal Runbook / PagerDuty
  -> structure recovery
  -> structure-aware semantic chunking
  -> searchable child chunks + stored parent chunks
  -> ACL pre-filter
  -> Dense + BM25
  -> RRF
  -> dedup and grouping
  -> cross-encoder reranker
  -> parent expansion
  -> citation builder
  -> context packing
  -> Knowledge Agent
  -> Knowledge Evidence[]
```

Analysis Agent 不直接检索。Reviewer 发现缺证据时返回 Supervisor，由 Supervisor 重新调度 Knowledge/Data Agent。Graph-RAG 是旁路 Evidence Provider，不替代 Text Hybrid RAG。

## 2. 本机约束

- CPU：Intel Core i9-12900H，14 核 20 线程。
- 内存：15.7 GB。
- GPU：RTX 3060 Laptop，约 6 GB VRAM。
- D 盘审计时剩余约 24 GB。

因此 Docker 使用 profile 隔离：

- `core`：GLPI、PostgreSQL、Keycloak。
- `rag`：OpenSearch、ingestion worker、retrieval model service。
- `graph`：Neo4j，只在 Phase 4.2 或 Graph Eval 时启动。
- `eval`：评测工具，不与完整生产栈长期并行。

## 3. 数据存储职责

### PostgreSQL

保存不可丢失的规范数据：

- source registry 和 source sync cursor；
- Document/Section/Block/ParentChunk/ChildChunk；
- KnowledgeProvenance 和 ACL；
- ingestion job、outbox、dead letter；
- parser artifact reference；
- citation span；
- index version 和发布状态。

### OpenSearch 3.8.0

只保存可重建的 Child Chunk 检索投影：

- BM25 text；
- 1024 维 dense vector；
- parent ID/document ID；
- tenant/entity/group/profile/user ACL filter fields；
- version/effective time/is_active；
- language/source/authority/synthetic；
- content hash。

Parent 不建立第二套向量索引。Rerank 后按 `parent_chunk_id` 从 PostgreSQL 批量扩展。

开发环境 OpenSearch 单节点，JVM heap 初始固定 1 GB；索引和 Dashboard 不同时常驻。OpenSearch Security 保持启用，ServiceMind 使用最小权限 service account。

### 原始文件

第一阶段保留在被 Git 忽略的 `data/phase4/raw`，同时记录 manifest 和 SHA-256。接入上传文档后增加 MinIO，保存原始二进制和 Docling lossless JSON；PostgreSQL 只保存 object key 和 hash。

### Neo4j 5.26 LTS

Phase 4.2 使用 `neo4j:5.26.30-community`。Neo4j 是 GLPI/Unified Schema 的可重建关系投影，不是 Source of Truth。

## 4. 结构恢复与切块

### Parser

- PDF/DOCX/PPTX/XLSX/复杂 HTML：Docling 2.x，固定 package 和 model revision。
- GLPI KB HTML：HTML sanitizer + DoclingDocument-compatible adapter。
- Markdown：AST parser，保留 heading/list/table/code block。
- CSV ticket cases：专用 Mendeley adapter，不通过通用文档 parser 猜字段；只用于 reference/eval。

落库格式使用 lossless JSON，不以 Markdown 作为标准中间格式，因为 Markdown 无法无损保留合并单元格。

### 结构模型

```text
Document
  Section
    Block(type=paragraph|table|list|code|image|other)
      ParentChunk
        ChildChunk
```

### Chunk 规则

- 先按 Section/Block 划分，禁止跨标题层级随意拼接。
- Table 作为一个结构块，超长时按行组切分并重复表头。
- List 保持 list item 顺序和父标题。
- Child 初始目标 220～420 tokens，overlap 仅限同一结构块，初始 40 tokens。
- Parent 初始目标 700～1,500 tokens，不做 ANN/BM25 检索。
- Semantic boundary 只在同一结构边界内计算；不得打散 Ticket ID、错误码、命令和表格行。
- 最终阈值由 Gold Set 的 Recall/MRR/NDCG 决定。

## 5. Embedding 与 Reranker

- Dense embedding：`BAAI/bge-m3`，MIT，1024 维，支持中英德葡西等多语言。
- Sparse：OpenSearch BM25，不把 BGE sparse 作为第一版必需项。
- Cross encoder：`BAAI/bge-reranker-v2-m3`，Apache-2.0。
- 本机模式：FP16 GPU；若显存不足，Embedding 常驻 GPU，Reranker 采用 CPU/ONNX 或请求级串行加载。
- 模型下载目录：`data/phase4/models`，必须固定 Hugging Face revision、license 和 artifact hash。
- Embedding model/version 是索引 schema 的一部分，模型变化必须创建新 index，不允许原地混用向量。

## 6. ACL 编译

ACL 是代码生成的 OpenSearch filter，不是 Prompt：

```text
tenant_id == caller.tenant_id OR corpus_scope == global_licensed
AND entity visibility intersects caller entities
AND group restriction is empty OR intersects caller groups
AND profile restriction is empty OR intersects caller profiles
AND user restriction is empty OR contains caller user_id
AND effective_from <= query_time
AND effective_to is null OR effective_to > query_time
AND is_active == true
AND index_version == active alias version
```

ACL 字段随 Child Chunk 冗余保存是为了 pre-filter；权威 ACL 仍在 PostgreSQL。每次结果返回前进行一次确定性 defense-in-depth 校验，但这不能替代 pre-filter。

## 7. Retrieval Pipeline

初始参数：

```text
Dense top_k = 40
BM25 top_k = 40
RRF rank_constant = 60
RRF candidate_k = 30
Reranker final_k = 8
Parent expansion max = 8
Context budget = 8,000 tokens
```

Query Processing 产生结构化结果：

- normalized query；
- identifiers（INC/PRB/CHG/KB、错误码、产品名）；
- entities；
- retrieval intent（procedure/case/policy/fact）；
- requested language；
- source preference，但不能改变 ACL。

精确标识符查询提高 BM25 权重；语义问题保持默认 RRF。RRF 后按 `document_id + parent_chunk_id + normalized content hash` 去重，再 rerank。

Context Packer 约束：

- 先放最高相关 Parent；
- 保留完整 Citation；
- 同源和同 Parent 设上限；
- 保证至少两种来源仅在相关性足够时出现；
- 不为了来源多样性塞入无关文档；
- 超预算时移除最低 rerank score，而不是截断表格中间。

## 8. 数据源具体入口

### GLPI Knowledge Base

生产核心。Connector 必须同步文章、revision、category、entity/group/profile/user targets、FAQ 状态、visible since/until、document attachments 和删除/取消发布事件。

### Internal Runbook/SOP

当前 3 条内置 Runbook 迁移为版本化 Document。后续文档必须走统一 ingestion，不再在 Agent 源码里追加常量。

### PagerDuty

只索引固定 commit 下 `docs/**/*.md`；排除图片、slides、MkDocs 配置和推荐阅读中的外链正文。Authority 为 `external_best_practice`，不能覆盖企业内部 SOP。

### Mendeley V3

官方数据有 66,691 个 issue、257,508 条 change history、30,104 条 utterance；utterance 只覆盖 360 个 issue。

- reference/eval Historical Case Index 只接收这 360 个可关联文本案例；生产 active alias 不接收。
- 其余 66,331 条仅进入结构化分析，不生成伪造的 case text。
- `issue_resolution` 是状态标签，不等于自然语言解决方案。
- 每个案例必须显示 `public_historical_case`、CC-BY-4.0、DOI 和低于内部案例的 authority。

## 9. ITSM-SafetyBench 修正

官方仓库和搜索索引确认项目存在，包含代码、数据、50 Incident、10 Change、6 Problem、20 CI、场景和 274 个测试，恢复为 Phase 4.3 安全评测候选。

当前执行主机访问 GitHub/codeload 时该仓库返回 404，因此本轮不能声称已经下载。其根目录搜索结果也未显示 LICENSE。进入 CI 前必须：

1. 取得仓库快照；
2. 固定 commit；
3. 确认 repo/data license；
4. 跑通上游 274 tests；
5. 将 ServiceNow GlideRecord schema/tool 映射为 ServiceMind Unified ITSM Schema；
6. 保持它为 Eval-only，绝不连接生产 GLPI。

AgentDojo 已下载为可用的通用 Prompt Injection 补充，但不取代 ITSM-SafetyBench 的 ITSM 数据完整性场景。

## 10. 实施切片

### Phase 4.0

- Unified ITSM Schema、KnowledgeProvenance、ACL、SourceManifest。
- PostgreSQL 文档/块/Chunk/ingestion/outbox 表。
- GLPI KB 和 Internal Runbook adapters。
- Parser contract、golden parser fixtures、Mendeley adapter。

### Phase 4.1A

- OpenSearch 3.8.0 Docker profile。
- BGE-M3 model service。
- Child index mapping、versioned alias、bulk indexing、delete propagation。
- Dense/BM25/RRF 和 ACL pre-filter。

### Phase 4.1B

- Reranker、Parent expansion、Citation Builder、Context Packer。
- Knowledge Agent 接入新 RAG subgraph。
- Reviewer `RETRIEVE_MORE` 回归测试。

### Phase 4.2

- Neo4j 5.26 LTS profile。
- GLPI/Seed Pack 图投影和参数化 Graph Retrieval。
- Graph Evidence 与文本 Evidence 合流。

### 原 Phase 4.3 评测计划（最终转入 Phase 7）

- ServiceMind GLPI Gold Set。
- TechQA、Classification、Semantic Similarity 辅助评测。
- EnterpriseOps-Gym ITSM 103-task adapter。
- ITSM-SafetyBench 和 AgentDojo 安全套件。
- CI quality gate 和版本对比报告。

## 11. 第一阶段验收门槛

- ACL 泄漏为 0。
- 无效 Citation 为 0。
- 删除、取消发布和 ACL 变更可传播。
- Parser 对 heading/list/table 顺序保持率 100%（golden fixtures）。
- ServiceMind Gold Recall@5 >= 0.85。
- MRR@10 >= 0.75。
- Grounded citation >= 0.95。
- 无答案 abstention >= 0.95。
- Dense-only、BM25-only、Hybrid、Hybrid+Reranker 都有独立可重复基线。
- Knowledge Agent 不拥有 GLPI 写工具。
- Eval corpus 不在任何生产 index alias 中。

### §11 验收状态（2026-09-08）

下表是治理修正前的小规模验收快照。15 条本地 gold 已被确认存在天花板效应；最终状态对照 `docs/PHASE4_ENGINEERING_CLOSURE_V1_2.md` 和 400 条 TechQA 代理报告。

| 门槛 | 状态 |
| --- | --- |
| ACL 泄漏为 0 | PASS（globex 活体 0 条） |
| 无效 Citation 为 0 | PASS（100/100 evidence 活体引文完整性 0 无效） |
| 删除/取消发布/ACL 变更可传播 | PASS（index lifecycle / repository 测试级） |
| Parser 顺序保持率 100% | PASS（golden fixture） |
| Gold Recall@5 >= 0.85 | PASS（四基线实测 1.0） |
| MRR@10 >= 0.75 | PASS（四基线实测 1.0） |
| Grounded citation >= 0.95 | PASS（实测 1.0） |
| 无答案 abstention >= 0.95 | **PASS** — agent 层语义实测 **1.0**（3/3 无答案查询正确拒答、零编造；2026-09-08 live `deepseek-v4-flash`，真实 acme 语料，`scripts/verify_phase4_abstention_live.py`） |
| 四基线独立可重复 | PASS |
| Knowledge Agent 无 GLPI 写工具 | PASS |
| Eval corpus 不在生产 alias | PASS |

> 说明：评测 harness 的 `abstention_rate` 是检索层指标（unanswerable query 的 ranked 是否为空）。OpenSearch top-k 恒返回 k 条，故该指标对所有 baseline 恒为 0.0、`answered_unanswerable=3`，并非模型答错。§11 门槛指的是 **agent 层语义 abstention**：2026-09-08 已授权并用 live DeepSeek 实测 PASS（见 `docs/PHASE4_ENTERPRISE_ACCEPTANCE.md` §8.3/§5.4）——3/3 无答案查询（秘密索取/PII 索取/未来预测）由语义 judge 判定证据不支持而显式拒答、零编造，5 条可答 control 全部给据作答。
