# ⚙️ ServiceMind

> **English version: [README.md](README.md)。**

ServiceMind 是一个**企业级 ITSM(IT 服务管理)智能体平台**:它把一条 GLPI 工单转化为一次受治理、可审计、多智能体编排的运行(run)。在此之上叠加了企业级检索能力 —— 基于 OpenSearch 的混合 RAG(含交叉编码器重排)、基于 Neo4j 的结构化 Graph-RAG、受治理的长期记忆、带成本/审计控制的模型网关,以及人工审批工作流 —— 全部构建在 LangGraph agent 服务之上。

本仓库衍生自 🧰 [AI Agent Service Toolkit](https://github.com/JoshuaC215/agent-service-toolkit)(MIT 许可),完整保留了上游 git 历史。详见[「上游、历史与许可」](#上游历史与许可)。

## ServiceMind 在做什么(产品特性)

- **ITSM 工单运行** —— `POST /v1/servicemind/runs` 启动一个与 GLPI 工单绑定的受治理 agent 运行。每次运行都按租户隔离、按角色鉴权,并写入 append-only 审计日志。计划写操作的运行会**暂停等待人工审批**(审批以不可变 action hash 为锚);策略内无法解决的运行会暂停等待人工复核,而不是自行臆断。
- **GLPI 集成** —— 面向 GLPI 11(High-Level API)的读写客户端、签名 Webhook 接入(`/v1/servicemind/webhooks/glpi`,幂等去重)、实体(entity)范围控制与健康探测。可使用 `deploy/glpi/` 内置本地栈运行。
- **混合企业级 RAG(Phase 4)** —— document → parent → child 语义切块;OpenSearch 上 BM25 + 稠密检索经 reciprocal rank fusion(RRF)融合;交叉编码器重排;多查询(rewrite)词法扩展;携带引文(citation)的证据;带 per-document/source 多样性上限的上下文打包器。检索期强制 ACL 与租户行级安全(RLS)。
- **Graph-RAG(Phase 4)** —— 在 Neo4j 中投影 `Ticket → CI → Service → Problem → Change` 关系,回答纯文本检索无法表达的结构化问题(如"还有哪些东西依赖这个服务?")。图谱发现是旁路补充;混合文本检索仍是主通道。
- **受治理长期记忆 + 上下文注入(Phase 5)** —— 声明式、特性开关控制的记忆:置信度阈值自动激活、向量检索、预算内的输入上下文打包器;上线/回滚无需 schema 降级。
- **模型网关治理(Phase 5)** —— 全局与按租户的 provider/model 白名单、逐调用审计持久化、重试/超时/成本上限、语义缓存。
- **技能(Skills)** —— 版本化技能(`skills/`),可为 agent 装配变更风险评估、事件分类、重大事件、重复问题、VPN/MFA 恢复等能力。

同时保留 toolkit 的运行脚手架:一个 FastAPI 服务同时挂载 ServiceMind API 与通用 LangGraph agents、`AgentClient`、以及带语音输入/输出的 Streamlit **ServiceMind Console** 聊天界面。

## 仓库结构

产品与脚手架同处一个 `src/` 树。**产品**代码自包含于 `src/servicemind/`,由 `src/service/service.py` 挂到服务上。

```text
src/
├── servicemind/        # 产品: rag/、graphrag/、domain/、memory/、context/、
│                       #   skills/、model_gateway/、security/、persistence/、
│                       #   orchestration/、harness/、integrations/、observability/
├── service/            # FastAPI 壳 —— 挂载 ServiceMind API + 通用 agent 端点
├── agents/             # 继承自上游的 LangGraph agents(chatbot、research-assistant、…)
├── core/               # 配置 + 模型查找(共享)
├── schema/             # 协议 + 模型名 schema
├── client/             # AgentClient(可基于 agent 服务构建其它应用)
├── voice/              # 聊天界面 STT/TTS providers
├── streamlit_app.py    # ServiceMind Console(聊天界面)
└── run_service.py      # 入口: uvicorn "service:app"
migrations/             # Alembic 迁移 0001–0010(产品 schema,Postgres)
deploy/glpi/            # 本地 GLPI + MariaDB + Postgres + Keycloak + OpenSearch +
                        #   Neo4j + TEI embedding/reranker 栈(docker compose)
evaluation/             # Gold 集、检索/路由/验收评测 harness 与报告
skills/                 # 可为 agent 装配的版本化技能
docker/                 # Dockerfiles(service/app)、附加 compose 文件
docs/                   # 各阶段验收文档 + 企业级主规格
scripts/                # 各阶段 seed/verify/ingest/evaluate 脚本
```

服务启动链路(`src/service/service.py` → `src/run_service.py`)与根级 `compose.yaml`
仍依赖继承自上游的脚手架层(`src/agents|core|memory|schema|client|voice` 与
`streamlit_app.py`),因此这些层是**承重**的运行时基础设施,不是"产品代码"——请勿当作
死代码删除。

## 架构速览

<img src="media/agent_architecture.png" width="700" alt="ServiceMind 架构图">

一条 GLPI 工单事件(或一次 `runs` 请求)进入 **service 壳**;ServiceMind **编排运行时**
把该运行规划为多步 LangGraph 图。检索层为规划器供料:**混合 RAG**(OpenSearch + 重排,
租户 RLS + ACL 过滤)与 **Graph-RAG**(Neo4j,可选)。写操作仅在审批人解决待决 action 后
才回写 **GLPI**。每一步都在 `set_config` 钉死的租户会话下事务于 **Postgres**,写入
**append-only 审计**;模型调用一律经由**模型网关**(白名单、成本上限、审计)。详见
[`docs/PHASE5_FINAL_ARCHITECTURE_AND_ACCEPTANCE.md`](docs/PHASE5_FINAL_ARCHITECTURE_AND_ACCEPTANCE.md)
与 [`docs/企业IT服务管理(ITSM)智能体平台.md`](docs/企业IT服务管理(ITSM)智能体平台.md)。

## 快速开始

### 前置条件

- Python ≥ 3.12、< 3.15,以及 [uv](https://docs.astral.sh/uv/)(CI 与 Dockerfile 均固定 uv
  版本;见 [DEPENDENCIES.md](DEPENDENCIES.md))。
- `.env` 中至少一个 LLM provider key(ServiceMind 默认 DeepSeek;toolkit 的
  OpenAI/Anthropic 等 provider 仍可用)。`USE_FAKE_MODEL=true` 可去掉该要求,用于零外部依赖演示。

### A. Docker Compose 全栈

根级 [compose.yaml](compose.yaml) 启动 Postgres、agent 服务与 Streamlit 应用:

```sh
cp .env.example .env        # 然后填入 provider key 与 SERVICEMIND_DATABASE_URL
docker compose watch        # 或: docker compose up --build
```

- ServiceMind Console:<http://localhost:8501>
- Agent 服务 + OpenAPI 文档:<http://localhost:8080/redoc>

### B. 企业级 GLPI 栈(使用 ITSM 功能推荐)

ServiceMind API 需要 Postgres 16;完整事件工作流还需要 GLPI + Keycloak + OpenSearch +
Neo4j + TEI。`deploy/glpi/compose.yaml` 可一键拉起整套本地栈;命令与端口见
[deploy/glpi/README.md](deploy/glpi/README.md)。

### C. 免 Docker 手动运行

```sh
uv sync --frozen

# .env: 填 provider key + SERVICEMIND_DATABASE_URL(建议 Postgres)。
# 仅做无 LLM key、无基础设施的界面演示:
#   USE_FAKE_MODEL=true
#   SERVICEMIND_DATABASE_URL=sqlite+aiosqlite:///./servicemind.db
cp .env.example .env

# 创建/扩展产品 schema(迁移面向 Postgres 16):
uv run alembic upgrade head

# 终端 1 —— agent 服务
uv run python src/run_service.py

# 终端 2 —— ServiceMind Console
uv run streamlit run src/streamlit_app.py
```

## 配置

全部通过 [`.env.example`](.env.example) 的环境变量驱动。主要分组:

- **模型 providers** —— `DEEPSEEK_API_KEY`(默认 provider),以及 toolkit 的
  OpenAI/Anthropic/Google/Groq/AWS/Ollama/OpenRouter/compatible keys。
- **运行时** —— `HOST`、`PORT`、`AUTH_SECRET`(HTTP bearer)、`MODE=dev`(uvicorn reload)、
  脚手架 checkpoint 的 `DATABASE_TYPE`/`POSTGRES_*`。
- **ServiceMind 数据库** —— `SERVICEMIND_DATABASE_URL`(运行时,RLS 租户隔离)与
  `SERVICEMIND_MIGRATION_DATABASE_URL`(alembic)。ITSM 端点需要 Postgres 16;sqlite 仅用于
  UI 演示时启动壳。
- **GLPI** —— `GLPI_BASE_URL`、`GLPI_API_VERSION`、凭据、entity/profile 默认值。
- **Phase 4 RAG / Graph-RAG** —— OpenSearch URL + 凭据、embedding/reranker 模型与钉死
  revision、TEI 端点、RAG 特性开关、Neo4j URI/凭据。
- **Phase 5 治理** —— `SERVICEMIND_{MEMORY,CONTEXT,SKILLS,MODEL_GATEWAY_AUDIT,
  SEMANTIC_CACHE}_ENABLED` 特性开关、模型白名单、成本/超时上限。

特性开关默认关闭;在目标环境启用前请先阅读 Phase 4/5 文档。

## HTTP API 速查

| 端点 | 用途 |
| --- | --- |
| `GET /health`、`GET /info` | 存活探针;可用 agents/models |
| `POST /{agent_id}/invoke`、`/stream` | 通用 toolkit agents(继承) |
| `GET/POST /threads`、`POST /history`、`POST /feedback` | 会话状态 + 反馈 |
| `POST /v1/servicemind/runs` | 启动受治理的 GLPI 工单运行(角色 `analyst`) |
| `GET /v1/servicemind/runs/{id}` | 运行状态 + 待决 action intent |
| `POST /v1/servicemind/runs/{id}/approval` | 批准/拒绝写操作(角色 `approver`) |
| `POST /v1/servicemind/runs/{id}/review-resolution` | 解决复核升级 |
| `POST /v1/servicemind/runs/{id}:cancel` | 取消 pending/awaiting 运行 |
| `GET /v1/servicemind/runs/{id}/events` | 运行事件 SSE 流 |
| `POST /v1/servicemind/webhooks/glpi` | 接入签名 GLPI webhook(幂等) |
| `GET /v1/servicemind/glpi/health` | GLPI 连通性/租户探针 |
| AG-UI 端点 | 连接任意 AG-UI 兼容前端 |

## 测试与 CI

测试套件是契约,提交前保持绿色:

```sh
uv run ruff format --check .
uv run ruff check .
uv run pyrefly check
uv run pytest                                   # 全量离线套件
uv run pytest tests/integration --run-docker    # docker 门控集成
```

`.github/workflows/test.yml` 运行 ruff、pyrefly、pytest(Python 3.12/3.13/3.14)、Markdown
lint 以及 docker 集成 job。提交/撰写约定见 [CLAUDE.md](CLAUDE.md)。

## 文档索引

- 企业级主规格(中文):[`docs/企业IT服务管理(ITSM)智能体平台.md`](docs/企业IT服务管理(ITSM)智能体平台.md)
- 架构图景:[`docs/PHASE3_CURRENT_ARCHITECTURE_MAP.md`](docs/PHASE3_CURRENT_ARCHITECTURE_MAP.md)
- Phase 4 RAG 技术基线:[`docs/PHASE4_RAG_TECHNICAL_BASELINE.md`](docs/PHASE4_RAG_TECHNICAL_BASELINE.md)
- Phase 5 架构与验收:[`docs/PHASE5_FINAL_ARCHITECTURE_AND_ACCEPTANCE.md`](docs/PHASE5_FINAL_ARCHITECTURE_AND_ACCEPTANCE.md)
- 各阶段验收报告:`docs/PHASE*_ACCEPTANCE.md`;实时报告见 `evaluation/reports/`
- 本地部署说明:[`LOCAL_DEPLOYMENT.md`](LOCAL_DEPLOYMENT.md)
- GLPI 栈:[`deploy/glpi/README.md`](deploy/glpi/README.md)
- 依赖与环境:[`DEPENDENCIES.md`](DEPENDENCIES.md)

## 上游、历史与许可

本项目衍生自 🧰 [AI Agent Service Toolkit](https://github.com/JoshuaC215/agent-service-toolkit)
(`JoshuaC215/agent-service-toolkit`,MIT),即 Joshua Carroll 及其贡献者构建的完整
LangGraph + FastAPI + Streamlit agent 服务工具箱。二者关系如实保留:

- **完整保留 git 历史** —— 本仓库不是 squash 重写。它始于上游谱系(约 255 条
  Joshua Carroll 与 toolkit 其它贡献者的提交),随后是 **Shark6438** 的 ServiceMind 提交
  (产品工作起始于 2026 年 9 月,上游基线 `fe3b2dc` 之上)。因此 GitHub 上的贡献者列表
  同时显示 ServiceMind 维护者与上游作者 —— 本仓库正是从他们的代码起步的。
- **LICENSE 与版权声明原样保留** —— [LICENSE](LICENSE) 为上游 MIT License,
  Copyright (c) 2024 Joshua Carroll;ServiceMind 的增量同样以 MIT 分发。
- **脚手架代码保持上游归属** —— 继承自 toolkit 的层(`src/agents`、`src/core`、
  `src/schema`、`src/client`、`src/voice`、`src/streamlit_app.py`、docker 文件与通用
  chat 端点)仍是其原作者的成果。

ServiceMind 自身的增量(`src/servicemind` 产品树、migrations、GLPI 栈、评测 harness、
skills、ServiceMind 阶段文档)由 **Shark6438** 维护。感谢上游 toolkit 及其作者奠定的基础。

## 贡献

欢迎提交 PR。请遵循 [CLAUDE.md](CLAUDE.md) 约定,保持测试套件绿色,并在改动中保留上游
归属。
