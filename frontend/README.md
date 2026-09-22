# ServiceMind Operator Console

Next.js 16 / React 19 control plane for the ServiceMind ITSM agent workflow. The console renders tenant-scoped runs, validated task DAGs, evidence and citations, Reviewer decisions, frozen action intents, governed memory review, append-only audit events, and a sanitized release-gate snapshot.

## Security boundary

- Browser authentication uses Keycloak Authorization Code + PKCE S256.
- Access tokens remain in the in-memory `keycloak-js` client; no token is written to browser storage.
- Every API call carries the user token. The backend remains authoritative for tenant, role, entity, group, approval, memory policy, and exact-snapshot checks.
- A per-response nonce CSP with `strict-dynamic` protects every script; the runtime verifier rejects missing or mismatched script nonces.
- The console has no direct GLPI write path. It can only request a governed ServiceMind run and decide already-frozen actions.
- The generic instance-agent `/agui` endpoint is deliberately excluded because it is outside the ServiceMind business-agent boundary.

## Local configuration

Copy `.env.local.example` to `.env.local` only when the defaults do not match the deployment. All variables are public endpoint identifiers; no client secret belongs in this application.

```bash
npm ci
npm run dev
```

The supported runtime is Node.js 24. On this host, run commands through the pinned Node container:

```bash
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$PWD:/app" -w /app node:24-bookworm npm run check
```

## Verification

```bash
npm run lint
npm run typecheck
npm run test
npm run build
npm run test:e2e
../.venv/bin/python ../scripts/verify_frontend_runtime.py --check
```

The live E2E suite requires the local API, Keycloak and frontend, plus `ACME_APPROVER_PASSWORD`. It runs desktop and mobile authentication, checks serious/critical axe violations, and captures workbench and quality-gate screenshots.

## Release snapshot

`public/release-status.json` is generated from the repository's accepted machine reports:

```bash
../.venv/bin/python ../scripts/export_frontend_release_status.py
```

It is intentionally a frozen, sanitized artifact. It does not claim real-time telemetry and must continue to expose the Phase 4 quality exception until tenant-domain evidence closes it.

CI also runs `npm audit`, exports a CycloneDX SBOM, and rejects any HIGH/CRITICAL finding in the production image with Trivy. The scanner image and the distroless Node runtime are pinned by OCI digest; the vulnerability database uses multiple official mirrors so a transient registry failure cannot silently disable the gate.

## Container and service

```bash
docker build -t servicemind-frontend:local .
systemctl --user enable --now servicemind-frontend.service
```

The versioned unit is `deploy/systemd/servicemind-frontend.service`; it binds the console to `127.0.0.1:3000` and runs the distroless image read-only as `nonroot`.
