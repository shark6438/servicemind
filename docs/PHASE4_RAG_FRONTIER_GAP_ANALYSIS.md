# Phase 4 RAG — 企业前沿实践 × 现状差距分析（2026-09-08）

> 方法：两路独立联网调研（A=检索/排序/索引前沿；B=agentic/eval/运维前沿，均带来源引用）+ 一路只读全代码盘点（C，逐条 file:line）→ 本人抽查复核 4 个关键论断（rewritten_queries 消费点、ABSTAIN 决策、GraphRAG 默认、RETRIEVE_MORE 轮数）后合成。
> 前沿报告中的厂商/二手数字已标注可信度；本文件不把未验证的数字当结论。

**一句话结论**：当前 RAG 已经覆盖前沿的“骨干”（结构感知切块+parent-child、dense+BM25+RRF+k=60、cross-encoder rerank、确定性 citation gate fail-closed、版本化 alias 蓝绿、RLS 双写、RLS-only parent 扩展、committed gold + 四基线 eval），**没有致命空洞**。差距集中在四类：① 已有代码未接线/未执行（multi-query 死代码、GraphRAG 默认关且未测、docling 未接入）；② 语义层深度（citation 门只查“存在/绑定”未查“逐 claim 蕴含”、无检索时注入再打分、运行时无显式 abstention 决策）；③ 评测/运维纵深（无答案层 scorecard、无 CI 门、judge 无协议固定、无漂移/缓存/金标 chunk 标注）；④ 可选效率杠杆（量化、contextual retrieval、加权融合——均为“先测再上”）。

## 0. 范围界定：项目里有两处 RAG，本分析只审计真实项目代码

| 位置 | 性质 | 技术栈 | 可达性 |
| --- | --- | --- | --- |
| `src/servicemind/rag/`（+ `src/servicemind/graphrag/` 旁路） | **项目真实 RAG（本文档审计对象）** | BGE-M3（1024-dim）+ BM25 + RRF + bge-reranker + citation/ACL/RLS | `servicemind` 全家引用 |
| `src/agents/rag_assistant.py`、`src/agents/knowledge_base_agent.py`、`src/agents/tools.py::database_search` | **脚手架样例**（langgraph agent-service-toolkit demo：AcmeBot 员工手册 / Bedrock Knowledge Bases） | `Chroma(persist_directory="./chroma_db")` + `OpenAIEmbeddings`；`AmazonKnowledgeBasesRetriever(AWS_KB_ID)` | 仅 `run_agent.py` / `service/service.py` / `service/agui.py`（样例 app-tier）引用 |

- 实证：`src/servicemind/` 不 import `rag_assistant/knowledge_base_agent/database_search`（唯一撞到脚手架的 `glpi_mvp.py → agents.lazy_agent.LazyLoadingAgent` 只是懒加载包装，非 RAG）。
- 含义 1：样例 RAG 进不了项目运行面，不应计入企业 RAG 能力或差距；本矩阵对其零引用。
- 含义 2：docker_e2e 两条失败（chatbot/fake-model/weather，`:80` `USE_FAKE_MODEL=true` service）实为**样例 app-tier** 测试，与项目 RAG 无关。
- 治理提示：样例 RAG 依赖 `OPENAI_API_KEY`/`AWS_KB_ID` 并写本地 `./chroma_db`，若未来在 GLPI/生产部署意外暴露该样例 agent 面，会绕过企业 ACL/citation/租户隔离。建议从仓库删除或隔离，使其不可能被注册进项目运行入口。

---

## 1. 差距矩阵（前沿 → 现状 → 判定）

