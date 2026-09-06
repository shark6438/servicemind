# ServiceMind GLPI Phase 1

## Goal

Prove a real, reproducible and read-only integration from the existing LangGraph/FastAPI runtime to GLPI before implementing autonomous multi-agent writes.

## Architecture

```text
Streamlit / REST / SSE
        |
ServiceMind GLPI Agent (read only)
        |
Typed LangChain tools
        |
GlpiClient: OAuth token cache + entity/profile headers + error handling
        |
GLPI High-Level REST API v2.3
        |
GLPI 11.0.8 + MariaDB 11.8
```

## Delivered scope

- Official GLPI 11.0.8 image pinned in Docker Compose.
- MariaDB 11.8 with named persistent volumes and no published database port.
- GLPI bound only to `127.0.0.1:8088`.
- High-Level REST API v2.3 enabled.
- Dedicated `servicemind-agent` GLPI user with Technician profile on entity 0.
- Dedicated OAuth password-grant client with `api` and `graphql` scopes.
- Typed asynchronous API adapter with token caching and one-time refresh on HTTP 401.
- Explicit `GLPI-Entity`, `GLPI-Profile` and non-recursive entity headers.
- Stable Pydantic Ticket model; unused GLPI fields are not passed to the model.
- Rich-text normalization that removes markup, script and style content.
- Read-only tools for one ticket and the most recent tickets.
- A read-only ServiceMind GLPI Agent registered in the existing service.
- `/servicemind/glpi/health` authentication health endpoint.
- Unit tests for auth headers, parsing, pagination bounds and token refresh.

## Acceptance evidence

- OAuth password grant returns a one-hour bearer token.
- GLPI OpenAPI reports version 2.3.0.
- A seed VPN/MFA Incident was created as GLPI Ticket 1 and read back successfully.
- The Python tool returns the sanitized Ticket 1 payload from the live GLPI API.
- The Agent answers questions about Ticket 1 and recent tickets through tool calls.
- No write tool exists in the Agent; a close-ticket request is refused.

## Explicitly deferred

- Ticket updates, assignments, followups, Problem and Change creation.
- Human approval, action hashes, idempotency and read-after-write verification.
- GLPI webhooks and event deduplication.
- Enterprise RAG, memory, MCP, GraphRAG and multi-agent planning.
- Mirroring GLPI data into ServiceMind persistence.

## Phase 2 entry criteria

Before any model-triggered write is enabled, build an operation contract matrix against the installed GLPI OpenAPI schema. Each write must have a deterministic policy, required role, approval rule, idempotency key and read-after-write assertion.
