from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "reconcile_frontend_oidc_client",
    ROOT / "scripts/reconcile_frontend_oidc_client.py",
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_versioned_frontend_oidc_contract_requires_pkce_and_exact_local_origins() -> None:
    desired = MODULE._desired(
        ROOT / "deploy/glpi/keycloak/servicemind-realm.json",
        "servicemind-api",
    )

    assert desired["pkce"] == "S256"
    assert desired["publicClient"] is True
    assert "http://127.0.0.1:3000" in desired["webOrigins"]
    assert all("*" not in origin for origin in desired["webOrigins"])


def test_oidc_drift_detects_removed_origin_and_disabled_pkce() -> None:
    desired = {
        "redirectUris": ["http://127.0.0.1:3000/*"],
        "webOrigins": ["http://127.0.0.1:3000"],
        "publicClient": True,
        "standardFlowEnabled": True,
        "pkce": "S256",
    }
    actual = {
        "redirectUris": [],
        "webOrigins": [],
        "publicClient": True,
        "standardFlowEnabled": True,
        "attributes": {},
    }

    assert MODULE._mismatches(actual, desired) == [
        "redirectUris",
        "webOrigins",
        "pkce.code.challenge.method",
    ]
