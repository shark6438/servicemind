#!/usr/bin/env python3
"""Create-if-absent and converge the Phase 7 acceptance identities in Keycloak.

The acceptance cases turn on four subjects differing *only* in which GLPI groups they
hold -- group 3, group 4, none, and an approver holding both -- because "a group grant
reaches the retrieval index" is only observable when a sibling identity that lacks the
grant is looking at the same corpus. A run against a realm missing one of the four does
not fail; it quietly stops testing the thing the cases exist to test, and reports a pass.

Realm import is creation-only for an existing realm, so the declaration in
``servicemind-realm.json`` cannot be the thing that makes a live realm match it. Unlike
``reconcile_phase5_memory_review_identity.py`` -- which deliberately refuses to create
identities and is left unmodified -- this command does create them, because a password
has to come from somewhere and ``.env`` is where every other seeded identity's comes
from. Drift is *classified* by delegating to that script's ``_user_mismatches``, so both
commands report identity drift in one vocabulary rather than two.

``--check`` reports and writes nothing; a realm that has not been seeded yet is expected
to report findings and exit 1, which is a correct answer rather than an error.

Exit codes: 0 nothing to repair; 1 findings (in ``--check``) or findings left unrepaired
(in apply mode); 2 this command or its inputs are misconfigured -- exit 2 is deliberately
not 1, because "the roster is not declared" and "the realm does not match the roster" are
different problems for whoever is reading the output.
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx

#: The roster is named explicitly rather than derived from the declaration. Keycloak's
#: realm file is a template for *every* identity the platform ships; a rule like "has a
#: globex tenant id" would sweep in identities that predate acceptance and silently start
#: resetting their credentials. Naming the four keeps this command's blast radius equal
#: to the thing it was written for, and ``_declared`` refuses a roster entry the realm
#: file does not declare.
ACCEPTANCE_USERNAMES = (
    "globex-analyst-g3",
    "globex-analyst-g4",
    "globex-analyst-nogroup",
    "globex-approver",
)

#: Where each subject's credential lives. Values are never printed by this command.
PASSWORD_VARIABLE = {
    "globex-analyst-g3": "GLOBEX_ANALYST_G3_PASSWORD",
    "globex-analyst-g4": "GLOBEX_ANALYST_G4_PASSWORD",
    "globex-analyst-nogroup": "GLOBEX_ANALYST_NOGROUP_PASSWORD",
    "globex-approver": "GLOBEX_APPROVER_PASSWORD",
}

#: Claims the platform reads to build a ``TenantContext``. A subject can exist, hold the
#: right roles and attributes, and still reach the platform with none of this in its
#: token, because a protocol mapper is a separate object from the attribute it maps.
#: Presence is checked on the token that a *login* produces, not on the admin view.
REQUIRED_CLAIMS = ("tenant_id", "glpi_entity_ids")

#: Fields the realm declares per user that belong to the account itself rather than to
#: ``attributes``. They are converged here because they are not free-form labels: this
#: realm's user profile declares ``firstName`` and ``lastName`` as *required for the
#: ``user`` role*, so an account created without them is one Keycloak refuses to
#: authenticate. It refuses with ``invalid_grant: Account is not fully set up`` -- the
#: same answer a wrong password produces -- while the admin view shows no
#: ``requiredActions``, because the action is computed from the profile at login and is
#: not stored on the user. That makes a missing name indistinguishable from a bad
#: credential unless the *account* fields are converged as first-class state.
PROFILE_FIELDS = ("email", "firstName", "lastName")


def _load_sibling(name: str) -> ModuleType:
    """Import a script next to this one as a module, without a package.

    ``scripts/`` is not importable, and the alternative -- copying the drift rules -- is
    how two commands end up disagreeing about what drift is.
    """
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"_servicemind_{name}", path)
    if spec is None or spec.loader is None:  # pragma: no cover - filesystem failure
        raise SystemExit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


phase5 = _load_sibling("reconcile_phase5_memory_review_identity")


def _declared(path: Path) -> dict[str, dict[str, Any]]:
    """The roster's role, attribute and profile declaration, from the realm template."""
    users = {
        user["username"]: {
            "roles": frozenset(user.get("realmRoles") or ()),
            "attributes": dict(user.get("attributes") or {}),
            "profile": {field: user[field] for field in PROFILE_FIELDS if field in user},
        }
        for user in json.loads(path.read_text(encoding="utf-8")).get("users", [])
    }
    absent = [name for name in ACCEPTANCE_USERNAMES if name not in users]
    if absent:
        raise SystemExit(
            f"configuration error: {path} does not declare {absent}; the roster and the "
            "realm template must agree before anything is written"
        )
    # A template that omits a required profile field cannot be converged *to* anything:
    # there is no declared value to write, and the account it would produce could not log
    # in. Refused here rather than at the login that would fail to explain itself.
    incomplete = [
        f"{name}:{field}"
        for name in ACCEPTANCE_USERNAMES
        for field in PROFILE_FIELDS
        if field not in users[name]["profile"]
    ]
    if incomplete:
        raise SystemExit(
            f"configuration error: {path} declares no {incomplete}; the realm's user "
            "profile requires these of every account, so an identity created without them "
            "cannot authenticate"
        )
    return {name: users[name] for name in ACCEPTANCE_USERNAMES}


