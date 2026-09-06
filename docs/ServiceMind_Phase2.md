# ServiceMind Phase 2: enterprise vertical slice

> 完整文件级改造、企业验收矩阵、真实 Run/GLPI 证据与生产边界见 `docs/PHASE2_ENTERPRISE_ACCEPTANCE.md`。

## Outcome

Phase 2 turns the Phase 1 GLPI reader into a multi-tenant, durable and controlled-write ITSM Agent workflow.

```text
GLPI webhook or authenticated request
  -> Keycloak JWT / TenantContext
  -> PostgreSQL RLS run
  -> Data Agent
  -> Analysis Agent
  -> Action Agent (ActionIntent only)
  -> LangGraph interrupt
  -> Approver JWT + action hash
  -> idempotent executor
  -> GLPI Followup
  -> read-after-write verification
  -> audit + durable events
```

## Infrastructure

- GLPI 11.0.8 and MariaDB 11.8.
- ServiceMind PostgreSQL 16 on host port 55432.
- Keycloak 26.7.3 on host port 8090.
- Two Keycloak tenants and two GLPI entities: Acme (entity 1) and Globex (entity 2).
- Database migration owner and a separate `NOBYPASSRLS` runtime role.
- Alembic migrations and reproducible tenant/integration seed scripts.

## Identity and isolation

New Phase 2 endpoints require a Keycloak access token. The token supplies `tenant_id`, roles and allowed GLPI entity IDs. Request bodies cannot override identity.

Tenant-scoped tables have PostgreSQL RLS with `FORCE ROW LEVEL SECURITY`. The runtime transaction sets `app.tenant_id`; without it, tenant tables return no rows. GLPI calls additionally use the tenant-specific technical user, entity and profile.

## API

- `POST /v1/servicemind/runs`
- `GET /v1/servicemind/runs/{run_id}`
- `GET /v1/servicemind/runs/{run_id}/events` (SSE and `Last-Event-ID`)
- `POST /v1/servicemind/runs/{run_id}/approval`
- `POST /v1/servicemind/runs/{run_id}:cancel`
- `POST /v1/servicemind/webhooks/glpi`

## Controlled write policy

Only `append_ticket_followup` is enabled. The Action Agent creates a canonical ActionIntent and SHA-256 action hash but has no GLPI credentials. The deterministic executor requires an approver role and the exact reviewed action hash.

The idempotency key is bound to the tenant, run and action hash. Followups include a run/action marker. If a process crashes after GLPI accepts the write but before ServiceMind commits the result, a retry searches for that marker and suppresses a duplicate.

## Webhooks

GLPI has one entity-scoped webhook per tenant. Each uses a different secret. ServiceMind verifies `HMAC-SHA256(body + timestamp)`, rejects requests older than five minutes, checks the payload entity against the tenant integration, and deduplicates retries.

GLPI's server-side URL protection remains enabled. Local development adds one exact allowlist entry for the ServiceMind webhook endpoint on `host.docker.internal`.

## Verified scenarios

- Acme can read Ticket 2 but receives 404 for Globex Ticket 3.
- Globex receives 404 for an Acme Run.
- An analyst receives 403 when attempting approval.
- An altered action hash receives 409.
- A waiting run survives an API restart and resumes from PostgreSQL checkpoint state.
- Approval creates a private GLPI Followup and read-back verification succeeds.
- Repeated approval creates no additional Followup.
- A real GLPI queued webhook receives HTTP 202 and creates a completed ServiceMind Run.
- Durable SSE event replay honors `Last-Event-ID`.

## Demo

```powershell
Set-Location D:\FastAPI\agent-service-toolkit
.\start-servicemind.ps1
.\scripts\demo_phase2.ps1 -TicketId 2 -Approve
```

The script obtains separate analyst and approver tokens. It prints the reviewed action hash before approval and reports the verified GLPI Followup ID after execution.

## Deliberately deferred

- Additional write tools such as assignment, Problem and Change creation.
- Knowledge and Reviewer Agents (the final 1+5 architecture).
- Enterprise hybrid RAG, memory governance, MCP and model gateway.
- Dedicated worker queue, Temporal and Kubernetes.
