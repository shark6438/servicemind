"""Drift-detection contracts for `scripts/reconcile_phase5_memory_review_identity.py`.

The command exists so that "the realm still matches its declaration" is a machine
check rather than a memory. Two defects made it unable to answer that:

1. it reconciled claims -- ``glpi_group_ids`` and the protocol mapper that publishes
   them -- but never the realm roles, even though ``/memories/review-queue`` and
   ``/memories/{id}/review`` gate on ``require_role("approver")``. A realm import only
   assigns roles while the realm is being created, so a role removed afterwards left
   the queue answering 403 while every check still passed;
2. its status was ``FAIL if --check and mismatches else PASS``, so an apply run
   reported PASS for drift it had just failed to repair.

The script is a standalone operator command rather than an importable module, so it is
loaded by path, and Keycloak is replaced by a routing stub: the properties below are
about what the command decides, not about a live realm.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "reconcile_phase5_memory_review_identity.py"
)

APPROVER = "acme-approver"
USER_ID = "5e1c4d3a-0000-4000-8000-000000000001"
DESIRED_ROLES = ["analyst", "approver", "operator", "viewer"]
DESIRED_ATTRIBUTES = {
    "tenant_id": ["11111111-1111-4111-8111-111111111111"],
    "glpi_entity_ids": ["1"],
    "glpi_group_ids": ["1", "2"],
}


def _load_module():
    spec = importlib.util.spec_from_file_location("reconcile_review_identity", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["reconcile_review_identity"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def reconcile():
    return _load_module()


# --- the pure diff -----------------------------------------------------------


def test_declared_identity_with_no_drift_reports_nothing(reconcile) -> None:
    desired = {"roles": frozenset(DESIRED_ROLES), "attributes": DESIRED_ATTRIBUTES}
    assert (
        reconcile._user_mismatches(
            APPROVER,
            desired,
            actual_roles=set(DESIRED_ROLES),
            actual_attributes=dict(DESIRED_ATTRIBUTES),
        )
        == []
    )


def test_a_revoked_approver_role_is_drift(reconcile) -> None:
    """Without the role the review queue answers 403, and nothing else looks wrong."""
    desired = {"roles": frozenset(DESIRED_ROLES), "attributes": DESIRED_ATTRIBUTES}
    mismatches = reconcile._user_mismatches(
        APPROVER,
        desired,
        actual_roles={"analyst", "operator", "viewer"},
        actual_attributes=dict(DESIRED_ATTRIBUTES),
    )
    assert mismatches == [f"user:{APPROVER}:role_missing:approver"]


def test_an_undeclared_role_is_drift_too(reconcile) -> None:
    """Privilege creep on a reviewer is drift in the other direction, not silence."""
    desired = {"roles": frozenset(DESIRED_ROLES), "attributes": DESIRED_ATTRIBUTES}
    mismatches = reconcile._user_mismatches(
        APPROVER,
        desired,
        actual_roles={*DESIRED_ROLES, "tenant_admin"},
        actual_attributes=dict(DESIRED_ATTRIBUTES),
    )
    assert mismatches == [f"user:{APPROVER}:role_undeclared:tenant_admin"]


def test_attribute_drift_stays_one_directional(reconcile) -> None:
    """Declared keys must match; an attribute nobody declared is someone else's."""
    desired = {"roles": frozenset(DESIRED_ROLES), "attributes": DESIRED_ATTRIBUTES}
    mismatches = reconcile._user_mismatches(
        APPROVER,
        desired,
        actual_roles=set(DESIRED_ROLES),
        actual_attributes={**DESIRED_ATTRIBUTES, "glpi_group_ids": ["1"], "locale": ["en"]},
    )
    assert mismatches == [f"user:{APPROVER}:attributes:glpi_group_ids"]


