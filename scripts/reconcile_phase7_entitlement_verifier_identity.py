#!/usr/bin/env python3
"""Create-if-absent and converge the read-only identity the resume boundary verifies with.

The platform ships with no entitlement verifier installed, and that default is a safety
property rather than an unfinished one: ``resolve_resume_scope`` refuses to resume a run
whose requester's authority it cannot re-establish, so a deployment without the
credentials *pauses* instead of proceeding on a scope it never re-confirmed. Configured
wrongly, though, the same boundary stops meaning anything -- a verifier holding
``realm-admin`` would answer correctly and hand the serving process the ability to read
and rewrite every identity in the realm.

So the identity this command converges is deliberately one role wide:

* it lives in the **master** realm, because ``KeycloakEntitlementVerifier`` exchanges a
  password for a token at ``/realms/master/...`` with ``client_id=admin-cli``, and a user
  in any other realm cannot authenticate there;
* it holds exactly one client role, ``view-users`` of the master realm's
  ``<realm>-realm`` client -- the cross-realm admin client Keycloak creates for each
  realm. That covers the two calls the verifier makes (read a user by id, read that
  user's realm role mappings) and nothing else;
* every other role mapping it has accumulated is removed. "Least privilege" is not a
  statement about the role that was granted; a subject that has acquired a second one is
  not the subject this command is responsible for, and reporting it while leaving it in
  place would make the report the only thing that changed.

``--check`` reports and writes nothing, and exits 1 when the realm does not match, which
is the correct answer for a realm that has not been seeded yet.

Exit codes: 0 nothing to repair; 1 findings (in ``--check``) or findings left unrepaired;
2 this command or its inputs are misconfigured.

Values are never printed. The password lives in ``deploy/glpi/.env`` beside every other
seeded identity's, and is mirrored into the application ``.env`` under the names
``core.settings`` reads it from -- one source, so the file the server loads cannot drift
from the file that documents it.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import httpx

#: The master-realm account the serving process authenticates as. Named rather than
#: derived: it is a service identity, not a person, and the name is the only thing that
#: tells an operator reading the realm why it exists.
READER_USERNAME = "servicemind-entitlement-reader"

#: Where the credential is declared, in the deployment env that seeds every other
#: acceptance identity.
PASSWORD_VARIABLE = "KEYCLOAK_ENTITLEMENT_READER_PASSWORD"

#: The single client role the reader may hold, on the master realm's ``<realm>-realm``
#: client. ``view-users`` is what Keycloak's own admin console calls "read-only access to
#: users"; the verifier reads a user and that user's realm role mappings and stops.
GRANTED_ROLE = "view-users"

#: ``core.settings`` names for the same four values. The application reads its own
#: ``.env``, so the credential declared above is written here in the names the process
#: actually loads. Kept as a mapping rather than four literals at the call sites because
#: a rename on one side and not the other is a verifier that silently reports
#: ``NOT_CONFIGURED`` -- a pause, which looks exactly like the supported default.
SETTING_NAMES = {
    "url": "SERVICEMIND_KEYCLOAK_ADMIN_URL",
    "username": "SERVICEMIND_KEYCLOAK_ADMIN_USERNAME",
    "password": "SERVICEMIND_KEYCLOAK_ADMIN_PASSWORD",
    "realm": "SERVICEMIND_KEYCLOAK_ADMIN_REALM",
}


def _env(path: Path) -> dict[str, str]:
    """Parse a ``.env`` into a mapping, last assignment winning."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def _set_env_lines(path: Path, desired: dict[str, str]) -> list[str]:
    """Converge the named keys in ``path``, preserving every other line.

    Rewrites in place, replacing an existing assignment wherever it sits and appending
    the ones that are absent -- so a second run is a no-op and the file keeps whatever
    ordering and comments it already had. Returns the keys it changed; the values are
    never returned, logged, or formatted anywhere.
    """
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    remaining = dict(desired)
    changed: list[str] = []
    lines: list[str] = []
    for line in original.splitlines():
        stripped = line.strip()
        key = stripped.partition("=")[0].strip() if "=" in stripped else ""
        if key in remaining and not stripped.startswith("#"):
            wanted = remaining.pop(key)
            if stripped != f"{key}={wanted}":
                changed.append(key)
            lines.append(f"{key}={wanted}")
        else:
            lines.append(line)
    for key, value in remaining.items():
        lines.append(f"{key}={value}")
        changed.append(key)
    path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    return changed


