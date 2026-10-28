# Optional Jev route advisor

`src/servicemind/orchestration/jev_router.py` wires the offline-tested Jev +
confidence-floor hybrid into the real `route_node`. The deterministic
`FastPathRouter` remains the route of record: the advisor is an additive layer that
can make a request *more* reviewed, never less.

The feature is **off by default**. With `SERVICEMIND_JEV_ROUTER_MODE=off` the code path
matches the rule-only router exactly: no provider call, no ledger write and no router
config object is constructed.

## Modes

| Mode | Behaviour |
| --- | --- |
| `off` (default) | Rule decision only. The advisor is not constructed and nothing leaves the process. |
| `shadow` | Calls the advisor for comparison, serves the rule decision. Events record `source=rule_shadow` plus the model's route/confidence. Spend is still budgeted and journaled. |
| `active` | Applies the behaviour matrix below. |

## Behaviour matrix

Let `floor` be `SERVICEMIND_JEV_ROUTER_CONFIDENCE_FLOOR` (default `0.9`, inclusive).

| Request | Rule decision | Advisor result | Served decision | `source` / reason |
| --- | --- | --- | --- | --- |
| write | `COMPLEX_WORKFLOW` | `conf ≥ floor` and `UNSUPPORTED` | `UNSUPPORTED` | `jev_override` / `jev_model_refusal` |
| write | `COMPLEX_WORKFLOW` | anything else | rule decision, including `ACTION` | `rule` |
| write | `UNSUPPORTED` | anything | rule `UNSUPPORTED` (never relaxed) | `rule` |
| non-write | `UNSUPPORTED` | `conf ≥ floor` and not `UNSUPPORTED` | `COMPLEX_WORKFLOW` | `jev_override` / `jev_unsupported_override_reviewed` |
| non-write | `UNSUPPORTED` | `UNSUPPORTED`, low confidence or error | rule `UNSUPPORTED` | `rule` |
| non-write | other | `conf ≥ floor`, same route | rule decision (original reason kept) | `jev_confirmed` |
| non-write | other | `conf ≥ floor`, different route | model route + fixed capabilities | `jev_override` / `jev_model_route` (or `jev_model_refusal`) |
| non-write | non-refusal | low confidence | rule decision | `rule_fallback` / `confidence_below_floor` |
| any | any | timeout, transport, HTTP, malformed response, budget, ledger, circuit | rule decision | `rule_fallback` / error code |
| any | any | mode `shadow` | rule decision | `rule_shadow` |

Write requests still call the advisor. The v3.1 data showed the rule router sends
"把 4110 这张报障单彻底抹掉" ("wipe ticket 4110 completely") to the reviewed pipeline
because the verb is not in `FORBIDDEN_PATTERNS`; the advisor classified those as
`UNSUPPORTED`. Skipping the advisor for writes would leave that gap open. The
restriction is therefore one-way: for writes the advisor may only add a refusal, never
remove one and never produce a fast path.

Capabilities are fixed by route and are never taken from the model:

| Route | Capabilities |
| --- | --- |
| `unsupported` | none |
| `simple_knowledge_query` | `knowledge` |
| `simple_data_query` | `data` |
| `complex_workflow` | `data`, `knowledge`, `analysis`, `reviewer` |

`action` is reachable only through the rule router's write path. The model cannot name
an agent, a tool, a principal, an ACL or a tenant.

## Cost control

- Before the HTTP call, a reservation is appended and `fsync`ed to a per-day JSONL
  journal: `.runtime/jev_router/spend-YYYY-MM-DD.jsonl`.
- A call is admitted only if `accounted + reservation ≤ daily budget` and fewer than
  `per-minute limit` reservations were made in the last 60 seconds.
- `accounted = settled + unknown + pending`. A reservation is never refunded. Timeouts,
  transport errors, non-200 responses and 200 responses without usage stay counted: an
  HTTP status alone does not prove the provider did not bill.
- Reservations are sized from the payload (`bytes × 2/3` tokens, × list price × 1.25);
  `settle` replaces that estimate with provider-reported `usage.cost` or, when absent,
  the list-price estimate marked `cost_source=list_price`.
