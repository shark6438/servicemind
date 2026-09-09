# ⚙️ ServiceMind

[![build status](https://github.com/shark6438/servicemind/actions/workflows/test.yml/badge.svg)](https://github.com/shark6438/servicemind/actions/workflows/test.yml)
[![Python Version](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fshark6438%2Fservicemind%2Fmain%2Fpyproject.toml)](https://github.com/shark6438/servicemind/blob/main/pyproject.toml)
[![GitHub License](https://img.shields.io/github/license/shark6438/servicemind)](https://github.com/shark6438/servicemind/blob/main/LICENSE)

> **简体中文说明见 [README.zh-CN.md](README.zh-CN.md)。**

ServiceMind is an **enterprise ITSM agent platform**: an orchestrator that turns a GLPI
ticket into a governed, auditable, multi-agent run. It layers enterprise retrieval
(hybrid RAG over OpenSearch with cross-encoder reranking, plus structural Graph-RAG over
Neo4j), governed long-term memory, a model gateway with cost/audit control, and human
approval workflows on top of a LangGraph agent service.

The repo is a derivative of the 🧰 [AI Agent Service Toolkit](https://github.com/JoshuaC215/agent-service-toolkit)
(MIT). The full upstream git history is preserved — see [Upstream, history and license](#upstream-history-and-license).

## What ServiceMind adds

- **ITSM ticket runs** — `POST /v1/servicemind/runs` starts a governed agent run bound to
  a GLPI ticket. Each run is tenant-scoped, role-checked, and recorded in an append-only
  audit log. Runs that plan to change state pause for **human approval** keyed to an
  immutable action hash; runs that cannot resolve under policy pause for human review
  instead of guessing.
- **GLPI integration** — read/action client for GLPI 11 (High-Level API), signed webhook
  ingestion (`/v1/servicemind/webhooks/glpi`) with idempotent dedupe, entity scoping, and a
  health probe. Run it against the bundled local stack in `deploy/glpi/`.
- **Hybrid enterprise RAG (Phase 4)** — document → parent → child chunking with a semantic
  chunker, BM25 + dense retrieval fused by reciprocal rank fusion on OpenSearch, a
  cross-encoder rerank pass, multi-query lexical fan-out, citation-carrying evidence, and a
  context packer with per-document/source diversity ceilings. ACLs and tenant row-level
  security are enforced at query time.
- **Graph-RAG (Phase 4)** — a Neo4j projection of `Ticket → CI → Service → Problem →
  Change` relationships answers structural queries (e.g. "what else depends on this
  service?") that text retrieval alone cannot express. Graph findings are a side channel;
  hybrid text retrieval stays the primary channel.
- **Governed long-term memory + context (Phase 5)** — declarative, feature-gated memory
  with confidence-thresholded auto-activation, vector retrieval, and an input context
  packer under a token budget, so rollout never needs a schema downgrade.
- **Model gateway governance (Phase 5)** — provider/model allowlists (global and per-tenant),
  per-call audit persistence, retry/timeout/cost ceilings, and a semantic cache.
- **Skills** — versioned skills (`skills/`) the agents can be equipped with for change
  risk, incident triage, major incidents, recurring problems, and VPN/MFA recovery.

It also keeps the toolkit's runtime scaffold: a FastAPI service that serves both the
ServiceMind API and the generic LangGraph agents, an `AgentClient`, and a Streamlit
"ServiceMind Console" chat UI with voice input/output.

## Repository layout

Product and scaffold live under one `src/` tree. The **product** is self-contained in
`src/servicemind/` and is mounted onto the service in `src/service/service.py`.

```text
src/
├── servicemind/        # The product: rag/, graphrag/, domain/, memory/, context/,
│                       #   skills/, model_gateway/, security/, persistence/,
│                       #   orchestration/, harness/, integrations/, observability/
├── service/            # FastAPI shell — mounts ServiceMind API + generic agent endpoints
├── agents/             # Inherited LangGraph agents (chatbot, research-assistant, …)
├── core/               # Settings + model lookup (shared)
├── schema/             # Protocol + model-name schema
├── client/             # AgentClient (build other apps on the agent service)
├── voice/              # STT/TTS providers for the chat UI
├── streamlit_app.py    # ServiceMind Console (chat UI)
└── run_service.py      # Entry point: uvicorn "service:app"
migrations/             # Alembic migrations 0001–0010 (product schema, Postgres)
deploy/glpi/            # Local GLPI + MariaDB + Postgres + Keycloak + OpenSearch +
                        #   Neo4j + TEI embedding/reranker stack (docker compose)
evaluation/             # Gold sets, retrieval/route/acceptance eval harness + reports
skills/                 # Versioned skills the agents can be equipped with
docker/                 # Dockerfiles (service / app), add-on compose files
docs/                   # Phase acceptance docs + the enterprise spec
scripts/                # Phase seed / verify / ingest / evaluate scripts
```

The service boot path (`src/service/service.py` → `src/run_service.py`) and the root
`compose.yaml` still depend on the inherited scaffold layers (`src/agents|core|memory|
schema|client|voice` + `streamlit_app.py`), so those layers are load-bearing and not
"product" code — treat them as runtime infrastructure.

## Architecture at a glance

<img src="media/agent_architecture.png" width="700" alt="ServiceMind architecture diagram">

A GLPI ticket event (or a `runs` request) enters the **service shell**; the ServiceMind
**orchestration runtime** plans the run as a multi-step LangGraph graph. Retrieval layers
feed the planner: **hybrid RAG** (OpenSearch + rerank, gated by tenant RLS + ACLs) and
**Graph-RAG** (Neo4j, optional). Writes go back through **GLPI** only after an approver
resolves the pending action. Every step transacts against **Postgres** under
`set_config`-pinned tenant sessions, is recorded to the **append-only audit**, and the
model calls go through the **model gateway** (allowlists, cost ceilings, audit). See
[`docs/PHASE5_FINAL_ARCHITECTURE_AND_ACCEPTANCE.md`](docs/PHASE5_FINAL_ARCHITECTURE_AND_ACCEPTANCE.md)
and [`docs/企业IT服务管理(ITSM)智能体平台.md`](docs/企业IT服务管理(ITSM)智能体平台.md).

## Quickstart

### Prerequisites

- Python ≥ 3.12, < 3.15, and [uv](https://docs.astral.sh/uv/) (this repo pins uv in CI and
  in the Dockerfiles; see [DEPENDENCIES.md](DEPENDENCIES.md)).
- At least one LLM provider key in `.env` (DeepSeek is the ServiceMind default; the
  toolkit's OpenAI/Anthropic/etc. providers remain available). `USE_FAKE_MODEL=true`
  removes that requirement for a zero-external demo.

### A. Full stack with Docker Compose

The root [compose.yaml](compose.yaml) starts Postgres, the agent service, and the
Streamlit app:

```sh
cp .env.example .env        # then add a provider API key and SERVICEMIND_DATABASE_URL
docker compose watch        # or: docker compose up --build
```

- ServiceMind Console: <http://localhost:8501>
- Agent service + OpenAPI docs: <http://localhost:8080/redoc>

### B. Enterprise GLPI stack (recommended for ITSM features)

The ServiceMind API needs Postgres 16, and full incident workflows need GLPI + Keycloak +
OpenSearch + Neo4j + TEI. `deploy/glpi/compose.yaml` provisions the whole local stack;
see [deploy/glpi/README.md](deploy/glpi/README.md) for commands and ports.

### C. Manual run without Docker

```sh
uv sync --frozen

# .env: set a provider key + SERVICEMIND_DATABASE_URL (Postgres recommended).
# For a UI-only demo with no LLM key and no infra:
#   USE_FAKE_MODEL=true
#   SERVICEMIND_DATABASE_URL=sqlite+aiosqlite:///./servicemind.db
cp .env.example .env

# Create/extend the product schema (migrations target Postgres 16):
uv run alembic upgrade head

# Shell 1 — agent service
uv run python src/run_service.py

# Shell 2 — ServiceMind Console
uv run streamlit run src/streamlit_app.py
```

## Configuration

Everything is environment-driven through [`.env.example`](.env.example). Main groups:

- **Providers** — `DEEPSEEK_API_KEY` (default provider), plus the toolkit's
  OpenAI/Anthropic/Google/Groq/AWS/Ollama/OpenRouter/compatible keys.
- **Runtime** — `HOST`, `PORT`, `AUTH_SECRET` (HTTP bearer), `MODE=dev` (uvicorn reload),
  `DATABASE_TYPE`/`POSTGRES_*` for the scaffold checkpointer.
- **ServiceMind database** — `SERVICEMIND_DATABASE_URL` (runtime, RLS-tenant-scoped) and
  `SERVICEMIND_MIGRATION_DATABASE_URL` (alembic). Postgres 16 is required for the ITSM
  endpoints; sqlite boots the shell for the UI demo only.
- **GLPI** — `GLPI_BASE_URL`, `GLPI_API_VERSION`, credentials, entity/profile defaults.
- **Phase 4 RAG / Graph-RAG** — OpenSearch URL + credentials, embedding/reranker model +
  pinned revisions, TEI endpoints, RAG feature gates, Neo4j URI/credentials.
- **Phase 5 governance** — `SERVICEMIND_{MEMORY,CONTEXT,SKILLS,MODEL_GATEWAY_AUDIT,
  SEMANTIC_CACHE}_ENABLED` feature gates, model allowlists, cost/time ceilings.

Feature gates are off by default; read the Phase 4/5 docs before enabling them in a
target environment.

## HTTP API reference

| Endpoint | Purpose |
| --- | --- |
| `GET /health`, `GET /info` | Liveness; available agents/models |
| `POST /{agent_id}/invoke`, `/stream` | Generic toolkit agents (inherited) |
| `GET/POST /threads`, `POST /history`, `POST /feedback` | Conversation state + feedback |
| `POST /v1/servicemind/runs` | Start a governed GLPI ticket run (role `analyst`) |
| `GET /v1/servicemind/runs/{id}` | Run state + pending action intent |
| `POST /v1/servicemind/runs/{id}/approval` | Approve/reject a write action (role `approver`) |
| `POST /v1/servicemind/runs/{id}/review-resolution` | Resolve a review escalation |
| `POST /v1/servicemind/runs/{id}:cancel` | Cancel a pending/awaiting run |
| `GET /v1/servicemind/runs/{id}/events` | SSE stream of run events |
| `POST /v1/servicemind/webhooks/glpi` | Ingest a signed GLPI webhook (idempotent) |
| `GET /v1/servicemind/glpi/health` | GLPI connectivity/tenant probe |
| AG-UI endpoint | Connect any AG-UI compatible frontend |

## Testing and CI

The test suite is the contract. Keep it green before pushing:

```sh
uv run ruff format --check .
uv run ruff check .
uv run pyrefly check
uv run pytest                                   # full offline suite
uv run pytest tests/integration --run-docker    # docker-gated integration
```

`.github/workflows/test.yml` runs ruff, pyrefly, pytest (Python 3.12/3.13/3.14), markdown
linting, and a docker-based integration job. For per-commit authoring conventions see
[CLAUDE.md](CLAUDE.md).

## Documentation index

- Enterprise spec (Chinese): [`docs/企业IT服务管理(ITSM)智能体平台.md`](docs/企业IT服务管理(ITSM)智能体平台.md)
- Architecture map: [`docs/PHASE3_CURRENT_ARCHITECTURE_MAP.md`](docs/PHASE3_CURRENT_ARCHITECTURE_MAP.md)
- Phase 4 RAG technical baseline: [`docs/PHASE4_RAG_TECHNICAL_BASELINE.md`](docs/PHASE4_RAG_TECHNICAL_BASELINE.md)
- Phase 5 architecture & acceptance: [`docs/PHASE5_FINAL_ARCHITECTURE_AND_ACCEPTANCE.md`](docs/PHASE5_FINAL_ARCHITECTURE_AND_ACCEPTANCE.md)
- Phase acceptance reports: `docs/PHASE*_ACCEPTANCE.md`, plus live reports under
  `evaluation/reports/`
- Local deployment notes: [`LOCAL_DEPLOYMENT.md`](LOCAL_DEPLOYMENT.md)
- GLPI stack: [`deploy/glpi/README.md`](deploy/glpi/README.md)
- Dependencies & environment: [`DEPENDENCIES.md`](DEPENDENCIES.md)

## Upstream, history and license

This project derives from the 🧰 [AI Agent Service Toolkit](https://github.com/JoshuaC215/agent-service-toolkit)
(`JoshuaC215/agent-service-toolkit`, MIT), a full LangGraph + FastAPI + Streamlit agent
service toolkit by Joshua Carroll and contributors. The relationship is kept honest:

- **Full git history is retained** — the repo is not a squashed rewrite. It begins at the
  upstream lineage (~255 commits by Joshua Carroll and the toolkit's other contributors)
  and continues with the ServiceMind commits by **Shark6438** (product work starting
  upstream of `fe3b2dc`, September 2026). The contribution history on GitHub therefore
  shows both the ServiceMind maintainer and the upstream authors whose code this repo
  started from.
- **LICENSE and copyright are preserved** — [LICENSE](LICENSE) is the upstream MIT
  License, Copyright (c) 2024 Joshua Carroll. ServiceMind's additions are distributed
  under the same license.
- **Scaffold code stays attributed** — the inherited toolkit layers (`src/agents`, `src/
  core`, `src/schema`, `src/client`, `src/voice`, `src/streamlit_app.py`, the docker
  files, and the generic chat endpoints) remain their original authors' work.

ServiceMind's own additions (the `src/servicemind` product tree, migrations, GLPI stack,
evaluation harness, skills, and the ServiceMind phase docs) are maintained by
**Shark6438**. Thanks to the upstream toolkit and its authors for the foundation.

## Contributing

PRs welcome. Follow [CLAUDE.md](CLAUDE.md) conventions, keep the test suite green, and
preserve the upstream attribution in anything you touch.
