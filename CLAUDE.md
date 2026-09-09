# CLAUDE.md

## Comment style

- Default to no comments in code you add or edit.
- Only add a comment when the *why* is non-obvious: a hidden constraint, a subtle invariant, or a workaround for a specific bug. Never explain *what* the code does — clear naming should do that.
- Never write multi-paragraph comment blocks or over-explain inline. Match the terseness of the surrounding code.
- Exception: brand-new files may start with a short, genuinely useful module/file-level docstring. This does not license verbose inline comments throughout the rest of the file.
- When editing an existing file, match its existing comment density and style rather than introducing a heavier style than what's already there.

## Repository orientation

This is **ServiceMind**, an enterprise ITSM agent platform. It is a derivative of
`JoshuaC215/agent-service-toolkit` (MIT, full history retained — see LICENSE and the
"Upstream & license" section of README). Keep that provenance honest: never strip the
upstream copyright, and keep the README attribution intact.

Product layout:

- `src/servicemind/` — the product: multi-agent orchestration, enterprise hybrid RAG
  (OpenSearch + rerank) and Graph-RAG (Neo4j), governed long-term memory, model gateway,
  GLPI integration, context/redaction, persistence. This tree is self-contained except
  for shared infra in `src/core` (settings/model lookup) and `src/schema/models.py`.
- `migrations/`, `deploy/glpi/`, `evaluation/`, `skills/`, `docs/PHASE*.md` — product data
  and phase documentation.
- `src/agents`, `src/client`, `src/core`, `src/memory`, `src/schema`, `src/service`,
  `src/voice`, `src/streamlit_app.py` — runtime scaffolding inherited from the toolkit
  that the service still boots on (`src/service/service.py` mounts both the generic agent
  endpoints and the ServiceMind `phase2_router`). These layers are load-bearing: do not
  delete them as "dead" without removing the boot/test/compose wiring that pins them.

Key facts when working here:

- Test suite is the contract: keep `uv run pytest` green (full suite, plus `--run-docker`
  for docker-gated integration). Keep `uv run ruff format --check .`, `uv run ruff check .`
  and `uv run pyrefly check` clean — they are enforced in `.github/workflows/test.yml`.
- Python `>=3.12,<3.15`, managed with `uv`; `pyproject.toml` + `uv.lock` are the dependency
  source of truth (`requirements.txt` is a generated convenience file — do not hand-edit).
- The service image boots with `python run_service.py` and needs `SERVICEMIND_DATABASE_URL`
  at import; tiktoken/embedding initialization must stay out of import time.
- For user-facing and commit messages, follow the tone of existing docs/commits.