def _user_by_name(client: httpx.Client, *, realm: str, username: str, headers) -> list[dict]:
    """Every exact-username match. A list, because zero and two are different findings.

    Keycloak's ``exact=true`` still returns a collection, and a realm holding two
    accounts for one name is a state where "converge this user" has no defined target --
    reporting it as ``missing`` would send an operator to create a third.
    """
    response = client.get(
        f"/admin/realms/{realm}/users",
        params={"username": username, "exact": "true"},
        headers=headers,
    )
    response.raise_for_status()
    return response.json()


def _roles_of(client: httpx.Client, *, realm: str, user_id: str, headers) -> set[str]:
    response = client.get(
        f"/admin/realms/{realm}/users/{user_id}/role-mappings/realm", headers=headers
    )
    response.raise_for_status()
    return {item["name"] for item in response.json()}


def _claims_of_token(token: str) -> dict[str, Any]:
    """Read a JWT payload without verifying it.

    The token is this process's own answer from this realm, so the signature proves
    nothing it did not already know; what is being read is which claims the realm chose
    to put in it, and a verification would only obscure that.
    """
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    body = json.loads(base64.urlsafe_b64decode(payload))
    return {
        "tenant_id": body.get("tenant_id"),
        "glpi_entity_ids": body.get("glpi_entity_ids"),
        "glpi_group_ids": body.get("glpi_group_ids"),
        "roles": sorted((body.get("realm_access") or {}).get("roles") or ()),
    }