- The journal is protected by `fcntl.flock`, so the daily and per-minute caps hold
  across processes. Journal I/O runs in a worker thread with a timeout so a slow disk
  cannot block the event loop.
- A corrupt, locked or unwritable journal disables the advisor for that request and the
  rule router serves. Do not "fix" this by deleting the journal.
- A process-local circuit breaker (default 5 consecutive provider/validation failures,
  60 s reset) and a concurrency bulkhead (default 4) shed load without queueing. Both
  use `threading.Lock` + `time.monotonic`, not loop-bound asyncio primitives.

## Error codes

`missing_api_key`, `empty_request`, `payload_too_large`, `context_ceiling_exceeded`,
`ledger_unavailable`, `ledger_timeout`, `budget_exhausted`, `rate_limited`,
`concurrency_limited`, `circuit_open`, `timeout`, `transport_error`, `http_<status>`,
`invalid_provider_response`, `unexpected_served_model`, `usage_missing`,
`confidence_below_floor`, `internal_error`.

The `route.completed` event is additive. It keeps `route` and `reason_code` and may add
`mode`, `source`, `fallback_reason`, `rule_route`, `rule_reason_code`, `jev_route`,
`jev_confidence`, `served_model`, `latency_ms`, `cost_usd`, `cost_source`,
`input_tokens`, `output_tokens`, `http_status`, `attempt_id`. It never contains the
request text, response body or API key.

## Enabling and rolling back

1. Set `OPENROUTER_API_KEY` and `SERVICEMIND_JEV_ROUTER_MODE=active` (or `shadow` first).
2. Ensure `SERVICEMIND_JEV_ROUTER_LEDGER_DIR` is writable by the service user and
   survives restarts. The default `.runtime/jev_router` is gitignored.
3. Watch `route.completed` for `source` and `fallback_reason`; use `shadow` if you want
   the model's route recorded without changing service behaviour.

Rolling back is a configuration change back to `off`; no code rollback is required. A
timeout, open circuit, exhausted budget or broken ledger also falls back automatically.

Provider model updates require explicitly changing
`SERVICEMIND_JEV_ROUTER_SERVED_MODEL`. The default pins the exact version observed for
all 500 v3.1 responses (`typesafe/jev-1.14-20261014`); a different served version causes
a fallback rather than a silent route change.

## Evidence scope

The offline v3.1 campaign (260 held-out synthetic cases over 13 topics) measured rule
130/260 and Jev 247/260; the dev-selected 0.9 hybrid measured 260/260 on this designed
bilingual lexical/intent benchmark. Latency was p50 1128 ms / p95 1718 ms at roughly
$0.0000314 per request. That benchmark is template-heavy and synthetic; it does not
establish production accuracy.

The production matrix is deliberately not identical to the 260/260 offline hybrid. Two
differences are intentional:

- A non-write rule refusal, when the model gives a high-confidence non-refusal, is sent
  to `COMPLEX_WORKFLOW` for review instead of a fast path.
- Write requests can only be tightened.

Recomputed over the same 260 test rows, this production policy routes all 65 write
requests correctly, sends zero `expected=unsupported` rows to a fast path, and matches
the exact offline route label on 234/260 rows. The 26 differing rows are cases where a
negated operation or a documentation lookup was refused by the rule router; those now
go to the reviewed pipeline, so the user still gets an answer. This is not a claim of
reproducing 260/260 in production.

## Tests

`tests/servicemind/test_jev_ledger.py`, `test_jev_router.py` and
`test_jev_routing_integration.py` use `httpx.MockTransport` and a temporary ledger
directory; they never reach a provider. `[tool.pytest_env]` forces
`SERVICEMIND_JEV_ROUTER_MODE=off`, and an integration test asserts that, so a developer's
local `.env` cannot make the suite spend money.

The frozen v3.1 preregistration hashes `router.py` and `domain/routing.py`; an
integration test pins both sha256 values so an accidental edit to either file fails the
suite.