def _admin_headers(client: httpx.Client, environment: dict[str, str]) -> dict[str, str]:
    """Authenticate as the realm administrator, who is the only one who can seed.

    Read from the deployment env rather than from the application settings on purpose:
    the application is configured for the *reader*, and a command that seeded the realm
    using the reader's own credential would be asserting the outcome it is meant to test.
    """
    for name in ("KEYCLOAK_ADMIN_USERNAME", "KEYCLOAK_ADMIN_PASSWORD"):
        if not environment.get(name):
            raise SystemExit(f"configuration error: the deployment env does not define {name}")
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
    return {"Authorization": f"Bearer {token.json()['access_token']}"}


def _user_by_name(client: httpx.Client, *, username: str, headers) -> list[dict]:
    """Every exact-username match in the master realm. Zero and two are different."""
    response = client.get(
        "/admin/realms/master/users",
        params={"username": username, "exact": "true"},
        headers=headers,
    )
    response.raise_for_status()
    return response.json()


def _role_mappings(client: httpx.Client, *, user_id: str, headers) -> dict[str, list[str]]:
    """Every role the account holds, as ``{"clientId/role": [...]}``-style labels.

    Read from the all-mappings endpoint rather than by querying the one client this
    command expects: an account that has quietly acquired a realm role or a role on some
    other client is precisely the condition this command exists to report, and asking
    only about the client it granted would make that condition unobservable.
    """
    response = client.get(f"/admin/realms/master/users/{user_id}/role-mappings", headers=headers)
    response.raise_for_status()
    body = response.json()
    labels: dict[str, list[str]] = {}
    realm_roles = sorted(item["name"] for item in body.get("realmMappings") or [])
    if realm_roles:
        labels["realm"] = realm_roles
    for client_id, mapping in sorted((body.get("clientMappings") or {}).items()):
        names = sorted(item["name"] for item in mapping.get("mappings") or [])
        if names:
            labels[client_id] = names
    return labels


def _client_uuid(client: httpx.Client, *, client_id: str, headers) -> str:
    found = client.get(
        "/admin/realms/master/clients", params={"clientId": client_id}, headers=headers
    )
    found.raise_for_status()
    matches = found.json()
    if len(matches) != 1:
        raise SystemExit(
            f"configuration error: the master realm does not hold exactly one {client_id!r} "
            f"client ({len(matches)} found); this realm has no cross-realm admin client for it"
        )
    return matches[0]["id"]