| 层 | 企业前沿实践（2025–26） | 当前现状 | 差距判定 | 优先级 |
| --- | --- | --- | --- | --- |
| **查询理解** | Multi-query fan-out 生成→执行→RRF 融合；HyDE 仅在词汇鸿沟场景（有知识泄漏证据，ACL2025） | `query_processor` 已产出 ≤3 条 `rewritten_queries`，注入检查完好，但 **service/opensearch 零消费**，检索只用 `normalized_query`（[query.py](src/servicemind/rag/query.py)、[opensearch.py](src/servicemind/rag/opensearch.py)） | **死代码→真功能**：接线 fan-out + eval 权衡；HyDE 不建议 | **P0** |
| **检索通道** | 第三臂 learned-sparse（SPLADE/OpenSearch neural sparse v3）；加权/归一融合（WAS 报告 +1.6~2.7 nDCG@10 over RRF，单研究） | dense+BM25+RRF(k=60)；eval 显示四基线 Recall@5=MRR@10=1.0（本语料已饱和） | 融合本身够用；SPLADE 仅在 jargon/ID 召回缺口时上 | P2 |
| **索引/切块** | Contextual retrieval（Anthropic：35%/49%/67%，厂商口径）；parent-child 已是标配 | parent-child 已实现且 evidence 走 parent 扩展；检索跑在 child 上，child 无上下文前缀、无 overlap | 需先量化：在自有语料测 contextual-prefix 相对 parent 扩展的边际收益再决定 | P1（先测） |
| **索引/切块** | Late chunking | BGE-M3 独立评测下 late chunking **降级**（0.225→0.067） | 跳过（明确不兼容风险） | — |
| **索引/切块** | RAPTOR 递归摘要 | 无 | 长上下文 LLM 兴起后 DOS-RAG 基线即可打平；维护成本高 | 跳过 |
| **索引/切块** | PDF/Office → docling lossless | `parse_file` 定义于 [parsing.py](src/servicemind/rag/parsing.py) 但 ingest 只按内容前缀走 md/html（[service.py](src/servicemind/rag/service.py)），**docling 路径无调用方** | 企业接入真实 PDF/Office 文档的硬缺口 | **P0/P1** |
| **向量效率** | int8/binary + 全精度 rescore 两阶段（int8≈98–99% recall@10，4× 压缩） | 纯 float32 1024-dim HNSW；2.7k children 规模无压力 | 有 reranker 兜底吸收损失；语料增长到 ~10⁶ 再触发；OpenSearch PQ 可用 | P2（规模触发） |
| **Rerank** | Cross-encoder 仍是默认（Redis 2026 共识）；LLM listwise（RankGPT）只对 hardest 查询做级联 | bge-reranker-v2-m3 已接入且 switchable | 保持；级联可选项 | P2 |
| **上下文** | Fixed-budget “replace 不 expand”（SEAL-RAG，HotpotQA +3–13pp）；lost-in-middle 缓解 | Context packer 有 per-doc/per-source 上限 + 8000 token 预算；但 retrieve-more 是**追加**型，且仅 1 轮平面重检（[reviewer.py](src/servicemind/agents/reviewer.py) round<1） | 追加型循环 = CRAG/Self-RAG 已知缺陷形态；改固定预算替换语义 | P1 |
| **查询路由** | Adaptive route-to-depth（direct/单发/agentic）+ 硬迭代上限（≤3） | fast_knowledge/fast_data **绕过 reviewer**；supervisor 有 max_replans=2；无查询难度路由 | 成本/延迟控制杠杆缺失（agentic 恒开 ≈3.7× 成本） | P1 |
| **Grounding** | Citation **faithfulness**（逐 claim NLI 蕴含，非“存在”）；ICTIR 报告 ≤57% citation 无 faithfulness | 确定性门验证 digest/锚定/绑定——**fail-closed 且强**，但查的是“citation 是否合法绑定”，不是“该 claim 是否被该 span 蕴含”；语义 judge 判 claim-support 但非 span 级、无独立 NLI | 深度缺口：加 HHEM-2.1+MiniCheck 双 NLI 廉价预闸，升到 span 级 | **P1** |
| **注入防御** | Retrieval-time top-k 再打分 + 来源信任分级（RAG-Poison：34–71% 成功率，二手）；XML untrusted 包裹 | query 注入 tripwire + 语义 judge `prompt_injection_detected` 都在，但**无检索后 prompt 前再打分、无来源信任分级** | 对多租户+多 connector ITSM 是真实缺口（doc store 是供应链） | **P1** |
| **Abstention** | 无答案正确拒答作为一等指标打分 | 无 ABSTAIN 决策/无显式“无法作答”终态；确定性门保证 fail-closed（知识不足即 RETRIEVE_MORE/ESCALATE，绝不 PASS），语义拒答仅以 live 脚本实测 1.0 | 语义缺口（平台是 action 审批制：abstention=“无支撑不动作”已成立；缺“面向用户的显式无可答”与 reviewer 直接消费 unsupported_claim_ids） | **P0（语义澄清）/P1** |
| **Agentic** | 失败分型升级（检索缺口→改写查询 vs 合成失败→重生成）；SEAL 替换语义 | planner 层有 DAG 分解（plan 级），但 retrieve-more 是**文本拼接重检**、无分型 | 区分“缺证据→RETRIEVE_MORE 改写”与“合成失败→REPLAN”，避免通用循环 | P1 |
| **GraphRAG** | 2025 共识：full GraphRAG 部署多为 eval 驱动、索引贵/难增量；global 需求倾向 LazyGraphRAG | 结构性 KG 检索已实现（param MERGE、tenant-pinned、BFS）但**默认 OFF 且 eval 未挂 graph_store**；`NodeKind.RUNBOOK` 声明但 projection 不产生 RUNBOOK 节点、CHANGE/RUNBOOK 锚点无 finding（基线 §4.2 本意是补 Runbook/Change 证据 demo） | Phase 4.2 完整性缺口：补 RUNBOOK 投影 + 开启 + 挂到 eval 实测 | **P0（完整性）** |
| **评测** | 三层 scorecard + 版本化金标 + CI 回归门(1–3%)；chunk 级标注 + hard negative + no-answer；judge 协议固定（模型/版本/温度0/kappa 校准） | 检索层 Recall@k/MRR/NDCG + 4 基线 + committed gold 强；但**无答案层 scorecard（faithfulness/claim-citation/abstention-correctness）作 CI 指标、gold 是 doc 级非 chunk 级、无 hard negative、judge 未固定版本无 kappa** | 评测纵深缺口（多数系统的共性缺失项） | **P1** |
| **在线评测** | shadow→canary→trace-linked 反馈回灌金标 | 无在线/回灌闭环 | | P2 |
| **运维** | index manifest + **embedding 漂移监测** + 升级当 release；tenant-scoped 语义缓存 | 代际 fingerprint + 蓝绿 alias + reconcile 强；**无漂移监测、无按 embedding 版本定期的金标 recall 跑批、无语义缓存** | 漂移监测低成本补上 | P1（漂移）/P2（缓存） |
| **Parser** | （并入索引/切块行） | 见上 docling 未接线 | | P0 |

