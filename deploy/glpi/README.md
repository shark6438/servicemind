# ServiceMind local infrastructure stack

This Compose file defines the infrastructure used by the ITSM workflow: GLPI 11.0.8 and
MariaDB, ServiceMind PostgreSQL 16, Keycloak, OpenSearch, TEI embedding and reranking,
Neo4j, Redis and OPA. OpenSearch, both TEI services and Neo4j are in the optional `rag`
profile. It does **not** start the ServiceMind API, Next.js operator console or Streamlit
console; start those separately after the dependencies are ready.

From this directory, provide a local `.env` containing the credentials and database names
referenced by [`compose.yaml`](compose.yaml), then run:

```sh
docker compose up -d
docker compose ps
docker compose logs --tail 100 glpi
```

Use `docker compose --profile rag up -d` when the RAG dependencies and their model files
and GPU devices are available.

Local host ports from the Compose file:

| Service | Address |
| --- | --- |
| GLPI | <http://127.0.0.1:18088> |
| ServiceMind PostgreSQL | `127.0.0.1:55434` |
| Keycloak | <http://127.0.0.1:8090> |
| OpenSearch | <http://127.0.0.1:9200> |
| TEI embedding / reranker | `127.0.0.1:8085` / `127.0.0.1:8086` |
| Neo4j HTTP / Bolt | `127.0.0.1:17474` / `127.0.0.1:17687` by default |
| Redis / OPA | `127.0.0.1:6379` / <http://127.0.0.1:8181> |

The Neo4j host ports can be overridden through the Compose variables. Stop services
without deleting their named volumes with
`docker compose stop`. Never run `docker compose down --volumes` unless the local data
should be permanently deleted.

The initial GLPI administrator account is only for local bootstrap. Change its default
password before exposing the service beyond localhost. The `bootstrap_*.php` scripts
idempotently provision the ServiceMind entities, OAuth client, service identities,
webhooks and evaluation tickets; pass credentials through environment variables rather
than storing them in the scripts.

The API listens on host loopback at port 18080. Container callbacks use the dedicated
[`servicemind-webhook-proxy.service`](../systemd/servicemind-webhook-proxy.service),
which exposes only `POST /v1/servicemind/webhooks/glpi` on the Docker bridge gateway.
Configure its environment file at `~/.config/servicemind/webhook-proxy.env` with
`SERVICEMIND_WEBHOOK_PROXY_BIND_HOST` equal to that gateway,
`SERVICEMIND_WEBHOOK_PROXY_ALLOWED_CIDR` equal to this Compose network's subnet, and
`SERVICEMIND_WEBHOOK_PROXY_UPSTREAM=http://127.0.0.1:18080`. The proxy defaults to
loopback and does not expose general API routes. It preserves the signed body, strips
unrelated credentials, refuses redirects, and enforces request bounds.

Set `SERVICEMIND_WEBHOOK_TARGET_URL` in this directory's private `.env` to
`http://<docker-bridge-gateway>:18082/v1/servicemind/webhooks/glpi`. Find the gateway and
subnet with `docker inspect` / `docker network inspect`; do not assume a fixed address.
This value also creates the exact URL exception in `local_define.php`; other private
network URLs stay blocked. Enable the user proxy unit and recreate only the GLPI
container to load its environment. Then pass the same target to
`bootstrap_webhooks.php` along with the existing webhook secrets. The bootstrap now
refuses an unspecified target instead of registering an unreachable port 8080.
