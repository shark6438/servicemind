#!/usr/bin/env python3
"""Reconcile Keycloak claims required by the Phase 5 memory review queue.

Realm import is creation-only for an existing Keycloak realm. This command makes the
user-profile declarations, protocol mapper and the seeded reviewers' realm roles and
GLPI claims converge without printing credentials or access tokens.

``--check`` reports drift without writing, and fails the run for anything it could not
have repaired; applying is therefore never reported as a check that would have failed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import httpx

PROFILE_ATTRIBUTES = {
    "tenant_id": False,
    "glpi_entity_ids": True,
    "glpi_group_ids": True,
}
MAPPER_NAME = "glpi-group-ids"
MAPPER_CONFIG = {
    "user.attribute": "glpi_group_ids",
    "claim.name": "glpi_group_ids",
    "multivalued": "true",
    "jsonType.label": "String",
    "id.token.claim": "true",
    "access.token.claim": "true",
    "userinfo.token.claim": "true",
}


def _env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def _desired_users(path: Path) -> dict[str, dict[str, Any]]:
    """Realm roles and attributes every seeded reviewer must hold.

    Roles belong to the identity contract rather than to decoration: the review queue
    and the decision endpoint gate on ``require_role("approver")``, which reads
    ``realm_access.roles``. A realm import reconciles roles only while the realm is
    being created, so a role removed afterwards leaves the queue answering 403 while
    every claim this command used to verify still looks correct.
    """
    realm = json.loads(path.read_text(encoding="utf-8"))
    return {
        user["username"]: {
            "roles": frozenset(user.get("realmRoles") or ()),
            "attributes": dict(user.get("attributes") or {}),
        }
        for user in realm.get("users", [])
        if (user.get("attributes") or {}).get("glpi_group_ids")
    }


def _user_mismatches(
    username: str,
    desired: dict[str, Any],
    *,
    actual_roles: set[str],
    actual_attributes: dict[str, list[str]],
) -> list[str]:
    """Diff one seeded identity against its declaration in the realm file.

    Role drift is reported in both directions: a missing ``approver`` denies the review
    queue, and an undeclared role grants authority the declaration never promised.
    Attribute drift stays one-directional -- declared keys must match, while attributes
    the declaration does not mention belong to whoever put them there.
    """
    desired_roles: frozenset[str] = desired["roles"]
    return [
        *(f"user:{username}:role_missing:{role}" for role in sorted(desired_roles - actual_roles)),
        *(
            f"user:{username}:role_undeclared:{role}"
            for role in sorted(actual_roles - desired_roles)
        ),
        *(
            f"user:{username}:attributes:{key}"
            for key, value in desired["attributes"].items()
            if actual_attributes.get(key) != value
        ),
    ]


def _mapper_payload() -> dict[str, Any]:
    return {
        "name": MAPPER_NAME,
        "protocol": "openid-connect",
        "protocolMapper": "oidc-usermodel-attribute-mapper",
        "consentRequired": False,
        "config": MAPPER_CONFIG,
    }


def _matches_mapper(mapper: dict[str, Any] | None) -> bool:
    if mapper is None:
        return False
    config = mapper.get("config") or {}
    return mapper.get("protocolMapper") == "oidc-usermodel-attribute-mapper" and all(
        config.get(key) == value for key, value in MAPPER_CONFIG.items()
    )


def _invalid_profile_attributes(profile: dict[str, Any]) -> list[str]:
    profile_by_name = {item["name"]: item for item in profile.get("attributes", [])}
    return [
        name
        for name, multivalued in PROFILE_ATTRIBUTES.items()
        if name not in profile_by_name
        or bool(profile_by_name[name].get("multivalued")) is not multivalued
        or "admin" not in (profile_by_name[name].get("permissions") or {}).get("view", [])
        or "admin" not in (profile_by_name[name].get("permissions") or {}).get("edit", [])
    ]


def _realm_role_catalog(
    client: httpx.Client, *, realm: str, headers: dict[str, str]
) -> dict[str, dict[str, Any]]:
    """Read every realm role; Keycloak's collection endpoint is paginated."""
    catalog: dict[str, dict[str, Any]] = {}
    first = 0
    page_size = 100
    while True:
        response = client.get(
            f"/admin/realms/{realm}/roles",
            params={"first": first, "max": page_size, "briefRepresentation": "false"},
            headers=headers,
        )
        response.raise_for_status()
        page = response.json()
        for role in page:
            catalog[role["name"]] = role
        if len(page) < page_size:
            return catalog
        first += len(page)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--base-url", default="http://127.0.0.1:8090")
    parser.add_argument("--realm", default="servicemind")
    parser.add_argument("--env-file", type=Path, default=Path("deploy/glpi/.env"))
    parser.add_argument(
        "--realm-definition",
        type=Path,
        default=Path("deploy/glpi/keycloak/servicemind-realm.json"),
    )
    args = parser.parse_args()
    environment = _env(args.env_file)
    desired_users = _desired_users(args.realm_definition)
    #: Drift found, and the subset of it this command cannot repair by itself. The two
    #: differ only outside --check, where a repaired finding must not still fail the
    #: run while an unrepairable one must not be reported as a pass.
    mismatches: list[str] = []
    unrepaired: list[str] = []

    with httpx.Client(base_url=args.base_url, timeout=20, trust_env=False) as client:
        token = client.post(
            "/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": environment["KEYCLOAK_ADMIN_USERNAME"],
                "password": environment["KEYCLOAK_ADMIN_PASSWORD"],
            },
        )
        token.raise_for_status()
        headers = {"Authorization": f"Bearer {token.json()['access_token']}"}

        clients = client.get(
            f"/admin/realms/{args.realm}/clients",
            params={"clientId": "servicemind-api"},
            headers=headers,
        )
        clients.raise_for_status()
        registered = clients.json()
        # A deleted client is drift to report, not an IndexError to crash on: the
        # mapper below hangs off it, and a traceback would read as a broken script
        # rather than as a realm that no longer matches its declaration.
        client_id: str | None = registered[0]["id"] if len(registered) == 1 else None
        if client_id is None:
            finding = (
                "client:servicemind-api:missing"
                if not registered
                else "client:servicemind-api:ambiguous"
            )
            mismatches.append(finding)
            unrepaired.append(finding)
        else:
            mapper_response = client.get(
                f"/admin/realms/{args.realm}/clients/{client_id}/protocol-mappers/models",
                headers=headers,
            )
            mapper_response.raise_for_status()
            mapper = next(
                (item for item in mapper_response.json() if item.get("name") == MAPPER_NAME), None
            )
            if not _matches_mapper(mapper):
                mismatches.append("protocol_mapper")
                if not args.check:
                    if mapper is None:
                        response = client.post(
                            f"/admin/realms/{args.realm}/clients/{client_id}"
                            f"/protocol-mappers/models",
                            headers=headers,
                            json=_mapper_payload(),
                        )
                    else:
                        response = client.put(
                            f"/admin/realms/{args.realm}/clients/{client_id}"
                            f"/protocol-mappers/models/{mapper['id']}",
                            headers=headers,
                            json={**_mapper_payload(), "id": mapper["id"]},
                        )
                    response.raise_for_status()
                    verified = client.get(
                        f"/admin/realms/{args.realm}/clients/{client_id}/protocol-mappers/models",
                        headers=headers,
                    )
                    verified.raise_for_status()
                    verified_mapper = next(
                        (item for item in verified.json() if item.get("name") == MAPPER_NAME),
                        None,
                    )
                    if not _matches_mapper(verified_mapper):
                        unrepaired.append("protocol_mapper")

        profile_response = client.get(f"/admin/realms/{args.realm}/users/profile", headers=headers)
        profile_response.raise_for_status()
        profile = profile_response.json()
        profile_by_name = {item["name"]: item for item in profile.get("attributes", [])}
        invalid_profile = _invalid_profile_attributes(profile)
        if invalid_profile:
            mismatches.append("user_profile")
            if not args.check:
                for name, multivalued in PROFILE_ATTRIBUTES.items():
                    profile_by_name[name] = {
                        **profile_by_name.get(name, {}),
                        "name": name,
                        "displayName": profile_by_name.get(name, {}).get("displayName", name),
                        "permissions": {"view": ["admin"], "edit": ["admin"]},
                        "multivalued": multivalued,
                    }
                profile["attributes"] = list(profile_by_name.values())
                saved = client.put(
                    f"/admin/realms/{args.realm}/users/profile",
                    headers=headers,
                    json=profile,
                )
                saved.raise_for_status()
                verified_profile = client.get(
                    f"/admin/realms/{args.realm}/users/profile", headers=headers
                )
                verified_profile.raise_for_status()
                if _invalid_profile_attributes(verified_profile.json()):
                    unrepaired.append("user_profile")

        catalog: dict[str, dict[str, Any]] = {}
        if not args.check:
            catalog = _realm_role_catalog(client, realm=args.realm, headers=headers)

        for username, desired in desired_users.items():
            users = client.get(
                f"/admin/realms/{args.realm}/users",
                params={"username": username, "exact": "true"},
                headers=headers,
            )
            users.raise_for_status()
            matches = users.json()
            if len(matches) != 1:
                # Creating an identity with its credentials is outside this command's
                # remit, so a missing reviewer stays a finding the operator must act on.
                finding = (
                    f"user:{username}:missing" if not matches else f"user:{username}:ambiguous"
                )
                mismatches.append(finding)
                unrepaired.append(finding)
                continue
            user_id = matches[0]["id"]
            response = client.get(f"/admin/realms/{args.realm}/users/{user_id}", headers=headers)
            response.raise_for_status()
            representation = response.json()
            current = representation.get("attributes") or {}
            mappings = client.get(
                f"/admin/realms/{args.realm}/users/{user_id}/role-mappings/realm",
                headers=headers,
            )
            mappings.raise_for_status()
            role_mappings = mappings.json()
            actual_roles = {item["name"] for item in role_mappings}
            drift = _user_mismatches(
                username,
                desired,
                actual_roles=actual_roles,
                actual_attributes=current,
            )
            mismatches.extend(drift)
            if args.check or not drift:
                continue

            desired_attributes = desired["attributes"]
            if any(current.get(key) != value for key, value in desired_attributes.items()):
                # Preserve attributes outside this command's contract, but actually
                # converge every declared tenant/entity/group claim. Previously these
                # mismatches were detected and then silently reported as repaired even
                # though the apply path never wrote them.
                updated = client.put(
                    f"/admin/realms/{args.realm}/users/{user_id}",
                    headers=headers,
                    json={
                        **representation,
                        "attributes": {**current, **desired_attributes},
                    },
                )
                updated.raise_for_status()

            missing = desired["roles"] - actual_roles
            for role in sorted(missing - catalog.keys()):
                # The declaration names a role this realm never defined. Granting it is
                # impossible, so record it as drift the run could not resolve rather
                # than reporting a convergence that did not happen.
                unrepaired.append(f"user:{username}:role_undefined:{role}")
            grantable = sorted(missing & catalog.keys())
            if grantable:
                granted = client.post(
                    f"/admin/realms/{args.realm}/users/{user_id}/role-mappings/realm",
                    headers=headers,
                    json=[catalog[name] for name in grantable],
                )
                granted.raise_for_status()

            undeclared = actual_roles - desired["roles"]
            if undeclared:
                # Use the representations returned by the user's own role mapping.
                # This avoids depending on the realm-role listing's pagination when an
                # undeclared role must be removed. ``Client.delete`` carries no body,
                # so the request is built by hand.
                by_name = {item["name"]: item for item in role_mappings}
                removed = client.request(
                    "DELETE",
                    f"/admin/realms/{args.realm}/users/{user_id}/role-mappings/realm",
                    headers=headers,
                    json=[by_name[name] for name in sorted(undeclared)],
                )
                removed.raise_for_status()

            # A 2xx response is not evidence that the intended state now exists. Read
            # both mutable surfaces back and make the postcondition the apply gate.
            verified_user = client.get(
                f"/admin/realms/{args.realm}/users/{user_id}", headers=headers
            )
            verified_user.raise_for_status()
            verified_mappings = client.get(
                f"/admin/realms/{args.realm}/users/{user_id}/role-mappings/realm",
                headers=headers,
            )
            verified_mappings.raise_for_status()
            unrepaired.extend(
                _user_mismatches(
                    username,
                    desired,
                    actual_roles={item["name"] for item in verified_mappings.json()},
                    actual_attributes=verified_user.json().get("attributes") or {},
                )
            )

    unresolvable = sorted(set(mismatches if args.check else unrepaired))
    print(
        json.dumps(
            {
                "status": "FAIL" if unresolvable else "PASS",
                "mode": "check" if args.check else "apply",
                "mismatches": sorted(mismatches),
                "unrepaired": unresolvable,
                "review_users": sorted(desired_users),
            },
            sort_keys=True,
        )
    )
    return 1 if unresolvable else 0


if __name__ == "__main__":
    raise SystemExit(main())
