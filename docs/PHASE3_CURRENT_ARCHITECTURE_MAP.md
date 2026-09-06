# ServiceMind Phase 3 — Current Architecture Map

> Inspection date: 2026-09-05. This map records the repository before Phase 3 implementation and freezes the Phase 2 write boundary.

## Runtime map

| Existing module | Current responsibility | Phase 3 decision |
| --- | --- | --- |
| `src/service/service.py` | FastAPI entry, upstream Agent routes, lifespan, checkpointer/store initialization | Modify only to configure the Phase 3 graph; retain upstream routes and authentication |
| `src/servicemind/api.py` | Tenant-authenticated run, approval, cancellation, SSE and GLPI webhook APIs | Reuse; add only Phase 3 review-escalation API if required |
| `src/servicemind/orchestration/workflow.py` | Phase 2 `data -> analysis -> action -> approval -> execute` StateGraph | Preserve as `phase2_graph`; Phase 3 receives a separate graph builder and becomes the default run graph |
| `src/servicemind/orchestration/runtime.py` | Builds tenant-scoped graph config and starts/resumes a run | Adapt to the Phase 3 graph without changing API callers |
| PostgreSQL `AsyncPostgresSaver` | Durable LangGraph checkpoint | Reuse; no second checkpoint system |
| `src/servicemind/persistence/repository.py` | Tenant-RLS run/event/action/approval/idempotency/audit access | Reuse; add only Phase 3 persistence operations needed by API/runtime |

## Frozen Phase 2 Harness

The only authorized business-write entry is:

```text
ControlledActionExecutor.execute(TenantContext, ActionIntent)
```

Its implementation is `src/servicemind/harness/executor.py` and currently permits only `append_ticket_followup`.

The existing write chain is frozen:

```text
persisted ActionIntent
  -> approver JWT + exact action hash
  -> PostgreSQL idempotency record
  -> tenant-scoped advisory lock
  -> GLPI marker reconciliation
  -> GLPI private Followup
  -> read-after-write verification
  -> append-only audit
```

Phase 3 must not add a second GLPI writer, approval implementation, credential resolver, idempotency table or executor.

## Identity and tenant boundary

| Module | Existing control | Phase 3 reuse |
| --- | --- | --- |
| `security/auth.py` | Keycloak JWT signature/issuer/audience/time claims, roles and allowed GLPI entities | TenantContext must be copied into every subgraph input and never accepted from model output |
| `persistence/database.py` | Transaction-local `app.tenant_id` | All Phase 3 persistence continues through tenant sessions |
| Alembic 0001 | `FORCE ROW LEVEL SECURITY` on tenant tables | No bypass or parallel persistence model |
| `integrations/glpi/resolver.py` | Decrypts tenant integration only after entity authorization | Only Data Agent and frozen Harness may reach it |

## Current agents

| Agent | Current capability | Phase 3 treatment |
| --- | --- | --- |
| Data Agent | Tenant-scoped GLPI Ticket read | Wrap in a structured Evidence-producing subgraph |
| Analysis Agent | Structured Ticket analysis with deterministic fallback | Adapt to consume joined Evidence and return AnalysisResult |
| Action Agent | Produces Followup ActionIntent only | Extend input contract to validated HandoffEnvelope; keep credential-free |
| Knowledge Agent | No ServiceMind implementation; upstream examples require AWS KB or OpenAI-backed Chroma | Add a Phase 3 read-only baseline Runbook provider with provenance; do not implement Phase 4 hybrid RAG |
| Reviewer Agent | Missing | Add structured, evidence-aware reviewer with no tools or write access |
| Supervisor | Missing | Add control-plane planner/DAG/dispatcher/replan/handoff components; no GLPI access |

## Existing integration capability

- `GlpiClient`: OAuth cache, one 401 refresh, entity/profile headers, Ticket read, Followup list/write.
- Stable GLPI models strip raw HTML/script/style before model context.
- No Problem, Change, Group or CMDB adapter exists yet. Phase 3 contracts may reserve those capabilities, but the planner may schedule only implemented providers.
- The approved Phase 3 write demo is a private recommendation Followup, not Assignment Group mutation.

## Existing observability and tests

- RunEvent provides durable sequence events and SSE replay.
- OTel spans exist for Data, Analysis and Tool execution; OTLP/HTTP export is optional.
- Phase 2 regression baseline: 203 passed, 4 skipped; real GLPI, concurrency, cross-tenant, approval, crash/recovery and browser verification already passed.

## Required adapters, not replacements

Phase 3 may add:

- structured domain contracts;
- Agent Registry and contracts;
- deterministic Router, Planner, DAG Validator, Dispatcher, Budget and Loop Guard;
- Knowledge/Reviewer agents;
- Evidence adapters around existing Data/Analysis outputs;
- Phase 3 graph and runtime adapter;
- an ActionIntent adapter into `ControlledActionExecutor`.

It must not replace:

- Keycloak verification;
- TenantContext;
- PostgreSQL RLS;
- GLPI credential encryption/resolution;
- approval records;
- idempotency/advisory lock;
- GLPI Followup writer;
- read-back verification;
- audit events.

## Inspection outcome

Phase 3 is feasible without breaking Phase 2. The only prompt conflict was resolved by the user: complex-write E2E will create a private GLPI recommendation Followup through the existing Harness; `assign_ticket` remains out of scope.
