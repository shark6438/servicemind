#!/usr/bin/env python3
"""Converge the live public OIDC client to the versioned frontend contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import httpx


def _env(path: Path) -> dict[str, str]:
    return {
        key: value
        for raw in path.read_text(encoding="utf-8").splitlines()
        if (line := raw.strip()) and not line.startswith("#") and "=" in line
        for key, value in [line.split("=", 1)]
    }


def _desired(path: Path, client_id: str) -> dict[str, Any]:
    realm = json.loads(path.read_text(encoding="utf-8"))
    clients = [item for item in realm["clients"] if item.get("clientId") == client_id]
    if len(clients) != 1:
        raise ValueError(f"realm definition must declare exactly one {client_id!r} client")
    client = clients[0]
    return {
        "redirectUris": sorted(client.get("redirectUris", [])),
        "webOrigins": sorted(client.get("webOrigins", [])),
        "publicClient": True,
        "standardFlowEnabled": True,
        "pkce": (client.get("attributes") or {}).get("pkce.code.challenge.method"),
    }


def _mismatches(actual: dict[str, Any], desired: dict[str, Any]) -> list[str]:
    findings: list[str] = []
    for field in ("redirectUris", "webOrigins"):
        if sorted(actual.get(field, [])) != desired[field]:
            findings.append(field)
    if actual.get("publicClient") is not True:
        findings.append("publicClient")
    if actual.get("standardFlowEnabled") is not True:
        findings.append("standardFlowEnabled")
    if (actual.get("attributes") or {}).get("pkce.code.challenge.method") != desired["pkce"]:
        findings.append("pkce.code.challenge.method")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--base-url", default="http://127.0.0.1:8090")
    parser.add_argument("--realm", default="servicemind")
    parser.add_argument("--client-id", default="servicemind-api")
    parser.add_argument("--env-file", type=Path, default=Path("deploy/glpi/.env"))
    parser.add_argument("--realm-definition", type=Path, default=Path("deploy/glpi/keycloak/servicemind-realm.json"))
    args = parser.parse_args()
    environment = _env(args.env_file)
    desired = _desired(args.realm_definition, args.client_id)
    with httpx.Client(base_url=args.base_url, timeout=20, trust_env=False) as client:
        token = client.post("/realms/master/protocol/openid-connect/token", data={
            "grant_type": "password", "client_id": "admin-cli",
            "username": environment["KEYCLOAK_ADMIN_USERNAME"],
            "password": environment["KEYCLOAK_ADMIN_PASSWORD"],
        })
        token.raise_for_status()
        headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
        response = client.get(f"/admin/realms/{args.realm}/clients", params={"clientId": args.client_id}, headers=headers)
        response.raise_for_status()
        matches = response.json()
        if len(matches) != 1:
            print(json.dumps({"status": "FAIL", "findings": ["client_missing_or_ambiguous"]}))
            return 1
        actual = matches[0]
        findings = _mismatches(actual, desired)
        if findings and not args.check:
            attributes = dict(actual.get("attributes") or {})
            attributes["pkce.code.challenge.method"] = desired["pkce"]
            update = {**actual, "redirectUris": desired["redirectUris"], "webOrigins": desired["webOrigins"],
                "publicClient": True, "standardFlowEnabled": True, "attributes": attributes}
            saved = client.put(f"/admin/realms/{args.realm}/clients/{actual['id']}", headers=headers, json=update)
            saved.raise_for_status()
            verified = client.get(f"/admin/realms/{args.realm}/clients/{actual['id']}", headers=headers)
            verified.raise_for_status()
            findings = _mismatches(verified.json(), desired)
        status = "PASS" if not findings else "FAIL"
        print(json.dumps({"status": status, "mode": "check" if args.check else "apply", "findings": findings}))
        return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
