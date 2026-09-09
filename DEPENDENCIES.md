# 依赖与环境(Dependencies)

本文说明 ServiceMind 的运行依赖、工具链、第三方库与外部基础设施,以及如何用最小外部
依赖把平台跑起来。仓库配套的中英文总览见 [README.md](README.md) 与
[README.zh-CN.md](README.zh-CN.md)。

## 工具链要求

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| Python | `>=3.12,<3.15` | CI 在 3.12 / 3.13 / 3.14 三个版本下测试 |
| uv | `0.11.32`(推荐,CI/Dockerfile 固定) | 依赖解析、锁文件、虚拟环境 |

Dockerfile(`docker/Dockerfile.service`)内固定 `uv==0.11.32`;`.github/workflows/test.yml`
用 `astral-sh/setup-uv` 固定 `0.11.32`。若 `pyproject.toml` 的 `[tool.uv] exclude-newer`
策略失效,通常就是本地 uv 过旧——先升级 uv 再重锁。

安装 uv 见 <https://docs.astral.sh/uv/getting-started/installation/>。

## 依赖源:权威 vs 生成

- **权威源:`pyproject.toml` + `uv.lock`**。所有第三方依赖的版本区间与解析结果都只以
  这两份文件为准;改依赖必须改 `pyproject.toml` 后重新 `uv lock`,不能直接改
  `uv.lock`。
- **生成文件:`requirements.txt`**(pip 可安装,不含 dev 组)。它是 `uv export` 的一次性
  导出,`uv sync` / Docker 并不读取它;仓库内存在只是为了方便不依赖 uv 的
  pip 用户。**请勿手改**。改依赖后用如下命令再生成:

```sh
uv lock --frozen
uv export --frozen --no-dev --no-hashes > requirements.txt
```

- **依赖分组**(见 `pyproject.toml`):默认 `dependencies` 为运行时全集;`dev` 组为
  测试/静态检查工具链;`client` 组为最小客户端 + Streamlit 运行子集
  (`uv sync --frozen --only-group client`)。Dockerfile.app 只装 `client` 组,
  CI 与 Dockerfile.service 装完整运行时。

### uv 供应链冷却策略

`pyproject.toml` 的 `[tool.uv] exclude-newer = "14 days"` 让依赖解析只接受发布 ≥ 14 天
的版本,降低上游仓未知风险。该约束只在足够新的 uv 下生效;依赖刷新策略与覆盖流程见
`.claude/skills/dependency-refresh/SKILL.md`。

## 核心第三方 Python 依赖

下表按用途归类;完整、确切的版本区间以 `pyproject.toml` 为准,完整解析以 `uv.lock` 为准。

| 用途 | 关键包 | 版本区间(节选) |
| --- | --- | --- |
| Agent 框架 | `langgraph`、`langgraph-supervisor`、`langgraph-checkpoint-*` | `~1.2.7` / `~0.0.31` |
| LangChain 生态 | `langchain-core`、`langchain`、`langchain-community` | `~1.4.8` / `~1.3.11` / `~0.4.2` |
| 模型 providers | `langchain-openai`、`langchain-anthropic`、`langchain-google-genai`、`langchain-google-vertexai`、`langchain-groq`、`langchain-aws`、`langchain-ollama` | 见 pyproject |
| DeepSeek | 经 OpenAI 兼容接入 | — |
| Web 服务 | `fastapi`、`uvicorn`、`httpx` | `~0.139.0` / `~0.51.0` / `~0.28.1` |
| 数据校验 | `pydantic`、`pydantic-settings` | `~2.13.4` / `~2.14.2` |
| 数据库 ORM/迁移 | `sqlalchemy[asyncio]`、`alembic` | `~2.0` / `~1.16` |
| 关系库驱动 | `psycopg[binary,pool]`(Postgres 16)、`aiosqlite` | `~3.3.4` / `>=0.22.1` |
| RAG 向量检索 | `opensearch-py[async]`(OpenSearch 3.x) | `>=3,<4` |
| Graph-RAG | `neo4j`(Neo4j 5.26) | `>=5.26,<6` |
| 嵌入/重排 | `sentence-transformers`(进程内兜底)、`tiktoken` | `>=5.1,<6` / `>=0.13.0` |
| 文档解析 | `docling`、`docx2txt`、`pypdf`、`beautifulsoup4` | `>=2.60,<3` 等 |
| 表格/科学 | `pandas`、`numpy`、`pyarrow`、`numexpr` | `~3.0.3` / `~2.5.0` 等 |
| 前端(UI) | `streamlit` | `~1.59.1` |
| 协议/AG-UI | `ag-ui-langgraph` | `~0.0.42` |
| 可观测 | `langfuse`、`langsmith`、`opentelemetry-*` | `~4.12` / `~0.10.2` / `~1.44.0` |

## 外部基础设施