def _effective_identity(
    base_url: str, *, realm: str, username: str, password: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Log in as the subject and report what its token actually carries.

    Returns the claims, or a one-word reason the login did not happen. A subject that
    cannot log in is not an acceptance identity however correct its admin-API record
    looks, and a subject that logs in without the tenant claim will be refused by the API
    for a reason that looks like an authorization bug rather than a missing mapper.
    """
    try:
        response = httpx.post(
            f"{base_url}/realms/{realm}/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "servicemind-api",
                "username": username,
                "password": password,
            },
            timeout=20,
            trust_env=False,
        )
    except Exception as exc:  # noqa: BLE001 - a transport failure is a finding, not a crash
        return None, f"transport:{type(exc).__name__}"
    if response.status_code != 200:
        return None, f"http:{response.status_code}"
    return _claims_of_token(str(response.json()["access_token"])), None


def _create(
    client: httpx.Client,
    *,
    realm: str,
    username: str,
    desired: dict[str, Any],
    password: str,
    headers,
) -> str:
    created = client.post(
        f"/admin/realms/{realm}/users",
        headers=headers,
        json={
            "username": username,
            "enabled": True,
            "emailVerified": True,
            # The declared names, not synthesized ones: they are what the realm's user
            # profile requires, and taking them from the template keeps the created
            # account identical to the one a realm import would have produced.
            **desired["profile"],
            "requiredActions": [],
            "attributes": desired["attributes"],
        },
    )
    created.raise_for_status()
    location = created.headers.get("Location", "")
    user_id = location.rstrip("/").rsplit("/", 1)[-1] if location else ""
    if not user_id:
        raise SystemExit(f"configuration error: Keycloak returned no id for {username}")
    # A created user has no password until one is set, and the password is not part of
    # the realm template's *live* effect -- import substitution happens at realm
    # creation, which is long past. This is the step that makes the credential real.
    _reset_credential(
        client,
        realm=realm,
        user_id=user_id,
        password=password,
        headers=headers,
        required_actions=[],
    )
    return user_id


def _reset_credential(
    client: httpx.Client,
    *,
    realm: str,
    user_id: str,
    password: str,
    headers,
    required_actions: list[str],
) -> bool:
    """Set the declared password, non-temporary, and clear pending required actions.

    Both halves in one place because Keycloak reports a pending ``UPDATE_PASSWORD`` and a
    wrong password the same way -- ``invalid_grant: Account is not fully set up`` against
    ``invalid_grant`` -- and a repair that fixed only one of them would look like it had
    done nothing. Returns whether the write was attempted and accepted; the caller
    re-verifies against the live directory rather than trusting this.
    """
    reset = client.put(
        f"/admin/realms/{realm}/users/{user_id}/reset-password",
        headers=headers,
        json={"type": "password", "value": password, "temporary": False},
    )
    reset.raise_for_status()
    cleared = client.put(
        f"/admin/realms/{realm}/users/{user_id}",
        headers=headers,
        json={"requiredActions": required_actions},
    )
    cleared.raise_for_status()
    return True


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

    environment = phase5._env(args.env_file)
    desired_by_user = _declared(args.realm_definition)
    missing_variables = [
        PASSWORD_VARIABLE[name]
        for name in ACCEPTANCE_USERNAMES
        if not environment.get(PASSWORD_VARIABLE[name])
    ]
    if missing_variables:
        # Refused rather than defaulted: an identity created with a guessed password is
        # an identity whose credential no longer matches the file that documents it.
        raise SystemExit(
            f"configuration error: {args.env_file} does not define {missing_variables}"
        )

    mismatches: list[str] = []
    unrepaired: list[str] = []
    subjects: list[dict[str, Any]] = []
    credentials: dict[str, str] = {}

    with httpx.Client(base_url=args.base_url, timeout=30, trust_env=False) as client:
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
        catalog = (
            {}
            if args.check
            else phase5._realm_role_catalog(client, realm=args.realm, headers=headers)
        )

        for username in ACCEPTANCE_USERNAMES:
            desired = desired_by_user[username]
            password = environment[PASSWORD_VARIABLE[username]]
            matches = _user_by_name(client, realm=args.realm, username=username, headers=headers)

            if len(matches) != 1:
                finding = (
                    f"user:{username}:missing" if not matches else f"user:{username}:ambiguous"
                )
                mismatches.append(finding)
                if args.check or matches:
                    unrepaired.append(finding)
                    continue
                _create(
                    client,
                    realm=args.realm,
                    username=username,
                    desired=desired,
                    password=password,
                    headers=headers,
                )
                matches = _user_by_name(
                    client, realm=args.realm, username=username, headers=headers
                )
                if len(matches) != 1:  # pragma: no cover - Keycloak accepted then lost the write
                    unrepaired.append(finding)
                    continue

            user_id = matches[0]["id"]
            representation = client.get(
                f"/admin/realms/{args.realm}/users/{user_id}", headers=headers
            )
            representation.raise_for_status()
            actual_attributes = representation.json().get("attributes") or {}
            actual_roles = _roles_of(client, realm=args.realm, user_id=user_id, headers=headers)

            current = representation.json()
            drift = phase5._user_mismatches(
                username, desired, actual_roles=actual_roles, actual_attributes=actual_attributes
            )
            mismatches.extend(drift)
            profile_drift = [
                f"user:{username}:profile:{field}"
                for field, value in desired["profile"].items()
                if current.get(field) != value
            ]
            mismatches.extend(profile_drift)

            if not args.check and (drift or profile_drift):
                attributes_differ = any(
                    actual_attributes.get(key) != value
                    for key, value in desired["attributes"].items()
                )
                profile_differs = any(
                    current.get(field) != value for field, value in desired["profile"].items()
                )
                if attributes_differ or profile_differs:
                    updated = client.put(
                        f"/admin/realms/{args.realm}/users/{user_id}",
                        headers=headers,
                        json={
                            **current,
                            **desired["profile"],
                            "attributes": {**actual_attributes, **desired["attributes"]},
                        },
                    )
                    updated.raise_for_status()

                missing_roles = desired["roles"] - actual_roles
                for role in sorted(missing_roles - catalog.keys()):
                    unrepaired.append(f"user:{username}:role_undefined:{role}")
                grantable = sorted(missing_roles & catalog.keys())
                if grantable:
                    granted = client.post(
                        f"/admin/realms/{args.realm}/users/{user_id}/role-mappings/realm",
                        headers=headers,
                        json=[catalog[name] for name in grantable],
                    )
                    granted.raise_for_status()

                undeclared = actual_roles - desired["roles"]
                if undeclared:
                    by_name = {
                        item["name"]: item
                        for item in client.get(
                            f"/admin/realms/{args.realm}/users/{user_id}/role-mappings/realm",
                            headers=headers,
                        ).json()
                    }
                    removed = client.request(
                        "DELETE",
                        f"/admin/realms/{args.realm}/users/{user_id}/role-mappings/realm",
                        headers=headers,
                        json=[by_name[name] for name in sorted(undeclared)],
                    )
                    removed.raise_for_status()

            claims, reason = _effective_identity(
                args.base_url, realm=args.realm, username=username, password=password
            )
            if claims is None and not args.check:
                # A user on this roster whose password is not the declared one is a state
                # *this command* owns: the subject is created here, with this password, and
                # the roster is what makes it an acceptance identity. Reporting the state
                # without repairing it leaves a documented prerequisite permanently unmet
                # and reads, downstream, as an authorization bug.
                #
                # ``requiredActions`` is cleared in the same write because a pending
                # UPDATE_PASSWORD makes Keycloak refuse the grant with "Account is not
                # fully set up" -- the very 400 a wrong password produces, so leaving it
                # would make the two indistinguishable.
                repaired = _reset_credential(
                    client,
                    realm=args.realm,
                    user_id=user_id,
                    password=password,
                    headers=headers,
                    required_actions=[],
                )
                if repaired:
                    claims, reason = _effective_identity(
                        args.base_url, realm=args.realm, username=username, password=password
                    )
            credentials[username] = "verified" if claims else f"rejected:{reason}"
            if claims is None:
                mismatches.append(f"user:{username}:credential_not_usable:{reason}")
                unrepaired.append(f"user:{username}:credential_not_usable:{reason}")
            else:
                for claim in REQUIRED_CLAIMS:
                    if not claims.get(claim):
                        mismatches.append(f"user:{username}:claim_absent:{claim}")
                        unrepaired.append(f"user:{username}:claim_absent:{claim}")

            subjects.append(
                {
                    "username": username,
                    "id": user_id,
                    "declared_roles": sorted(desired["roles"]),
                    "declared_attributes": desired["attributes"],
                    "effective_claims": claims,
                }
            )

        # A 2xx is not evidence the intended state now exists. Read both mutable surfaces
        # back and make the postcondition -- not the write -- the thing applied runs are
        # judged on. Nothing is re-verified on the credential, because the login above
        # already happened against the live directory rather than against a record.
        if not args.check:
            for username in ACCEPTANCE_USERNAMES:
                matches = _user_by_name(
                    client, realm=args.realm, username=username, headers=headers
                )
                if len(matches) != 1:
                    unrepaired.append(
                        f"user:{username}:missing" if not matches else f"user:{username}:ambiguous"
                    )
                    continue
                representation = client.get(
                    f"/admin/realms/{args.realm}/users/{matches[0]['id']}", headers=headers
                )
                representation.raise_for_status()
                reread = representation.json()
                unrepaired.extend(
                    phase5._user_mismatches(
                        username,
                        desired_by_user[username],
                        actual_roles=_roles_of(
                            client, realm=args.realm, user_id=matches[0]["id"], headers=headers
                        ),
                        actual_attributes=reread.get("attributes") or {},
                    )
                )
                unrepaired.extend(
                    f"user:{username}:profile:{field}"
                    for field, value in desired_by_user[username]["profile"].items()
                    if reread.get(field) != value
                )

    unresolvable = sorted(set(mismatches if args.check else unrepaired))
    print(
        json.dumps(
            {
                "status": "FAIL" if unresolvable else "PASS",
                "mode": "check" if args.check else "apply",
                "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "mismatches": sorted(mismatches),
                "unrepaired": unresolvable,
                "credentials": credentials,
                "subjects": sorted(subjects, key=lambda item: item["username"]),
                "roster": list(ACCEPTANCE_USERNAMES),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 1 if unresolvable else 0


if __name__ == "__main__":
    raise SystemExit(main())
