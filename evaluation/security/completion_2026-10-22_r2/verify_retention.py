"""Verify physical disposal of this batch's four isolated stores, preserving shared data."""

import hashlib
import json
import secrets
import socket
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psycopg
import redis
from neo4j import GraphDatabase

BATCH = Path(__file__).resolve().parent
ports = json.loads(Path("/tmp/servicemind-completion-ports.json").read_text())
nonce = "synthetic-retention-" + secrets.token_hex(16)
with psycopg.connect(
    host="127.0.0.1",
    port=ports["pg"],
    user="security_admin",
    password="isolated-test-only",
    dbname="servicemind_security",
) as connection:
    with connection.cursor() as cursor:
        cursor.execute("CREATE TABLE security_batch_retention (canary text NOT NULL)")
        cursor.execute("INSERT INTO security_batch_retention VALUES (%s)", (nonce,))
        cursor.execute("SELECT canary FROM security_batch_retention")
        assert cursor.fetchone()[0] == nonce
cache = redis.Redis(host="127.0.0.1", port=ports["redis"])
cache.set("security-completion-retention", nonce)
assert cache.get("security-completion-retention").decode() == nonce
cache.close()
with GraphDatabase.driver(
    f"bolt://127.0.0.1:{ports['neo4j']}", auth=("neo4j", "isolated-test-only")
) as driver:
    driver.execute_query("CREATE (n:SecurityCompletionRetention {canary:$canary})", canary=nonce)
    records, _, _ = driver.execute_query(
        "MATCH (n:SecurityCompletionRetention) RETURN n.canary AS canary"
    )
    assert any(r["canary"] == nonce for r in records)
with httpx.Client(
    base_url=f"http://127.0.0.1:{ports['os']}", trust_env=False, timeout=30
) as client:
    response = client.put(
        "/security-completion-retention/_doc/1?refresh=wait_for", json={"canary": nonce}
    )
    response.raise_for_status()
    response = client.get("/security-completion-retention/_doc/1")
    assert response.json()["_source"]["canary"] == nonce
names = ["servicemind-security-completion-" + suffix for suffix in ("pg", "redis", "neo4j", "os")]
inspected = json.loads(subprocess.check_output(["docker", "inspect", *names]))
volumes = sorted(
    {
        mount["Name"]
        for container in inspected
        for mount in container["Mounts"]
        if mount["Type"] == "volume"
    }
)
assert all(
    container["Config"]["Labels"]["servicemind.purpose"] == "security-assessment"
    for container in inspected
)
subprocess.run(["docker", "rm", "-f", "-v", *names], check=True, capture_output=True)
for name in names:
    assert subprocess.run(["docker", "inspect", name], capture_output=True).returncode != 0
for volume in volumes:
    assert (
        subprocess.run(["docker", "volume", "inspect", volume], capture_output=True).returncode != 0
    )
for port in ports.values():
    with socket.socket() as client:
        assert client.connect_ex(("127.0.0.1", port)) != 0
result = {
    "verdict": "PASS",
    "observed_at": datetime.now(UTC).isoformat(),
    "source_revision": json.loads((BATCH / "frozen-components.json").read_text())[
        "source_revision"
    ],
    "positive_controls": {
        store: "written and independently read before destruction"
        for store in ["PostgreSQL", "Redis", "Neo4j", "OpenSearch"]
    },
    "canary_sha256": hashlib.sha256(nonce.encode()).hexdigest(),
    "removed_containers": names,
    "removed_anonymous_volumes": volumes,
    "closed_ports": ports,
    "method": "Whole isolated store teardown removes batch Run/memory/document/index/cache/checkpoint/audit copies, including append-only ledgers, without altering production immutability triggers.",
    "retained_exceptions": [
        {
            "scope": "New raw evidence, source snapshots, synthetic fixture IDs and model observer hashes",
            "reason": "User-requested audit reproducibility",
            "expires_at": "2026-11-21T23:59:59+08:00",
        },
        {
            "scope": "Synthetic fixtures and append-only audit/approval records in shared deployment; native GLPI ticket 39 and new local-model probe IDs listed in artifacts",
            "reason": "User-authorized live assessment; preserve independent read-back and immutable ledgers",
            "expires_at": "2026-11-21T23:59:59+08:00",
            "disposal": "Authorized administrator must use existing retention procedure; this report does not certify production erasure or automatic expiry.",
        },
        {
            "scope": "Private pre-migration database/config backup",
            "reason": "Rollback safety; chmod 0700 directory and 0600 backup",
            "expires_at": "2026-11-21T23:59:59+08:00",
        },
    ],
}
(BATCH / "retention-observation.json").write_text(json.dumps(result, indent=2) + "\n")
print(
    json.dumps(
        {"verdict": "PASS", "containers_removed": len(names), "volumes_removed": len(volumes)}
    )
)