def _client_role(client: httpx.Client, *, uuid: str, name: str, headers) -> dict[str, Any] | None:
    response = client.get(f"/admin/realms/master/clients/{uuid}/roles/{name}", headers=headers)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def _probe(
    base_url: str, *, username: str, password: str, realm: str, subject_id: str
) -> dict[str, Any]:
    """Log in *as the reader* and exercise exactly what the verifier exercises.

    The admin API returning 200 to the realm administrator says nothing about what this
    identity can do. The questions that matter are asked here, as the reader: can it
    exchange its password for a token at the endpoint the verifier uses, can it read a
    subject and that subject's realm roles (the two calls ``_fetch`` makes), and is it
    refused when it tries to write one. A grant that is one role too wide passes the
    first two and fails the third.
    """
    try:
        token = httpx.post(
            f"{base_url}/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": username,
                "password": password,
            },
            timeout=20,
            trust_env=False,
        )
    except Exception as exc:  # noqa: BLE001 - a transport failure is a finding, not a crash
        return {"token": f"transport:{type(exc).__name__}"}
    if token.status_code != 200:
        return {"token": f"http:{token.status_code}"}
    headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
    with httpx.Client(base_url=base_url, timeout=20, trust_env=False) as client:
        read = client.get(f"/admin/realms/{realm}/users/{subject_id}", headers=headers)
        roles = client.get(
            f"/admin/realms/{realm}/users/{subject_id}/role-mappings/realm", headers=headers
        )
        # A write the reader must not be able to make. ``manage-users`` would allow it, and
        # an identity that can rewrite a subject is not a read-only one however it is
        # named; the refusal is checked rather than assumed from the grant list.
        refused = client.put(
            f"/admin/realms/{realm}/users/{subject_id}",
            headers=headers,
            json={"enabled": True},
        )
        return {
            "token": "ok",
            "read_subject": read.status_code,
            "read_roles": roles.status_code,
            "write_status": refused.status_code,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--base-url", default="http://127.0.0.1:8090")
    parser.add_argument("--realm", default="servicemind")
    parser.add_argument("--env-file", type=Path, default=Path("deploy/glpi/.env"))
    parser.add_argument("--settings-file", type=Path, default=Path(".env"))
    parser.add_argument(
        "--probe-subject",
        default="globex-approver",
        help="the servicemind-realm subject the read probe is performed against",
    )
    args = parser.parse_args()

    environment = _env(args.env_file)
    password = environment.get(PASSWORD_VARIABLE, "")
    if not password:
        # Refused rather than defaulted: an identity created with a guessed password is
        # one whose credential no longer matches the file that documents it, and the
        # failure would surface later as a run that pauses for no visible reason.
        raise SystemExit(
            f"configuration error: {args.env_file} does not define {PASSWORD_VARIABLE}"
        )

    mismatches: list[str] = []
    unrepaired: list[str] = []
    cross_realm_client = f"{args.realm}-realm"

    with httpx.Client(base_url=args.base_url, timeout=30, trust_env=False) as client:
        headers = _admin_headers(client, environment)
        client_uuid = _client_uuid(client, client_id=cross_realm_client, headers=headers)
        role = _client_role(client, uuid=client_uuid, name=GRANTED_ROLE, headers=headers)
        if role is None:
            raise SystemExit(
                f"configuration error: client {cross_realm_client!r} has no {GRANTED_ROLE!r} role"
            )

        matches = _user_by_name(client, username=READER_USERNAME, headers=headers)
        if len(matches) > 1:
            # Two accounts answering to one service name is not a state this command can
            # converge. Deleting either might remove an identity something else is using,
            # and choosing one would make the report a guess, so it stops here and says so.
            mismatches.append("reader:ambiguous")
            unrepaired.append("reader:ambiguous")
            matches = []
        elif not matches:
            mismatches.append("reader:missing")
            if args.check:
                unrepaired.append("reader:missing")
            else:
                created = client.post(
                    "/admin/realms/master/users",
                    headers=headers,
                    json={
                        "username": READER_USERNAME,
                        "enabled": True,
                        "emailVerified": True,
                        # The realm's user profile declares these required for the *user*
                        # role, and an account created without them is refused at login
                        # with "Account is not fully set up" -- the same answer a wrong
                        # password gives, which is the one failure this command exists to
                        # make impossible to reach.
                        "email": f"{READER_USERNAME}@localhost",
                        "firstName": "ServiceMind",
                        "lastName": "Entitlement Reader",
                        "requiredActions": [],
                    },
                )
                created.raise_for_status()
                location = created.headers.get("Location", "")
                user_id = location.rstrip("/").rsplit("/", 1)[-1] if location else ""
                if not user_id:
                    raise SystemExit("configuration error: Keycloak returned no id for the reader")
                reread = client.get(f"/admin/realms/master/users/{user_id}", headers=headers)
                reread.raise_for_status()
                matches = [reread.json()]

        if matches:
            user_id = matches[0]["id"]
            body = matches[0]
            if not body.get("enabled", True):
                mismatches.append("reader:disabled")
            held = _role_mappings(client, user_id=user_id, headers=headers)
            if held.get(cross_realm_client) != [GRANTED_ROLE]:
                mismatches.append(f"reader:roles:{json.dumps(held, sort_keys=True)}")
            if args.check:
                unrepaired.extend(mismatches)
            else:
                if not body.get("enabled", True):
                    enabled = client.put(
                        f"/admin/realms/master/users/{user_id}",
                        headers=headers,
                        json={"enabled": True},
                    )
                    enabled.raise_for_status()
                # Grant first, then narrow. A reader that is briefly without its one role
                # answers nothing, and a run that resumes in that window pauses for a
                # reason an operator cannot reproduce afterwards -- whereas narrowing last
                # means the window only ever holds *more* than the identity needs.
                if GRANTED_ROLE not in (held.get(cross_realm_client) or []):
                    granted = client.post(
                        f"/admin/realms/master/users/{user_id}/role-mappings/clients/{client_uuid}",
                        headers=headers,
                        json=[role],
                    )
                    granted.raise_for_status()
                surplus = {
                    client_id: [name for name in names if name != GRANTED_ROLE]
                    for client_id, names in held.items()
                    if client_id != cross_realm_client
                }
                surplus = {key: value for key, value in surplus.items() if value}
                extra_on_client = [
                    name for name in held.get(cross_realm_client, []) if name != GRANTED_ROLE
                ]
                if extra_on_client:
                    surplus.setdefault(cross_realm_client, []).extend(extra_on_client)
                for client_id, names in sorted(surplus.items()):
                    if client_id == "realm":
                        payload = [
                            item
                            for item in client.get(
                                "/admin/realms/master/roles", headers=headers
                            ).json()
                            if item["name"] in names
                        ]
                        url = f"/admin/realms/master/users/{user_id}/role-mappings/realm"
                    else:
                        uuid = (
                            client_uuid
                            if client_id == cross_realm_client
                            else _client_uuid(client, client_id=client_id, headers=headers)
                        )
                        payload = [
                            item
                            for item in client.get(
                                f"/admin/realms/master/clients/{uuid}/roles", headers=headers
                            ).json()
                            if item["name"] in names
                        ]
                        url = f"/admin/realms/master/users/{user_id}/role-mappings/clients/{uuid}"
                    removed = client.request("DELETE", url, headers=headers, json=payload)
                    removed.raise_for_status()

                # A created user has no password until one is set, and the realm template
                # never had one to import: this is the step that makes the credential real.
                reset = client.put(
                    f"/admin/realms/master/users/{user_id}/reset-password",
                    headers=headers,
                    json={"type": "password", "value": password, "temporary": False},
                )
                reset.raise_for_status()
                cleared = client.put(
                    f"/admin/realms/master/users/{user_id}",
                    headers=headers,
                    json={"requiredActions": []},
                )
                cleared.raise_for_status()

                # The write is not the postcondition. Read the account back and make the
                # state applied runs are judged on the thing this command reports.
                reread = client.get(f"/admin/realms/master/users/{user_id}", headers=headers)
                reread.raise_for_status()
                if not reread.json().get("enabled", True):
                    unrepaired.append("reader:disabled")
                final = _role_mappings(client, user_id=user_id, headers=headers)
                if final.get(cross_realm_client) != [GRANTED_ROLE]:
                    unrepaired.append(f"reader:roles:{json.dumps(final, sort_keys=True)}")

                # Both of the steps below run whether or not the probe subject exists. A
                # realm that has not been seeded with the acceptance identities still
                # needs its settings converged -- gating that on the probe would report
                # PASS for a deployment whose server cannot read the credential this
                # command just created.
                subjects = client.get(
                    f"/admin/realms/{args.realm}/users",
                    headers=headers,
                    params={"username": args.probe_subject, "exact": "true"},
                )
                subjects.raise_for_status()
                if subjects.json():
                    probe = _probe(
                        args.base_url,
                        username=READER_USERNAME,
                        password=password,
                        realm=args.realm,
                        subject_id=subjects.json()[0]["id"],
                    )
                    mismatch = (
                        probe.get("token") != "ok"
                        or probe.get("read_subject") != 200
                        or probe.get("read_roles") != 200
                        or probe.get("write_status") != 403
                    )
                    if mismatch:
                        unrepaired.append(f"reader:probe:{json.dumps(probe, sort_keys=True)}")
                else:
                    # Not a failure of the identity and not silently skipped either: on
                    # this realm the read half of the grant is unverified, and saying so
                    # is the only honest outcome.
                    unrepaired.append(f"reader:probe-subject-absent:{args.probe_subject}")

                settings_changes = _set_env_lines(
                    args.settings_file,
                    {
                        SETTING_NAMES["url"]: args.base_url,
                        SETTING_NAMES["username"]: READER_USERNAME,
                        SETTING_NAMES["password"]: password,
                        SETTING_NAMES["realm"]: args.realm,
                    },
                )
                if settings_changes:
                    # The names, never the values. A secret that reaches a terminal
                    # reaches a scrollback file and a transcript.
                    mismatches.append(f"settings:updated:{','.join(sorted(settings_changes))}")

    unresolvable = sorted(set(mismatches if args.check else unrepaired))
    print(
        json.dumps(
            {
                "status": "FAIL" if unresolvable else "PASS",
                "mode": "check" if args.check else "apply",
                "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "reader": READER_USERNAME,
                "realm": "master",
                "granted": f"{cross_realm_client}:{GRANTED_ROLE}",
                "target_realm": args.realm,
                "mismatches": sorted(mismatches),
                "unrepaired": unresolvable,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 1 if unresolvable else 0


if __name__ == "__main__":
    raise SystemExit(main())
