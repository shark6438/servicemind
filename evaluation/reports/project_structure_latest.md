# ServiceMind project structure audit

Status: **PASS**

This audit covers repository structure, import direction, packaging and the four deployed
process manifests. It does not rate unrelated instance Agents and does not convert missing
RAG business evidence into a quality pass.

## Executable gates

| gate | result |
| --- | --- |
| `src_layout` | PASS |
| `build_backend_declared` | PASS |
| `explicit_package_discovery` | PASS |
| `streamlit_pages_packaged` | PASS |
| `installed_process_entrypoints` | PASS |
| `tests_use_installed_distribution` | PASS |
| `fastapi_application_factory` | PASS |
| `http_adapter_split` | PASS |
| `frontend_application_boundary` | PASS |
| `frontend_release_manifests` | PASS |
| `domain_dependency_rule` | PASS |
| `acyclic_servicemind_packages` | PASS |
| `source_tree_boundary` | PASS |
| `scaffold_import_budget` | PASS |
| `installed_execution_hygiene` | PASS |
| `versioned_process_manifests` | PASS |

## Frozen structure

- Packaged `src` modular monolith; tests import the installed editable distribution.
- `service.service:create_app` is the composition root.
- Domain contracts do not import runtime, persistence, HTTP or provider adapters.
- HTTP entry adapters live under `servicemind.interfaces.http`; repositories and providers
  remain outbound adapters.
- API, Streamlit, outbox and the Next.js operator console are separate versioned process
  manifests.
- The inherited top-level instance-agent shell remains load-bearing but sits outside the
  ServiceMind platform dependency graph.

## Scaffold import budget

The product and the instance-agent shell are separate lineages; every import below is
migration debt, so the budget may only shrink. A new import site in an already-coupled
file fails the gate rather than passing silently.

| scaffold root | current sites | budget |
| --- | --- | --- |
| `core` | 26 | 26 |
| `schema` | 1 | 1 |

## Package dependency graph

| package | depends on |
| --- | --- |
| `_root` | `domain`, `harness`, `integrations`, `interfaces`, `model_gateway`, `orchestration`, `persistence`, `security` |
| `agents` | `context`, `domain`, `foundation`, `integrations`, `rag`, `runtime`, `security` |
| `context` | `foundation`, `persistence` |
| `domain` | — |
| `evaluation` | `context`, `domain`, `graphrag`, `integrations`, `memory`, `orchestration`, `persistence` |
| `foundation` | — |
| `graphrag` | `domain` |
| `harness` | `domain`, `integrations`, `persistence`, `security` |
| `integrations` | `persistence`, `security` |
| `interfaces` | `memory`, `persistence`, `security` |
| `mcp` | `persistence`, `security`, `tool_platform` |
| `memory` | `domain`, `persistence` |
| `model_gateway` | `domain`, `persistence` |
| `observability` | `context` |
| `orchestration` | `agents`, `context`, `domain`, `foundation`, `harness`, `integrations`, `memory`, `model_gateway`, `observability`, `persistence`, `rag`, `runtime`, `security`, `skills` |
| `persistence` | — |
| `rag` | `context`, `domain`, `foundation`, `graphrag`, `integrations`, `persistence`, `runtime`, `security` |
| `reliability` | `persistence` |
| `runtime` | `domain`, `integrations`, `model_gateway`, `security`, `tool_platform` |
| `security` | `domain` |
| `skills` | `context` |
| `tool_platform` | `domain`, `graphrag`, `integrations`, `memory`, `persistence`, `security` |

Strongly connected package components: **0**.

## Reference basis

- [PyPA: src layout](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/)
- [PyPA: pyproject and entry points](https://packaging.python.org/en/latest/specifications/pyproject-toml/)
- [FastAPI: larger applications and routers](https://fastapi.tiangolo.com/tutorial/bigger-applications/)
- [LangGraph: discrete nodes, raw state and durable execution](https://docs.langchain.com/oss/javascript/langgraph/thinking-in-langgraph)
- [OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/)
- [Twelve-Factor App](https://12factor.net/)