---

## 2. 采用路线（按 ROI/成本排序）

### P0 — 立即可做，低成本高确定性（多为“已有代码只差接线”）
1. **Multi-query fan-out 接线或显式废弃**：把 `rewritten_queries` 实际执行并 RRF 融合（query 处理器 + 注入门已就绪，只差 opensearch/service 消费），用现有 gold 基线测 Recall/MRR 边际；无增益则删除死字段。预计半天 + 一次 eval。
2. **GraphRAG Phase 4.2 收口**：补 `RUNBOOK` 节点投影 + CHANGE/RUNBOOK 锚点路径（基线 §4.2 明确要 Change/Runbook 证据），在 `.env` 开启并让 eval 挂 graph_store，跑一次 real GLPI demo（VPN/MFA → Authentication Service → Problem/Change）实测边际。Neo4j 已在跑、代码已 90%，差最后一环 + 测量。
3. **运行时 abstention 语义澄清**：让 reviewer 直接消费 `unsupported_claim_ids`（现在只透传到 `ReviewResult.unsupported_claims`，不参与决策）→ 决定 REPLAN vs 显式“证据不足不可动作”终态；明确 action 审批制的 abstention 定义并写进门禁。
4. **docling/PDF 接入**：`EnterpriseRAG.ingest` 按文件类型路由到 `parse_file`，补 PDF/DOCX golden 用例（企业真实附件是 md/html/csv 之外的常态）。