ServiceMind 本体可在无外部基础设施时启动做界面演示(见下节),但 **ITSM 运行端点、RAG、
记忆等能力按需依赖下列服务**。整套本地栈由 `deploy/glpi/compose.yaml` 一键拉起。

| 服务 | 角色 | 默认本地端口 | 说明 |
| --- | --- | --- | --- |
| Postgres 16 | 产品数据库(RLS 租户隔离、审计、运行/记忆状态) | `127.0.0.1:55434` | `SERVICEMIND_DATABASE_URL` 运行时;`SERVICEMIND_MIGRATION_DATABASE_URL` 供 alembic |
| GLPI 11 | ITSM 工单/资产来源 | `127.0.0.1:18088` | ServiceMind 读/写目标;webhook 事件源 |
| MariaDB 11.8 | GLPI 自己的数据库 | 内部 | 由 GLPI 容器专用 |
| Keycloak 26 | OIDC 身份(演示租户/角色) | `127.0.0.1:8090` | realm `servicemind` |
| OpenSearch 3.8 | 混合 RAG 索引(BM25 + 稠密 + RRF) | `127.0.0.1:9200` | 检索侧强制租户 RLS + ACL 过滤 |
| TEI embedding | BGE-M3 嵌入服务(可选加速) | `127.0.0.1:8085` | 为空则走进程内 sentence-transformers |
| TEI reranker | bge-reranker-v2-m3 重排服务(可选加速) | `127.0.0.1:8086` | 为空则走进程内兜底 |
| Neo4j 5.26 | Graph-RAG 投影图 | `127.0.0.1:17687`(bolt) | 可选;`SERVICEMIND_GRAPH_RAG_ENABLED` 关闭时不依赖 |
| DeepSeek API | 默认 LLM provider | 外部 | `.env` 填 `DEEPSEEK_API_KEY` |

其中 Postgres 之外,GLPI、Keycloak、OpenSearch、Neo4j、TEI 等仅在你启用对应功能时才是
硬依赖。RAG 的进程内嵌入/重排路径在
`SERVICEMIND_EMBEDDING_URL` / `SERVICEMIND_RERANKER_URL` 为空时启用,可读取本地预取快照
(`SERVICEMIND_*_CACHE_DIR` + 钉死的 `*_REVISION`)实现**完全离线**;这些快照目录
(`data/phase4/models/…`)已被 gitignore,需运维按
`data/phase4/README.md` 预先放置。

## 最小零外部模式(界面/脚手架演示)

不需要任何 LLM key 与外部服务,即可把服务壳与 ServiceMind Console 跑起来做 UI 演示:

```text
# .env
USE_FAKE_MODEL=true
DATABASE_TYPE=sqlite
SERVICEMIND_DATABASE_URL=sqlite+aiosqlite:///./servicemind.db
```

```sh
uv sync --frozen
uv run python src/run_service.py        # 服务
uv run streamlit run src/streamlit_app.py   # Console
```

注意:此模式只用于验证壳/通用 agent 端点与界面;**ServiceMind 的 ITSM 端点需要 Postgres 16
及 `alembic upgrade head` 建好的产品 schema**(迁移与运行时 RLS 均面向 Postgres),该模式下
不会工作。

## 特性开关

产品能力按特性开关显式启用(`.env.example` 已分组列出,默认关闭)。启用前请阅读对应阶段
文档:

| 开关 | 能力 | 阶段 |
| --- | --- | --- |
| `SERVICEMIND_RAG_ENABLED` / `_REQUIRED` | 混合 RAG 检索 | Phase 4 |
| `SERVICEMIND_GRAPH_RAG_ENABLED` | Graph-RAG(Neo4j) | Phase 4 |
| `SERVICEMIND_MEMORY_ENABLED` / `_VECTOR_ENABLED` | 长期记忆 | Phase 5 |
| `SERVICEMIND_CONTEXT_ENABLED` | 上下文注入 | Phase 5 |
| `SERVICEMIND_SKILLS_ENABLED` | 技能装配 | Phase 5 |
| `SERVICEMIND_MODEL_GATEWAY_AUDIT_ENABLED` | 逐调用审计持久化 | Phase 5 |
| `SERVICEMIND_SEMANTIC_CACHE_ENABLED` | 语义缓存 | Phase 5 |

各开关详见 [`.env.example`](.env.example) 与 [`docs/企业IT服务管理(ITSM)智能体平台.md`](docs/企业IT服务管理(ITSM)智能体平台.md)。

## 上游与许可

本项目依赖与代码衍生自 🧰 [AI Agent Service Toolkit](https://github.com/JoshuaC215/agent-service-toolkit)(MIT,
Copyright (c) 2024 Joshua Carroll);上游归属、历史保留与许可说明见
[README.md — Upstream, history and license](README.md#upstream-history-and-license)(及
中文版[「上游、历史与许可」](README.zh-CN.md#上游历史与许可))。