def test_desired_users_carry_the_roles_the_realm_declares(reconcile, tmp_path: Path) -> None:
    definition = tmp_path / "realm.json"
    definition.write_text(
        json.dumps(
            {
                "users": [
                    {
                        "username": APPROVER,
                        "realmRoles": DESIRED_ROLES,
                        "attributes": DESIRED_ATTRIBUTES,
                    },
                    {
                        # No GLPI group claim: outside this command's review scope.
                        "username": "acme-analyst",
                        "realmRoles": ["viewer"],
                        "attributes": {"tenant_id": ["t"]},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    desired = reconcile._desired_users(definition)
    assert sorted(desired) == [APPROVER]
    assert desired[APPROVER]["roles"] == frozenset(DESIRED_ROLES)


# --- the command's decision, against a stub realm ----------------------------


class _Response:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


class _StubKeycloak:
    """Routes just enough of the admin API to exercise the command's decisions."""

    def __init__(
        self,
        realm: dict[str, Any],
        users: list[dict[str, Any]],
        *,
        persist_writes: bool = True,
    ) -> None:
        self.realm = realm
        self.users = users
        self.persist_writes = persist_writes
        self.realm_roles = [{"id": f"role-{name}", "name": name} for name in DESIRED_ROLES]
        self.writes: list[tuple[str, str]] = []

    def __enter__(self) -> _StubKeycloak:
        return self

    def __exit__(self, *_: object) -> bool:
        return False

    def _profile(self) -> dict[str, Any]:
        return {
            "attributes": [
                {
                    "name": name,
                    "multivalued": multivalued,
                    "permissions": {"view": ["admin"], "edit": ["admin"]},
                }
                for name, multivalued in {
                    "tenant_id": False,
                    **{key: True for key in ("glpi_entity_ids", "glpi_group_ids")},
                }.items()
            ]
        }

    def get(self, url: str, **_: Any) -> _Response:
        if url.endswith("/clients"):
            return _Response([{"id": "client-1"}] if self.realm.get("client") else [])
        if url.endswith("/protocol-mappers/models"):
            return _Response([self.realm["mapper"]] if self.realm.get("mapper") else [])
        if url.endswith("/users/profile"):
            return _Response(self._profile())
        if url.endswith("/roles"):
            return _Response(self.realm_roles)
        if url.endswith("/role-mappings/realm"):
            return _Response(
                [role for role in self.realm_roles if role["name"] in self.users[0]["roles"]]
            )
        if url.endswith("/users"):
            return _Response([{"id": USER_ID}] if self.users else [])
        return _Response({"attributes": self.users[0]["attributes"]})

    def post(self, url: str, **kwargs: Any) -> _Response:
        if url.endswith("/token"):
            return _Response({"access_token": "stub-token"})
        self.writes.append(("POST", url.rsplit("/", 1)[-1]))
        if self.persist_writes:
            for role in kwargs.get("json") or []:
                self.users[0]["roles"].add(role["name"])
        return _Response({})

    def put(self, url: str, **kwargs: Any) -> _Response:
        self.writes.append(("PUT", url.rsplit("/", 1)[-1]))
        if self.persist_writes and f"/users/{USER_ID}" in url:
            self.users[0]["attributes"] = dict(kwargs["json"]["attributes"])
        return _Response({})

    def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        self.writes.append((method, url.rsplit("/", 1)[-1]))
        if self.persist_writes:
            for role in kwargs.get("json") or []:
                self.users[0]["roles"].discard(role["name"])
        return _Response({})


def _mapper() -> dict[str, Any]:
    return {
        "name": "glpi-group-ids",
        "protocolMapper": "oidc-usermodel-attribute-mapper",
        "config": {
            "user.attribute": "glpi_group_ids",
            "claim.name": "glpi_group_ids",
            "multivalued": "true",
            "jsonType.label": "String",
            "id.token.claim": "true",
            "access.token.claim": "true",
            "userinfo.token.claim": "true",
        },
    }


def _run(
    reconcile,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    check: bool,
    roles: set[str],
    attributes: dict[str, list[str]] | None = None,
    present: bool = True,
    client: bool = True,
    persist_writes: bool = True,
) -> tuple[int, dict[str, Any], _StubKeycloak]:
    definition = tmp_path / "realm.json"
    definition.write_text(
        json.dumps(
            {
                "users": [
                    {
                        "username": APPROVER,
                        "realmRoles": DESIRED_ROLES,
                        "attributes": DESIRED_ATTRIBUTES,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text(
        "KEYCLOAK_ADMIN_USERNAME=admin\nKEYCLOAK_ADMIN_PASSWORD=not-a-real-secret\n",
        encoding="utf-8",
    )
    stub = _StubKeycloak(
        {"client": client, "mapper": _mapper()},
        [
            {
                "attributes": dict(DESIRED_ATTRIBUTES if attributes is None else attributes),
                "roles": set(roles),
            }
        ]
        if present
        else [],
        persist_writes=persist_writes,
    )
    monkeypatch.setattr(reconcile, "httpx", SimpleNamespace(Client=lambda **_: stub))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "reconcile",
            *(["--check"] if check else []),
            "--env-file",
            str(env_file),
            "--realm-definition",
            str(definition),
        ],
    )
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = reconcile.main()
    return code, json.loads(buffer.getvalue()), stub


def test_a_revoked_reviewer_role_fails_the_check_and_is_restored_by_apply(
    reconcile, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    revoked = {role for role in DESIRED_ROLES if role != "approver"}
    code, report, _ = _run(reconcile, monkeypatch, tmp_path, check=True, roles=revoked)
    assert code == 1
    assert report["status"] == "FAIL"
    assert report["mismatches"] == [f"user:{APPROVER}:role_missing:approver"]

    code, report, stub = _run(reconcile, monkeypatch, tmp_path, check=False, roles=revoked)
    assert code == 0
    assert report["status"] == "PASS"
    assert report["unrepaired"] == []
    assert report["mismatches"] == [f"user:{APPROVER}:role_missing:approver"]
    assert ("POST", "realm") in stub.writes
    assert "approver" in stub.users[0]["roles"]


def test_apply_never_passes_drift_it_could_not_repair(
    reconcile, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The command cannot create an identity, so a missing reviewer must stay red.

    Reporting PASS here is the exact failure mode the status field had: an apply run
    that repairs nothing still exits 0, and the drift survives the "successful" run.
    """
    code, report, _ = _run(
        reconcile,
        monkeypatch,
        tmp_path,
        check=False,
        roles=set(DESIRED_ROLES),
        present=False,
    )
    assert code == 1
    assert report["status"] == "FAIL"
    assert report["unrepaired"] == [f"user:{APPROVER}:missing"]


def test_a_missing_client_is_drift_rather_than_a_crash(
    reconcile, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code, report, _ = _run(
        reconcile,
        monkeypatch,
        tmp_path,
        check=True,
        roles=set(DESIRED_ROLES),
        client=False,
    )
    assert code == 1
    assert "client:servicemind-api:missing" in report["mismatches"]


def test_apply_converges_declared_attributes_and_verifies_the_written_state(
    reconcile, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    drifted = {**DESIRED_ATTRIBUTES, "glpi_group_ids": ["999"]}
    code, report, stub = _run(
        reconcile,
        monkeypatch,
        tmp_path,
        check=False,
        roles=set(DESIRED_ROLES),
        attributes=drifted,
    )
    assert code == 0
    assert report["status"] == "PASS"
    assert report["mismatches"] == [f"user:{APPROVER}:attributes:glpi_group_ids"]
    assert report["unrepaired"] == []
    assert stub.users[0]["attributes"]["glpi_group_ids"] == ["1", "2"]
    assert ("PUT", USER_ID) in stub.writes


def test_apply_fails_when_keycloak_accepts_a_write_without_converging(
    reconcile, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 2xx response is insufficient; the state read back must match the contract."""
    revoked = {role for role in DESIRED_ROLES if role != "approver"}
    code, report, _ = _run(
        reconcile,
        monkeypatch,
        tmp_path,
        check=False,
        roles=revoked,
        persist_writes=False,
    )
    assert code == 1
    assert report["status"] == "FAIL"
    assert report["unrepaired"] == [f"user:{APPROVER}:role_missing:approver"]


def test_a_converged_realm_passes_in_both_modes(
    reconcile, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for check in (True, False):
        code, report, _ = _run(
            reconcile, monkeypatch, tmp_path, check=check, roles=set(DESIRED_ROLES)
        )
        assert code == 0
        assert report["status"] == "PASS"
        assert report["mismatches"] == []
