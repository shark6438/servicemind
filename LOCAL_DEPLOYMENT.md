# Local deployment

This checkout is configured to run entirely on the local machine with Ollama (`qwen2.5:1.5b`) and SQLite. No cloud model API key is required.

`NO_PROXY` is set for localhost because this Windows installation has a system proxy; without the bypass, Python's Ollama client receives a proxy-generated HTTP 502 response.

## Start

```powershell
Set-Location D:\FastAPI\agent-service-toolkit
.\start-local.ps1
```

- API documentation: <http://127.0.0.1:8080/docs>
- Streamlit UI: <http://127.0.0.1:8501>
- GLPI: <http://127.0.0.1:8088>
- Keycloak: <http://127.0.0.1:8090>
- ServiceMind PostgreSQL: `127.0.0.1:55432`

## Stop

```powershell
.\stop-local.ps1
```

Runtime logs and PID files are stored in `.runtime/`. Conversation checkpoints are stored in `data/checkpoints.db`.

To use a cloud model, set its API key in `.env`, remove the `OLLAMA_*` settings, and keep `USE_FAKE_MODEL=false`.

Use `start-servicemind.ps1` and `stop-servicemind.ps1` to start or stop the Agent service, UI, GLPI and MariaDB together.

The combined script also applies Alembic migrations, initializes LangGraph PostgreSQL schemas and idempotently seeds the Acme/Globex tenant integrations.

## This Linux server

The deployed API in this checkout is a **user systemd unit**, not a system unit and not
the process on port 8080:

- unit: `servicemind-api.service` under `systemctl --user`;
- working directory: `/home/shihongye/data1/servicemind`;
- configured bind address: `127.0.0.1:18080` from `.env`;
- health URL: `http://127.0.0.1:18080/health`.

Use the machine-readable verifier so the unit scope, executable, working directory,
configured port, HTTP status and response body are checked together. The verifier
disables environment proxies for its loopback request; otherwise a local health probe
can be sent to a proxy and return an unrelated HTTP 502.

```sh
cd /home/shihongye/data1/servicemind
.venv/bin/python scripts/verify_servicemind_runtime.py --check \
  --output evaluation/reports/servicemind_runtime_latest.json
```

The four versioned unit files live in `deploy/systemd/`. API and outbox use installed
console entry points and do not depend on `PYTHONPATH`; the Next.js console runs from the
versioned `servicemind-frontend:local` image. Run `uv sync --frozen` before restarting
Python services, and rebuild the image after a frontend change.

For direct inspection use `systemctl --user status servicemind-api.service` and
`curl --noproxy '*' http://127.0.0.1:18080/health`. A plain `systemctl is-active
servicemind` checks the system manager and therefore does not address this deployment.

Existing Keycloak realms are not updated by `--import-realm`. After adding or changing
the Phase 5 memory-review grants, reconcile the user-profile declarations, the
`glpi_group_ids` token mapper, the seeded reviewers' realm roles and their attributes,
then run the read-only drift check:

```sh
.venv/bin/python scripts/reconcile_phase5_memory_review_identity.py
.venv/bin/python scripts/reconcile_phase5_memory_review_identity.py --check
```

The command reads credentials from `deploy/glpi/.env` and never prints passwords or
tokens. A missing `glpi_group_ids` claim is interpreted as no group access, so the
review queue fails closed.

The React operator console runs as `servicemind-frontend.service` on
`http://127.0.0.1:3000`. Build and install it with:

```sh
docker build -t servicemind-frontend:local frontend
cp deploy/systemd/servicemind-frontend.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now servicemind-frontend.service
```

An existing Keycloak client also needs its exact redirect URIs, web origins and PKCE
S256 setting reconciled from the versioned realm declaration:

```sh
.venv/bin/python scripts/reconcile_frontend_oidc_client.py
.venv/bin/python scripts/reconcile_frontend_oidc_client.py --check
```

The client is public and has no browser secret. The console sends each user's in-memory
access token to the API; API-side tenant/RBAC/ABAC checks remain authoritative. The API
CORS allowlist accepts only the exact origins in `SERVICEMIND_FRONTEND_ORIGINS`.

Realm roles are part of the same contract: the review queue gates on
`require_role("approver")`, and realm import assigns roles only while the realm is being
created. The check therefore compares each seeded reviewer's realm roles against the
realm definition in both directions -- a role that went missing (the queue would answer
403) and a role nobody declared -- and exits non-zero for anything it could not have
repaired. Run the command without `--check` to converge, then re-run `--check`.