### P1 — 语义深度 + 评测纵深（中等成本，先立测量）
5. **Citation faithfulness 升 span 级**：HHEM-2.1-Open + MiniCheck-Flan-T5（双 NLI min-ensemble）作廉价 fail-closed 预闸，放在贵 LLM judge 前；把“该 claim 是否被引用 span 蕴含”单独计为指标（≠ citation 存在）。<600MB、近实时。
6. **Retrieval-time 注入再打分 + 来源信任分级**：只对进 prompt 的 top-k chunk 打分（5–20 chunk/查询，便宜）；低信任来源不可达高信任查询——多租户 ITSM 最该补的一层。
7. **Judge 协议固定**：pin 模型/版本 + 温度 0 + 温度取 0；judge 换版当作测试集变更；跑小样本 human-kappa 校准；abstention-correctness 作为一等指标。
8. **硬迭代/预算上限入配置**：context 固定预算 + “replace 不 expand”替换语义；给 supervisor 加查询难度路由开关（direct/单发/agentic）。
9. **金标升 chunk 级 + hard negative + no-answer + CI 门**：doc 级 relevant 不够细；补 chunk 标注与 hard negative；CI 在 Recall/faithfulness/abstention-correctness 超 1–3% 容差时 fail。

### P1′ — 先测再上（自有语料，别用厂商数字直接决定）
10. **Contextual retrieval 边际测量**：parent-child 已把父上下文带进 evidence；测 contextual-prefix 对 child 级 BM25/dense 的增量是否值回一次性 LLM 重索引成本（本机 DeepSeek 或小模型）。
11. **加权/归一融合**：仅在已有 label 的查询集上对比 RRF(k=60) vs WAS；无 label 保持 RRF。
12. **Embedding 漂移监测 + 定期金标 recall**：索引代际 fingerprint 已有；补 query-encoder 与索引一致性 fast-fail 与按 embedding 版本的周跑召回。

### P2 — 延迟/按需/规模触发
13. int8/PQ 两阶段量化（语料≥~10⁵ 时）；14. tenant-scoped 语义缓存（查询量大时）；15. LLM listwise rerank 级联（仅 hardest 查询）；16. SPLADE 第三臂（jargon/ID 召回缺口时）；17. 在线 canary + trace 回灌（有生产流量时）。

### 明确跳过（附原因）
- **Full Microsoft GraphRAG**：索引贵、增量难、多租户 ACL 在图上添治理摩擦；LazyGraphRAG 若未来要 global 再评估。
- **ColBERTv2**：存储 ~5–10×、OpenSearch 不原生；已有 cross-encoder 吸收大半收益。
- **RAPTOR**：长上下文 LLM + DOS-RAG 基线已打平；维护成本高。
- **HyDE**：ACL2025 知识泄漏（≤83.5% FEVER 泄漏金标）；私域语料收益存疑。
- **Late chunking（BGE-M3）**：独立评测明确降级。
- **CRAG/Self-RAG 恒开循环**：context 稀释缺陷；要的是固定预算替换语义（见 P1-8）。

---

## 3. 可信度声明
- 前沿数字仅来自联网检索与二手转述处均已标注：Contextual retrieval 35/49/67% 与成本为 **Anthropic 厂商口径**（主帖本环境抓取被墙，多来源一致引用）；SPLADE/OpenSearch neural sparse、Jina late-chunking、WAS、RAG-Poison、语义缓存命中率等为**厂商/单研究/二手**，未独立复现；SEAL-RAG(HotpotQA +3–13pp)、judge-bias(arXiv 2504.09946)等有主文献可查。
- 现状侧全部来自只读代码盘点（附 file:line），本人抽查 4 项关键论断全部成立。
- 任何“采纳”都建议先落 P1′ 的 measurement，不以本文件数字作为 SLA。
