"""Execute the Phase 7.6 acceptance cases against the running stack, and record what happened.

The driver observes; it does not judge. Every case is turned into one
``evaluation/acceptance/replays/<case>.json`` -- a ``CaseExecution`` -- and the grader is
a separate pure function over those files, so a verdict can be re-derived offline from the
raw observation and a change in judgement never requires re-running the stack.

Three rules the code below follows, because breaking any of them makes the result a
statement about the driver rather than about the platform:

* **A step's behaviour is declared, not inferred.** Steps are dispatched on the step's
  own ``id`` and ``action``, and an id the driver does not know is an error rather than a
  best guess. A case edit that renamed a step would otherwise silently execute some
  default path and produce observations for a step nobody performed.
* **Nothing is recorded that was not seen.** An assertion's evidence is the response, the
  row, or the probe output -- never "it must have worked because the next step did".
* **A driver that cannot finish says so.** Failures are collected into
  ``CaseExecution.errors`` and the case is left unobserved rather than partly asserted
  on; an exception escaping to the top would lose the observations already taken.

Secrets are read from ``deploy/glpi/.env`` by variable name and never printed, logged, or
written into a replay: the replays are committed evidence.

    uv run python scripts/verify_phase7_acceptance_live.py
    uv run python scripts/verify_phase7_acceptance_live.py --only ACC-13 --timeout-scale 2
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import selectors
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
from dotenv import dotenv_values
from sqlalchemy import select

from core import settings
from servicemind.domain.analysis import AnalysisResult
from servicemind.domain.evidence import CITATION_KEY, Evidence, evidence_items
from servicemind.domain.knowledge import Citation
from servicemind.domain.review import ReviewResult
from servicemind.evaluation.acceptance import (
    AcceptanceCase,
    AcceptanceCaseSet,
    CaseExecution,
    CaseStep,
    ObservedActionIntent,
    ObservedEnvironment,
    ObservedFollowup,
    ObservedGraphReading,
    ObservedMemoryRecord,
    ObservedSubject,
    StepObservation,
    case_set_digest,
    new_followups,
)
from servicemind.evaluation.graph_probe import graph_fixture_reading, principal_for
from servicemind.graphrag.build import build_graph_store
from servicemind.integrations.glpi.client import GlpiClient
from servicemind.integrations.glpi.models import html_to_text
from servicemind.integrations.glpi.resolver import resolve_glpi_config
from servicemind.persistence.database import close_database, tenant_session
from servicemind.persistence.models import (
    AuditEvent,
    ContextArtifactRecord,
    MemoryRecordRow,
    ToolOutboxRecord,
)
from servicemind.reliability.outbox import (
    PUBLISHED_RETENTION,
    STREAM_MAXLEN,
    RedisStreamPublisher,
)
from servicemind.security.auth import TenantContext
from servicemind.security.entitlements import current_entitlement_verifier
from servicemind.tool_platform.runtime import build_tool_gateway

REPO_ROOT = Path(__file__).resolve().parents[1]
CASES = REPO_ROOT / "evaluation" / "acceptance" / "cases.v1.json"
REPLAYS = REPO_ROOT / "evaluation" / "acceptance" / "replays"
FIXTURE_TICKETS = REPO_ROOT / "evaluation" / "acceptance" / "fixtures" / "globex" / "tickets.json"
DEPLOY_ENV = REPO_ROOT / "deploy" / "glpi" / ".env"
FRONTEND = REPO_ROOT / "frontend"

DEFAULT_BASE_URL = "http://127.0.0.1:18080"
#: The identity provider, and the realm the acceptance subjects live in. Written as two
#: literals with the token URL derived, rather than the token URL hardcoded beside a
#: second hardcoded realm name: the realm appears in every admin path a revocation case
#: takes, and a rename that reached one and not the other would revoke a subject in a
#: realm the platform does not read -- which looks, from the case's side, exactly like a
#: revocation that had no effect.
KEYCLOAK_URL = "http://127.0.0.1:8090"
KEYCLOAK_REALM = "servicemind"
KEYCLOAK_TOKEN_URL = f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token"
KEYCLOAK_CLIENT_ID = "servicemind-api"

#: Which password each subject authenticates with. Names only -- the values are read from
#: ``deploy/glpi/.env`` at call time and never leave the process.
SUBJECT_PASSWORD_VARS = {
    "globex-analyst-g3": "GLOBEX_ANALYST_G3_PASSWORD",
    "globex-analyst-g4": "GLOBEX_ANALYST_G4_PASSWORD",
    "globex-analyst-nogroup": "GLOBEX_ANALYST_NOGROUP_PASSWORD",
    "globex-approver": "GLOBEX_APPROVER_PASSWORD",
    "acme-analyst": "ACME_ANALYST_PASSWORD",
}

#: Statuses a run rests at. ``pending``/``running`` are the only ones worth waiting past,
#: and ``waiting_*`` are resting places: a run resting in waiting is not a run that is
#: late, so the two must not share a timeout.
SETTLED = {"succeeded", "failed", "cancelled", "waiting_approval", "waiting_review"}

#: How long a case that asks to re-read after the run has gone still must actually wait
#: before re-reading. A "still pending" step that read immediately would be satisfied by a
#: write that had not happened *yet*, which is the opposite of what the step observes.
STILLNESS_SECONDS = 20.0

#: The resting place every case that goes on to address an approval has to find its run in.
#:
#: "The reviewer abstained" is a correct answer -- the evidence did not support the claims
#: -- but it is a terminal one: no action is derived, so the steps that follow have nothing
#: to approve, nothing to revoke and nothing to tamper with. Judging the case on such a run
#: would either blame the platform for the reviewer's refusal or report a permission check
#: that never ran, and neither is a statement about what the case exists to observe. The
#: boundary is therefore asked for again rather than assumed, and how often it had to be
#: asked for is recorded: see ``CaseRun.seek_approval_boundary``.
APPROVAL_BOUNDARY_STATUS = "waiting_approval"

#: How many submissions a case may spend asking for that boundary. The abstention is a
#: property of the model's reading of the evidence and not of the platform, so a single
#: run is a coin toss on a case whose whole subject is what happens at the approval; three
#: is enough that a case is not reported unobservable because one sample went the other way.
APPROVAL_BOUNDARY_ATTEMPTS = 3

#: The Playwright image pinned to the major the frontend declares, so the probe's browser
#: is not whatever the host happens to have. This is not a convenience: the host's own
#: node is v14 and the console's toolchain does not run on it.
PLAYWRIGHT_IMAGE = "mcr.microsoft.com/playwright:v1.63.0-noble"
MCP_PROTOCOL_VERSION = "2026-07-28"
GOVERNED_EXECUTION_EXTENSION = "com.servicemind/governed-execution"

# ----------------------------------------------------------------------------- steps

#: What each ``GET /runs/{run_id}`` step is for. The action string says only that it is a
#: read; the step's id says which read, and the driver refuses an id it does not know.
RUN_READ_STEPS = {
    "observe-terminal": "settle",
    "observe-pending": "settle",
    # After a withdrawal the run is expected to come to rest on a *new* approval, so
    # this settles like any other observation -- and in doing so it re-reads the intent,
    # which is what makes the next approve step address the re-derived action rather
    # than the one that was voided.
    "observe-rederived": "settle",
    "observe-still-pending": "stillness",
    # Read *inside* the outage rather than after it: what the pause left behind is a
    # question about the moment the approval was refused, and a read taken after the
    # service came back answers a different question. ``once`` because there is nothing
    # to settle for -- the run is meant to be exactly where it already was.
    "observe-during-outage": "once",
    "observe-a": "settle",
    "observe-b": "settle",
    "own-read": "once",
    "foreign-read": "once",
}

#: The step that submitted each observe step's run. A case may submit more than one run
#: (ACC-12b needs two, because the platform only proposes a cross-ticket procedure once
#: two *different* tickets carry the same pattern key), so "the current run" is not a
#: single value and an observe step has to say which submission it is observing.
OBSERVE_TARGET = {"observe-a": "submit-a", "observe-b": "submit-b"}

#: What each approval step asks for. ``tamper-approval`` is the only one whose hash is
#: deliberately wrong: it is the conflict that case is about.
APPROVAL_STEPS = {
    "tamper-approval": ("approved", "tamper"),
    "attempt-approval": ("approved", "as-is"),
    "approve": ("approved", "as-is"),
    "repeat-approval": ("approved", "as-is"),
    "decline-approval": ("rejected", "as-is"),
    # The revocation cases approve a correctly-hashed action that the requester is no
    # longer entitled to have executed. A separate id from ``approve`` is what keeps the
    # refusal from being forgiven everywhere: ACC-10b and ACC-11 approve the same way
    # and a 409 there is the failure, not the observation.
    "revoked-approval": ("approved", "as-is"),
    "outage-approval": ("approved", "as-is"),
}

#: Steps whose refusal is the observation rather than a driver error. Kept as data so the
#: driver does not have to read the case's expectations to know whether to give up.
REFUSAL_IS_EXPECTED = {
    "tamper-approval": {409},
    "attempt-approval": {409},
    "repeat-approval": {409},
    "revoked-approval": {409},
    "outage-approval": {409},
}

#: Steps after which the case's memory rows are read. ACC-12b's two assertions are two
#: moments of one row -- quarantined after the run, active after a human activated it --
#: and a single end-of-case read would lose the first.
MEMORY_SNAPSHOT_STEPS = {"observe-a", "observe-b", "activate-memory"}

#: What each ``KEYCLOAK`` step does, keyed by step id for the same reason the run-read
#: steps are: the action string says only that this is an identity-provider edit, and an
#: id the driver does not recognise is refused rather than guessed at.
#:
#: The revocation pairs are deliberately two steps rather than one step that reverses
#: itself. A single step that revokes, observes and restores would leave a case that
#: dies mid-way with a realm that stays revoked -- and the next case's premise would be
#: whatever the last failure left behind, which is the one condition under which a
#: revocation case passes without anything having been revoked.
KEYCLOAK_STEPS = {
    "revoke-entitlement": "revoke",
    "restore-entitlement": "restore",
    "verifier-outage-on": "outage-on",
    "verifier-outage-off": "outage-off",
}

#: Where the verifier is pointed while the outage is in effect. Nothing listens here, so
#: the failure is a refused connection rather than a wrong answer: an endpoint that
#: answers 500 would be a *different* case, about an identity provider that is up and
#: broken.
VERIFIER_OUTAGE_URL = "http://127.0.0.1:1"

#: The application setting the outage overrides, and the unit it is overridden on. The
#: override is an environment variable on the systemd user manager, which outranks the
#: ``.env`` the process reads -- so the outage is undone by unsetting it, and the
#: deployment returns to exactly the configuration the other cases ran under.
VERIFIER_URL_SETTING = "SERVICEMIND_KEYCLOAK_ADMIN_URL"
API_UNIT = "servicemind-api"


@dataclass(frozen=True, slots=True)
class Moment:
    """When a step began, on both clocks a step needs.

    A duration must come from ``time.monotonic`` -- that is the clock that cannot go
    backwards -- while a record that a human reads must carry a wall time. Taking one
    reading and using it for both is what put ``1970-03-04`` into the first replay: the
    monotonic counter is not an epoch, and rendering it as a date produces a timestamp
    that is precise, plausible-looking and false.
    """

    wall: datetime
    monotonic: float


def now() -> Moment:
    return Moment(wall=datetime.now(tz=UTC), monotonic=time.monotonic())


@contextlib.contextmanager
def _password_env_file(variable: str, value: str) -> Iterator[Path]:
    """One variable, in a file docker can read and nobody else can, removed afterwards."""
    handle, name = tempfile.mkstemp(prefix="servicemind-acceptance-", suffix=".env")
    path = Path(name)
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(f"{variable}={value}\n")
        yield path
    finally:
        path.unlink(missing_ok=True)


def _tamper(action_hash: str) -> str:
    """One character changed, and not to a value it already had.

    A 409 has to come from the comparison the case is about. Editing the hash into a
    different *shape* would be refused by validation instead, which is a different
    refusal and would read as a pass.
    """
    head = action_hash[:-1]
    last = "0" if action_hash[-1] != "0" else "1"
    return head + last


#: How long before a token's own expiry the driver stops using it. Not zero, because a
#: request sent one second before expiry can arrive after it, and a refresh is cheap.
TOKEN_REFRESH_MARGIN_SECONDS = 30.0


def _run(*argv: str) -> str:
    """One external command, with its failure carrying the command that produced it."""
    completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            f"{' '.join(argv)} exited {completed.returncode}: "
            f"{(completed.stderr or completed.stdout).strip()[:400]}"
        )
    return completed.stdout


#: The account fields echoed back on every write to a user.
#:
#: Keycloak's user update replaces the representation it is sent: a PUT carrying only
#: ``attributes`` does not merge, it nulls out every field the body omitted. Sending
#: ``{"enabled": false}`` therefore also clears ``email``/``firstName``/``lastName``, and
#: the account stops being able to log in at all -- ``invalid_grant: Account is not fully
#: set up``. That would be a revocation that revokes strictly more than the case says it
#: does, and every assertion after it would hold for a reason the case is not testing.
#: The fix is read-modify-write: echo the account back with only the managed field changed.
ACCOUNT_FIELDS = (
    "username",
    "email",
    "firstName",
    "lastName",
    "emailVerified",
    "enabled",
    "attributes",
    "requiredActions",
    "disableableCredentialTypes",
    "notBefore",
)


def _representation(account: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """The account as Keycloak returned it, with ``changes`` applied on top."""
    body = {name: account[name] for name in ACCOUNT_FIELDS if name in account}
    body.update(changes)
    return body


class Entitlements:
    """The identity provider as something the driver changes, and changes back.

    Only the revocation cases edit the realm, and there the edit is the *input* to the
    observation rather than a side effect of it. Two properties matter more than
    convenience.

    The snapshot is taken immediately before the change, so restoring puts back what was
    there rather than what the case list expects to be there. A restore driven by
    expectation would silently repair a realm that had already drifted, and every case
    after it would run on a premise nobody had checked -- the failure mode where the
    acceptance harness becomes the thing that keeps the acceptance premise true.

    And a subject may only be revoked while it has no unconsumed snapshot. Two revocations
    stacked on one subject would make the second snapshot a record of the already-revoked
    state, so the second restore would put back the revocation and the case would report a
    clean run over a realm it had quietly broken.
    """

    def __init__(self, stack: Stack) -> None:
        self._stack = stack
        self._token: tuple[str, float] | None = None
        self._snapshots: dict[str, dict[str, Any]] = {}

    @property
    def realm(self) -> str:
        return self._stack.tenant_realm

    async def _admin_headers(self) -> dict[str, str]:
        now_mono = time.monotonic()
        if self._token is not None and self._token[0] > now_mono:
            return {"Authorization": f"Bearer {self._token[1]}"}
        username = self._stack.secret("KEYCLOAK_ADMIN_USERNAME")
        password = self._stack.secret("KEYCLOAK_ADMIN_PASSWORD")
        response = await self._stack.client.post(
            f"{self._stack.keycloak_url}/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": username,
                "password": password,
            },
        )
        response.raise_for_status()
        body = response.json()
        self._token = (
            now_mono + max(float(body.get("expires_in", 60.0)) - TOKEN_REFRESH_MARGIN_SECONDS, 1.0),
            str(body["access_token"]),
        )
        return {"Authorization": f"Bearer {self._token[1]}"}

    async def _call(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = {**kwargs.pop("headers", {}), **await self._admin_headers()}
        response = await self._stack.client.request(
            method, f"{self._stack.keycloak_url}{path}", headers=headers, **kwargs
        )
        response.raise_for_status()
        return response

    async def user(self, username: str) -> dict[str, Any]:
        """The account, by exact username. Zero and two matches are both refusals.

        Refused rather than picked, for the same reason the reconcile command refuses:
        a realm holding two accounts under one service name is one this driver has no
        business editing, and choosing either would make the revocation apply to a
        subject nobody named.
        """
        found = (
            await self._call(
                "GET",
                f"/admin/realms/{self.realm}/users",
                params={"username": username, "exact": "true"},
            )
        ).json()
        if len(found) != 1:
            raise RuntimeError(
                f"the realm holds {len(found)} accounts named {username!r}; a revocation "
                f"cannot say which one it revoked"
            )
        return found[0]

    async def snapshot(self, username: str) -> dict[str, Any]:
        if username in self._snapshots:
            raise RuntimeError(
                f"{username!r} was revoked without being restored; a second snapshot "
                f"would record the revoked state as the state to return to"
            )
        account = await self.user(username)
        roles = (
            await self._call(
                "GET", f"/admin/realms/{self.realm}/users/{account['id']}/role-mappings/realm"
            )
        ).json()
        snapshot = {
            "account": account,
            "attributes": dict(account.get("attributes") or {}),
            "roles": [{"id": str(role["id"]), "name": str(role["name"])} for role in roles],
        }
        self._snapshots[username] = snapshot
        return snapshot

    async def revoke(self, username: str, kind: str, value: int | str | None) -> str:
        """Take one coordinate away, and return a description of what was taken."""
        if kind not in {"disable", "role", "group", "entity"}:
            # Refused rather than defaulted. The fall-through below is the entity
            # attribute, so an unset or misspelled kind would silently narrow the wrong
            # coordinate -- and the case would then observe a scope that moved, for a
            # reason other than the one it says it is testing.
            raise RuntimeError(f"unrecognised revocation kind {kind!r}")
        taken = await self.snapshot(username)
        account = taken["account"]
        user_id = str(account["id"])
        if kind == "disable":
            await self._call(
                "PUT",
                f"/admin/realms/{self.realm}/users/{user_id}",
                json=_representation(account, enabled=False),
            )
            return f"disabled {username}"
        if kind == "role":
            match = [role for role in taken["roles"] if role["name"] == value]
            if not match:
                raise RuntimeError(f"{username!r} does not hold the realm role {value!r}")
            await self._call(
                "DELETE",
                f"/admin/realms/{self.realm}/users/{user_id}/role-mappings/realm",
                json=match,
            )
            return f"removed realm role {value!r} from {username}"
        attribute = "glpi_group_ids" if kind == "group" else "glpi_entity_ids"
        current = [str(item) for item in taken["attributes"].get(attribute, [])]
        wanted = str(value)
        if wanted not in current:
            raise RuntimeError(
                f"{username!r} does not hold {attribute}={wanted}; current {current}. A "
                f"revocation of a grant that was not held narrows nothing, and the case "
                f"after it would observe a scope that never moved."
            )
        attributes = dict(taken["attributes"])
        attributes[attribute] = [item for item in current if item != wanted]
        await self._call(
            "PUT",
            f"/admin/realms/{self.realm}/users/{user_id}",
            json=_representation(account, attributes=attributes),
        )
        return f"removed {attribute}={wanted} from {username}"

    async def restore(self, username: str) -> str:
        snapshot = self._snapshots.pop(username, None)
        if snapshot is None:
            raise RuntimeError(f"{username!r} has no snapshot to restore from")
        account = snapshot["account"]
        user_id = str(account["id"])
        # Read back before writing, and send that back with only the two fields this
        # command is responsible for changed. Echoing the snapshot wholesale would also
        # undo anything else that moved while the account was revoked -- which is a
        # restore that repairs more than it broke, and hides the very drift the next
        # case's premise depends on.
        current = await self.user(username)
        await self._call(
            "PUT",
            f"/admin/realms/{self.realm}/users/{user_id}",
            json=_representation(
                current,
                enabled=bool(account.get("enabled", True)),
                attributes=dict(account.get("attributes") or {}),
            ),
        )
        present = (
            await self._call(
                "GET", f"/admin/realms/{self.realm}/users/{user_id}/role-mappings/realm"
            )
        ).json()
        held = {str(role["name"]) for role in present}
        missing = [role for role in snapshot["roles"] if role["name"] not in held]
        if missing:
            await self._call(
                "POST",
                f"/admin/realms/{self.realm}/users/{user_id}/role-mappings/realm",
                json=missing,
            )
        return (
            f"restored {username}: enabled={bool(account.get('enabled', True))}, "
            f"attributes={json.dumps(snapshot['attributes'], sort_keys=True)}, "
            f"{len(missing)} role(s) re-added"
        )

    async def has_unrestored(self) -> list[str]:
        return sorted(self._snapshots)


class Stack:
    """Everything the driver reaches the platform through, and the secrets it needs."""

    def __init__(self, *, base_url: str, tenant_id: UUID, timeout_scale: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.tenant_id = tenant_id
        self.timeout_scale = timeout_scale
        self._secrets = dotenv_values(DEPLOY_ENV)
        #: Token and the monotonic instant it stops being usable, per subject. A bare
        #: cached string is what made the first full sweep stop observing halfway
        #: through: the access token outlives a couple of cases, not a whole run of
        #: them, and once it expired every case from ACC-11 on read ``401 Invalid or
        #: expired access token`` -- an answer about the driver's clock that looks,
        #: downstream, like a platform refusing a legitimate caller.
        self._tokens: dict[str, tuple[str, float]] = {}
        self._claims: dict[str, dict[str, Any]] = {}
        self.client = httpx.AsyncClient(timeout=300, trust_env=False)
        self.entitlements = Entitlements(self)
        #: Whether this sweep has left the serving process pointed away from the identity
        #: provider. Cleared by the restore path, and read by the cleanup that runs even
        #: when a case dies mid-outage.
        self.outage_active = False

    async def aclose(self) -> None:
        await self.client.aclose()
        await self.end_outage()

    @property
    def keycloak_url(self) -> str:
        return KEYCLOAK_URL

    @property
    def tenant_realm(self) -> str:
        return KEYCLOAK_REALM

    def secret(self, name: str) -> str:
        """A deployment secret by variable name. The value never leaves the process."""
        value = self._secrets.get(name)
        if not value:
            raise RuntimeError(f"{name} is not set in {DEPLOY_ENV}")
        return str(value)

    # -------------------------------------------------------------------- outage

    async def await_health(self, seconds: float = 120.0) -> None:
        """Wait for the serving process to answer again after a restart.

        Polled rather than slept, because the interval is a property of the process and
        not of the driver, and a fixed sleep that is long enough today is a case that
        reports a connection error as a platform behaviour tomorrow.
        """
        deadline = time.monotonic() + seconds
        last = "no attempt"
        while time.monotonic() < deadline:
            try:
                response = await self.client.get(f"{self.base_url}/health")
                if response.status_code == 200:
                    return
                last = f"status {response.status_code}"
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                last = type(exc).__name__
            await asyncio.sleep(1.0)
        raise RuntimeError(f"the API did not come back within {seconds}s after a restart: {last}")

    async def start_outage(self) -> str:
        """Make the verifier's admin endpoint unreachable for the serving process.

        The override is an environment variable on the systemd user manager, which
        outranks the ``.env`` file the process also reads -- so this changes what the
        process loads without editing a file, and undoing it is an unset. The unit is
        restarted because the setting is read at import; nothing else about the
        deployment moves, and in particular JWT verification uses a different setting,
        so the platform still authenticates callers while it cannot answer the question
        the resume boundary asks.
        """
        self._systemctl("set-environment", f"{VERIFIER_URL_SETTING}={VERIFIER_OUTAGE_URL}")
        self._systemctl("restart", API_UNIT)
        self.outage_active = True
        await self.await_health()
        return f"{VERIFIER_URL_SETTING}={VERIFIER_OUTAGE_URL} on {API_UNIT}, then restarted"

    async def end_outage(self) -> str | None:
        """Put the deployment back. Idempotent, and safe to call when nothing is wrong."""
        if not self.outage_active:
            return None
        self._systemctl("unset-environment", VERIFIER_URL_SETTING)
        self._systemctl("restart", API_UNIT)
        self.outage_active = False
        await self.await_health()
        return f"{VERIFIER_URL_SETTING} unset on {API_UNIT}, then restarted"

    @staticmethod
    def _systemctl(*argv: str) -> None:
        _run("systemctl", "--user", *argv)

    # ------------------------------------------------------------------- identity

    def password(self, subject: str) -> str:
        variable = SUBJECT_PASSWORD_VARS.get(subject)
        if variable is None:
            raise RuntimeError(f"no password variable is declared for subject {subject!r}")
        value = self._secrets.get(variable)
        if not value:
            raise RuntimeError(f"{variable} is not set in deploy/glpi/.env")
        return str(value)

    async def token(self, subject: str, *, force: bool = False) -> str:
        cached = self._tokens.get(subject)
        if not force and cached is not None and time.monotonic() < cached[1]:
            return cached[0]
        response = await self.client.post(
            KEYCLOAK_TOKEN_URL,
            data={
                "grant_type": "password",
                "client_id": KEYCLOAK_CLIENT_ID,
                "username": subject,
                "password": self.password(subject),
            },
        )
        response.raise_for_status()
        payload = response.json()
        lifetime = float(payload.get("expires_in", 60.0))
        self._tokens[subject] = (
            str(payload["access_token"]),
            time.monotonic() + max(0.0, lifetime - TOKEN_REFRESH_MARGIN_SECONDS),
        )
        return self._tokens[subject][0]

    async def request(self, subject: str, method: str, url: str, **kwargs) -> httpx.Response:
        """One authenticated call, re-minted once if the platform rejects the token.

        The clock above is the ordinary path. This covers the case the clock cannot see --
        a token revoked or invalidated between two calls, or a token minted elsewhere --
        and it is deliberately one retry: a second 401 after a freshly minted token is an
        authorization answer, and retrying would only delay the observation that says so.
        """
        headers = {**kwargs.pop("headers", {}), **await self.headers(subject)}
        response = await self.client.request(method, url, headers=headers, **kwargs)
        if response.status_code != 401:
            return response
        headers = {**headers, **await self.headers(subject, force=True)}
        return await self.client.request(method, url, headers=headers, **kwargs)

    def claims(self, subject: str) -> dict[str, Any]:
        """The token's own claims, decoded without re-verifying what Keycloak signed.

        The acceptance records what the platform *was told* about the subject, so that a
        pass cannot rest on the case list's idea of who ran it.
        """
        if subject not in self._claims:
            import jwt

            token, _ = self._tokens[subject]
            self._claims[subject] = jwt.decode(token, options={"verify_signature": False})
        return self._claims[subject]

    async def observed_subject(self, subject: str) -> ObservedSubject:
        await self.token(subject)
        claims = self.claims(subject)
        return ObservedSubject(
            username=str(claims.get("preferred_username", claims["sub"])),
            tenant_id=UUID(str(claims["tenant_id"])),
            roles=frozenset(claims.get("realm_access", {}).get("roles", [])),
            entity_ids=frozenset(int(value) for value in claims.get("glpi_entity_ids", [])),
            group_ids=frozenset(int(value) for value in claims.get("glpi_group_ids", [])),
        )

    async def headers(self, subject: str, *, force: bool = False) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self.token(subject, force=force)}"}

    async def context(self, subject: str) -> TenantContext:
        """A ``TenantContext`` for in-process reads, from the same token the calls use.

        Asynchronous because it mints the token if it has not been minted yet. Reading the
        cached claims directly assumed some earlier call had already authenticated this
        subject, which holds in a whole-sweep run and fails the moment a case is run on its
        own -- and running one case on its own is how a case gets debugged.
        """
        await self.token(subject)
        claims = self.claims(subject)
        return TenantContext(
            tenant_id=UUID(str(claims["tenant_id"])),
            user_id=str(claims["sub"]),
            username=str(claims.get("preferred_username", claims["sub"])),
            roles=set(claims.get("realm_access", {}).get("roles", [])),
            allowed_glpi_entity_ids={int(value) for value in claims.get("glpi_entity_ids", [])},
            allowed_glpi_group_ids={int(value) for value in claims.get("glpi_group_ids", [])},
        )

    # ------------------------------------------------------------------- platform

    async def create_run(
        self, subject: str, *, ticket_id: int, goal: str, request_write: bool
    ) -> httpx.Response:
        return await self.request(
            subject,
            "POST",
            f"{self.base_url}/v1/servicemind/runs",
            json={"ticket_id": ticket_id, "goal": goal, "request_write": request_write},
        )

    async def get_run(self, subject: str, run_id: UUID) -> httpx.Response:
        return await self.request(subject, "GET", f"{self.base_url}/v1/servicemind/runs/{run_id}")

    async def approve(
        self, subject: str, run_id: UUID, *, decision: str, action_hash: str, comment: str
    ) -> httpx.Response:
        return await self.request(
            subject,
            "POST",
            f"{self.base_url}/v1/servicemind/runs/{run_id}/approval",
            json={
                "decision": decision,
                "expected_action_hash": action_hash,
                "comment": comment,
            },
        )

    async def resolve_review(
        self, subject: str, run_id: UUID, *, decision: str, comment: str
    ) -> httpx.Response:
        """Answer a review escalation, as the reviewer it was routed to answers it.

        An escalated run is not a dead end and not a failure: the platform's own
        ``finalize_node`` records ``continue`` as *accepting the reviewer's blocked
        outcome*, leaves the reviewer's ``escalate`` untouched in the result, persists the
        human's answer beside it, and emits a distinct event so a human-resolved
        escalation stays countable. A batch that stops at ``waiting_review`` therefore
        measures the platform's silence rather than its behaviour -- it observes the state
        the escalation was *in*, never the outcome the platform documents for it.
        """
        return await self.request(
            subject,
            "POST",
            f"{self.base_url}/v1/servicemind/runs/{run_id}/review-resolution",
            json={"decision": decision, "comment": comment},
        )

    async def settle(self, subject: str, run_id: UUID, deadline: float) -> httpx.Response:
        """Poll until the run stops moving, and return the last response seen.

        The deadline is an argument rather than an internal retry count so the case
        list's own budget is the thing that decides when a run is late.
        """
        response = await self.get_run(subject, run_id)
        while time.monotonic() < deadline:
            payload = response.json() if response.status_code == 200 else {}
            if payload.get("status") in SETTLED:
                return response
            await asyncio.sleep(1.0)
            response = await self.get_run(subject, run_id)
        return response

    async def timeline_events(self, subject: str, run_id: UUID) -> list[str]:
        """The run's event log, which this endpoint replays and then closes."""
        response = await self.request(
            subject, "GET", f"{self.base_url}/v1/servicemind/runs/{run_id}/events"
        )
        if response.status_code != 200:
            return []
        return [
            line.removeprefix("event: ").strip()
            for line in response.text.splitlines()
            if line.startswith("event: ")
        ]

    async def followups(self, subject: str, ticket_id: int) -> list[ObservedFollowup]:
        """GLPI read back through the product's own client, as the case's subject."""
        config = await resolve_glpi_config(await self.context(subject))
        async with GlpiClient(config) as glpi:
            rows = await glpi.list_ticket_followups(ticket_id)
        return [
            ObservedFollowup(
                followup_id=int(row.id),
                content_raw=str(row.content),
                content_text=html_to_text(str(row.content)),
            )
            for row in rows
        ]


# ------------------------------------------------------------------------- execution


@dataclass
class EvidenceView:
    """The persisted result parsed once, in the platform's own models.

    Parsed once because several assertions read it, and separate parses could disagree:
    a row that fails validation would be noted once per reader, and those notes would be
    the only trace that the readers saw different sets.
    """

    evidence: list[Evidence] = field(default_factory=list)
    analysis: AnalysisResult | None = None
    review: ReviewResult | None = None
    citations: list[Citation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class CaseRun:
    """One case's execution, with the bookkeeping the steps share."""

    def __init__(self, case: AcceptanceCase, stack: Stack, tickets: dict[str, int]) -> None:
        self.case = case
        self.stack = stack
        self.tickets = tickets
        self.steps: list[StepObservation] = []
        self.errors: list[str] = []
        self.notes: list[str] = []
        #: Run ids by the id of the step that created them, and the most recent one.
        self.runs: dict[str, UUID] = {}
        self.run_id: UUID | None = None
        self.status: str | None = None
        self.total_seconds: float | None = None
        self.submitted_at: Moment | None = None
        self.intent: ObservedActionIntent | None = None
        self.before: list[ObservedFollowup] = []
        self.after: list[ObservedFollowup] = []
        self.memory: list[ObservedMemoryRecord] = []
        self.graph: list[ObservedGraphReading] = []
        self.audit: list[str] = []
        self.timeline: list[str] = []
        self.manifest: list[dict[str, Any]] = []
        self.foreign_status: int | None = None
        self.listed_own: bool | None = None
        self.result: dict[str, Any] = {}
        self._baseline_taken = False
        #: Runs submitted only to reach an approval boundary, after an earlier submission
        #: rested somewhere the case could not use. Kept so the retry is a visible part of
        #: the observation rather than a silent second try.
        self.boundary_attempts: list[str] = []
        #: Who the case submitted *as*. A run's requester is written into the run, so a
        #: resubmission that changed it would be a different run about a different person
        #: -- see ``seek_approval_boundary``.
        self.submitted_as: str | None = None

    # ------------------------------------------------------------- step plumbing

    def ticket_id(self, step: CaseStep) -> int:
        reference = step.ticket_ref or self.case.ticket_ref
        if reference is None:
            raise RuntimeError(f"{self.case.id}/{step.id}: the step names no ticket")
        if reference not in self.tickets:
            raise RuntimeError(f"{self.case.id}/{step.id}: no resolved ticket for {reference!r}")
        return self.tickets[reference]

    def case_tickets(self) -> list[int]:
        references = {self.case.ticket_ref} | {
            step.ticket_ref for step in self.case.steps if step.ticket_ref
        }
        return sorted(self.tickets[ref] for ref in references if ref in self.tickets)

    def target_run(self, step: CaseStep) -> UUID | None:
        """The run this step acts on: a named submission's, or the most recent one."""
        source = OBSERVE_TARGET.get(step.id)
        return self.runs.get(source) if source else self.run_id

    async def snapshot_followups(self, subject: str) -> list[ObservedFollowup]:
        collected: list[ObservedFollowup] = []
        for ticket_id in self.case_tickets():
            collected.extend(await self.stack.followups(subject, ticket_id))
        return collected

    async def take_baseline(self, subject: str) -> None:
        """Record the tickets as they stood before this case changed anything.

        Taken once, before the first mutating step, and compared by id rather than by
        count: the assertions are set differences, and a count would report a run that
        added one followup and had another deleted as having written nothing.
        """
        if self._baseline_taken:
            return
        self._baseline_taken = True
        self.before = await self.snapshot_followups(subject)

    def observe_payload(self, payload: dict[str, Any]) -> None:
        self.status = str(payload.get("status"))
        if isinstance(payload.get("result"), dict):
            self.result = payload["result"]
        intent = self._intent_of(payload)
        if intent is not None:
            self.intent = ObservedActionIntent(
                action_hash=str(intent["action_hash"]),
                status=str(intent["status"]),
                action_type=str(intent["action_type"]),
                target_id=int(intent["target_id"]),
                arguments=dict(intent.get("arguments") or {}),
                evidence_refs=[str(ref) for ref in intent.get("evidence_refs") or []],
                requested_by=(
                    str(intent["requested_by"]) if intent.get("requested_by") is not None else None
                ),
            )

    @staticmethod
    def _intent_of(payload: dict[str, Any]) -> dict[str, Any] | None:
        intent = payload.get("action_intent")
        return intent if isinstance(intent, dict) else None

    def step(
        self,
        step: CaseStep,
        *,
        started: Moment,
        http_status: int | None = None,
        payload: dict[str, Any] | None = None,
        outcome: str | None = None,
        detail: str | None = None,
    ) -> None:
        intent = self._intent_of(payload or {})
        self.steps.append(
            StepObservation(
                id=step.id,
                description=step.description,
                action=step.action,
                started_at=started.wall,
                elapsed_seconds=max(0.0, time.monotonic() - started.monotonic),
                http_status=http_status,
                action_hash=str(intent["action_hash"]) if intent else None,
                action_status=str(intent["status"]) if intent else None,
                outcome=outcome,
                detail=detail,
            )
        )

    # --------------------------------------------------------------- the actions

    async def submit(self, step: CaseStep, subject: str) -> None:
        await self.take_baseline(subject)
        self.submitted_as = subject
        started = now()
        response = await self.stack.create_run(
            subject,
            ticket_id=self.ticket_id(step),
            goal=self.case.question,
            request_write=self.case.request_write,
        )
        payload = response.json() if response.status_code < 300 else {}
        if response.status_code < 300:
            run_id = UUID(str(payload["id"]))
            self.runs[step.id] = run_id
            self.run_id = run_id
            self.submitted_at = started
            self.observe_payload(payload)
        else:
            self.errors.append(
                f"{self.case.id}/{step.id}: run creation returned {response.status_code} "
                f"{response.text[:400]}"
            )
        self.step(step, started=started, http_status=response.status_code, payload=payload)

    async def seek_approval_boundary(self, step: CaseStep, subject: str) -> None:
        """Submit again while the run rests somewhere this case cannot use.

        Called when the run a case addresses came to rest having derived no action -- the
        Reviewer abstained, and abstention is terminal by design. Nothing was decided and
        nothing was attempted, so there is no earlier observation to invalidate and
        nothing the following steps could act on; the case simply never reached the
        question it exists to ask. Each abandoned submission is recorded as its own step,
        which is what keeps this from being a silent retry: the replay says how many runs
        it took to reach the boundary, and says it in the same place as everything else.

        Only reached when no action intent was derived. A run that did derive one and
        still is not at the boundary is a different situation -- there is a real action to
        account for -- and is left alone for its own assertions to judge.

        The resubmission is made as the *requester* of the run it replaces, not as the
        case's default subject. Those are different people whenever a case submits with
        ``as_subject``: the case runs as the approver and the run belongs to the analyst.
        Approving runs is what the approver does, so a retry made in their name reaches
        the boundary -- and every later step of such a case then addresses a run about the
        approver. On ACC-20 (2026-09-23) exactly that happened: the retry was submitted as
        ``globex-approver``, the case revoked ``analyst`` from ``globex-analyst-g3``, and
        the revoked run was never the one being approved, so the approval succeeded and
        wrote to the ticket. The case passed its own premise without the platform being
        asked the question it exists to ask, which is the one failure mode a retry must
        never have.
        """
        requester = self.submitted_as or subject
        while (
            len(self.boundary_attempts) < APPROVAL_BOUNDARY_ATTEMPTS - 1
            and self.status in SETTLED
            and self.status != APPROVAL_BOUNDARY_STATUS
            and self.intent is None
        ):
            attempt = len(self.boundary_attempts) + 1
            started = now()
            attempt_step = step.model_copy(
                update={
                    "id": f"{step.id}~attempt-{attempt}",
                    "description": (
                        f"第 {attempt} 次提交：上一次运行停在 {self.status!r}、没有派生动作，"
                        "本案例的后续步骤没有可处理的对象，因此重新提交以取得审批边界"
                    ),
                }
            )
            response = await self.stack.create_run(
                requester,
                ticket_id=self.ticket_id(step),
                goal=self.case.question,
                request_write=self.case.request_write,
            )
            payload = response.json() if response.status_code < 300 else {}
            if response.status_code >= 300:
                self.step(
                    attempt_step,
                    started=started,
                    http_status=response.status_code,
                    payload=payload,
                    detail=response.text[:400],
                )
                self.errors.append(
                    f"{self.case.id}/{step.id}: the run rested at {self.status!r} with no "
                    f"action to address, and resubmitting for an approval boundary "
                    f"returned {response.status_code}"
                )
                return
            run_id = UUID(str(payload["id"]))
            settled = await self.stack.settle(
                requester,
                run_id,
                started.monotonic + self.case.timeout_seconds * self.stack.timeout_scale,
            )
            settled_payload = settled.json() if settled.status_code == 200 else {}
            self.boundary_attempts.append(str(run_id))
            # The abandoned run's identity is replaced whole rather than merged:
            # ``observe_payload`` only overwrites the intent when the new payload carries
            # one, so an intent left over from an earlier run could otherwise be read as
            # this run's -- an approval addressed to an action that no longer exists.
            self.intent = None
            if settled.status_code == 200:
                self.observe_payload(settled_payload)
            else:
                self.status = None
            self.runs[step.id] = run_id
            self.run_id = run_id
            self.submitted_at = started
            self.total_seconds = time.monotonic() - started.monotonic
            self.step(
                attempt_step,
                started=started,
                http_status=settled.status_code,
                payload=settled_payload,
                detail=(
                    f"resubmitted run {run_id} to reach an approval boundary; it rested "
                    f"at {self.status!r} after {time.monotonic() - started.monotonic:.1f}s"
                ),
            )
        if self.status != APPROVAL_BOUNDARY_STATUS and self.intent is None:
            self.errors.append(
                f"{self.case.id}/{step.id}: the run never reached an approval boundary; "
                f"{len(self.boundary_attempts) + 1} submission(s) rested at "
                f"{self.status!r} with no action to address"
            )

    async def read_run(self, step: CaseStep, subject: str) -> None:
        run_id = self.target_run(step)
        if run_id is None:
            self.errors.append(f"{self.case.id}/{step.id}: no run to read")
            return
        mode = RUN_READ_STEPS[step.id]
        started = now()
        if mode == "stillness":
            # The stillness precedes the read, not follows it: a write landing during the
            # window has to fall inside the observation, and reading first would leave the
            # wait proving nothing about the moment the case is asking about.
            await asyncio.sleep(STILLNESS_SECONDS * self.stack.timeout_scale)
        if mode in {"settle", "stillness"}:
            deadline = started.monotonic + self.case.timeout_seconds * self.stack.timeout_scale
            response = await self.stack.settle(subject, run_id, deadline)
            if response.status_code == 200 and self.submitted_at is not None:
                self.total_seconds = time.monotonic() - self.submitted_at.monotonic
        else:
            response = await self.stack.get_run(subject, run_id)
        payload = response.json() if response.status_code == 200 else {}
        if response.status_code == 200:
            self.observe_payload(payload)
        if step.id == "foreign-read":
            self.foreign_status = response.status_code
        elif step.id == "own-read":
            self.listed_own = response.status_code == 200
        if mode in {"settle", "stillness"} and self.status not in SETTLED:
            self.errors.append(
                f"{self.case.id}/{step.id}: the run never settled; last status "
                f"{self.status!r} after {time.monotonic() - started.monotonic:.1f}s"
            )
        self.step(
            step,
            started=started,
            http_status=response.status_code,
            payload=payload,
            # The run's own error travels with the status: a case that observed `failed`
            # and nothing else sends its reader to the server log to find out why, and the
            # replay is supposed to be the account of what happened.
            detail=json.dumps(
                {
                    "status": self.status,
                    "elapsed_seconds": round(time.monotonic() - started.monotonic, 1),
                    "error": payload.get("error"),
                    "termination_code": (payload.get("result") or {}).get("termination_code"),
                },
                ensure_ascii=False,
            ),
        )
        if step.id == "observe-pending":
            # ``observe-pending`` is the step that establishes the boundary every later
            # approval, revocation and tamper step addresses, so it is where the case has
            # to notice it is not there. The read above is recorded first and in full: the
            # run that abstained really did rest where the step says it did.
            await self.seek_approval_boundary(step, subject)

    async def approval(self, step: CaseStep, subject: str) -> None:
        if self.run_id is None:
            self.errors.append(f"{self.case.id}/{step.id}: no run to approve")
            return
        if self.intent is None:
            self.errors.append(
                f"{self.case.id}/{step.id}: no action intent was observed, so there is "
                "no hash to approve or to tamper with"
            )
            return
        await self.take_baseline(subject)
        decision, hash_mode = APPROVAL_STEPS[step.id]
        action_hash = (
            _tamper(self.intent.action_hash) if hash_mode == "tamper" else self.intent.action_hash
        )
        started = now()
        response = await self.stack.approve(
            subject,
            self.run_id,
            decision=decision,
            action_hash=action_hash,
            comment=f"P7.6 {self.case.id} {step.id}",
        )
        payload = response.json() if response.status_code == 200 else {}
        if response.status_code < 300:
            self.observe_payload(payload)
        elif response.status_code not in REFUSAL_IS_EXPECTED.get(step.id, set()):
            self.errors.append(
                f"{self.case.id}/{step.id}: approval returned {response.status_code} "
                f"{response.text[:400]}"
            )
        self.step(
            step,
            started=started,
            http_status=response.status_code,
            payload=payload,
            detail=(
                f"decision={decision} hash={'tampered' if hash_mode == 'tamper' else 'as-observed'}"
            ),
        )

    async def read_followups(self, step: CaseStep, subject: str) -> None:
        started = now()
        try:
            self.after = await self.snapshot_followups(subject)
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            self.errors.append(f"{self.case.id}/{step.id}: GLPI read failed: {exc!r}")
            self.step(step, started=started, detail=repr(exc))
            return
        new = new_followups(self.before, self.after)
        if new and not self.case.writes_followups:
            # The case list's isolation rule depends on this being impossible: a
            # non-writing case's ticket is shared with other cases, so a followup it did
            # not declare is a followup the *next* case will read as its own evidence.
            # Reported here, at the moment it is observable, rather than as an unexpected
            # difference in some later case's result.
            self.errors.append(
                f"{self.case.id}/{step.id}: the case is not declared as writing to its "
                f"ticket but {len(new)} followup(s) appeared: "
                f"{[item.followup_id for item in new]}. Its ticket is shared, so the "
                f"fixture is now polluted for every other case using it."
            )
        self.step(
            step,
            started=started,
            detail=f"{len(self.after)} followup(s) after, {len(self.before)} before",
        )

    async def keycloak(self, step: CaseStep, mode: str) -> None:
        """Change or restore the requester's entitlements, or take the verifier offline.

        A failure here is collected as a driver error rather than raised: the *next*
        steps still need to be attempted, because the step after a failed revocation is
        usually its restore, and abandoning the case would leave the realm revoked.
        """
        started = now()
        subject = step.entitlement_subject or self.case.subject
        try:
            if mode == "revoke":
                detail = await self.stack.entitlements.revoke(
                    subject, str(step.entitlement_kind), step.entitlement_value
                )
            elif mode == "restore":
                detail = await self.stack.entitlements.restore(subject)
            elif mode == "outage-on":
                detail = await self.stack.start_outage()
            else:
                detail = await self.stack.end_outage() or "no outage was in effect"
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            self.errors.append(f"{self.case.id}/{step.id}: {exc!r}")
            self.step(step, started=started, outcome="failed", detail=repr(exc)[:2000])
            return
        # ``outcome`` stays unset on success: on a step that is not a probe it means "a
        # probe reached this verdict", and stamping a passed outcome on an edit the case
        # is not asserting would put a verdict in the replay that nothing graded.
        self.step(step, started=started, detail=f"{mode}: {detail}")

    async def graph_read(self, step: CaseStep, subject: str) -> None:
        """One principal's traversal of the graph side channel, through the real retriever."""
        started = now()
        store = build_graph_store()
        if store is None:
            self.errors.append(
                f"{self.case.id}/{step.id}: Graph-RAG is disabled, so the graph reading "
                "this case needs cannot be taken"
            )
            self.step(step, started=started)
            return
        try:
            observed = await self.stack.observed_subject(subject)
            reading = await graph_fixture_reading(
                store,
                principal_for(
                    self.stack.tenant_id,
                    user_id=observed.username,
                    entity_ids=set(observed.entity_ids),
                    group_ids=set(observed.group_ids),
                ),
                ticket_id=self.ticket_id(step),
            )
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            self.errors.append(f"{self.case.id}/{step.id}: graph read failed: {exc!r}")
            self.step(step, started=started, detail=repr(exc))
            return
        finally:
            await store.close()
        self.graph.append(
            ObservedGraphReading(
                subject=observed.username,
                source_record_ids=reading,
                observed_at_step=step.id,
            )
        )
        self.step(step, started=started, detail=f"{observed.username} saw {reading}")

    async def pattern_key_producer(self, step: CaseStep, subject: str) -> None:
        """Evaluate ``_procedure_pattern`` directly on fixed inputs.

        The mechanism is deterministic, so it is judged on its own terms rather than
        through two live model runs that happen to agree: a case that ran the model twice
        and compared would pass on a coincidence and fail on a paraphrase, which measures
        the sampler rather than the grouping rule.

        What "the same root cause" means is the rule the platform actually applies, and
        D15 changed it. The identity is ``classification``, ``recommended_group`` and
        *which* recommendation fields the analysis filled; the recommendation prose is
        deliberately not part of it, because prose moves with the evidence a ticket
        happened to hold rather than with the condition. ACC-12b is the measurement that
        forced this: two structurally isomorphic tickets produced analyses that agreed
        verbatim on classification and owner, and whose ``problem_recommendation``
        differed in *polarity* -- the second ticket had seen one more sibling incident, so
        it proposed a problem record where the first had declined to. Hashing the prose
        asked two independent samples to agree on the one field that is supposed to
        change.

        So the same-root-cause inputs below are the variations the rule must now absorb --
        case and whitespace, a rewritten recommendation, and the flag the model happens to
        set -- and the different-root-cause inputs are the two fields that still carry the
        identity. Weakening one without the other would let the surface collapse to
        "everything groups", which is why both are asserted rather than only the first.
        """
        from servicemind.orchestration.phase5_governance import _procedure_pattern

        started = now()
        rebind = {
            "recurring_incident": True,
            "classification": "mfa_device_binding",
            "recommended_group": "Identity Team",
            "problem_recommendation": "Rebind the authentication device.",
        }
        if step.id == "produce-same":
            variants = [
                rebind,
                {
                    "recurring_incident": True,
                    # Cased and spaced differently: the class of variation normalization
                    # was always meant to absorb.
                    "classification": "  MFA_Device_Binding ",
                    "recommended_group": "identity   team",
                    "problem_recommendation": "Rebind\t the authentication\n  device.",
                },
                {
                    # Same condition, same owner, same field shape. Only the sentence the
                    # model chose is different -- which is the whole of D15. The recurrence
                    # flag is flipped as well, because it is not an input either and a
                    # reader of the record should see that stated rather than assumed.
                    "recurring_incident": False,
                    # Cased differently and nothing else: this input is here to isolate
                    # the rewrite below, so it must not also move a field that is in the
                    # identity. An earlier version of this input said "MFA Device
                    # Binding", which normalizes to a different token than
                    # "mfa_device_binding" and split the key for a reason that had
                    # nothing to do with what the input was testing.
                    "classification": "MFA_Device_Binding",
                    "recommended_group": "Identity Team",
                    "problem_recommendation": (
                        "Re-register the second factor and confirm the binding holds "
                        "before the ticket is closed."
                    ),
                },
            ]
            expectation = "one key for one root cause, across spellings and rewrites"
            keys = [entry[0] if entry else None for entry in map(_procedure_pattern, variants)]
            passed = None not in keys and len(set(keys)) == 1
        elif step.id == "produce-different":
            variants = [
                rebind,
                {
                    "recurring_incident": True,
                    # Only the classification differs. It names the condition, so a
                    # different one is a different condition however the recommendation
                    # reads.
                    "classification": "gateway_connectivity",
                    "recommended_group": "Identity Team",
                    "problem_recommendation": "Rebind the authentication device.",
                },
                {
                    "recurring_incident": True,
                    # Only the owner differs. It decides which team is told to run the
                    # procedure, so merging these would send one team another's work.
                    "classification": "mfa_device_binding",
                    "recommended_group": "Network Team",
                    "problem_recommendation": "Rebind the authentication device.",
                },
            ]
            expectation = "a different root cause produces a different key"
            keys = [entry[0] if entry else None for entry in map(_procedure_pattern, variants)]
            passed = None not in keys and len(set(keys)) == len(variants)
        else:
            raise RuntimeError(f"unknown pattern_key step: {step.id!r}")

        self.step(
            step,
            started=started,
            outcome="passed" if passed else "failed",
            detail=json.dumps(
                {
                    "step": step.id,
                    "expectation": expectation,
                    "classification": [variant["classification"] for variant in variants],
                    "recommended_group": [variant["recommended_group"] for variant in variants],
                    "pattern_keys": keys,
                    # The inputs themselves, so the claim can be re-derived from the
                    # record instead of taken on trust.
                    "inputs": variants,
                },
                ensure_ascii=False,
            ),
        )

    async def context_pruning_producer(self, step: CaseStep, subject: str) -> None:
        """Evaluate ``ContextBuilder.build`` directly on a payload built to exceed it.

        The budget rule is deterministic and lives in one function, so it is judged by
        calling that function rather than by reading the manifest of a live run. The live
        reading was the wrong instrument: the run's payload is assembled from whatever the
        retrieval returned, and on 2026-09-23 ACC-06 measured *not* over budget at every
        round -- its manifest showed no pruning, so a case whose premise is "this payload
        does not fit" was decided by whether some *other* agent's envelope happened to
        bind that time. That is a property of the retrieval, not of the budget rule, and
        it passed and failed for reasons the case does not name.

        The inputs below are fixed and the numbers are derived from them, not from the
        deployment's settings: the reader can re-run the arithmetic from the step's own
        detail. What is asserted is the whole of the rule -- an over-budget payload loses
        a row *and* states why -- because "something was dropped" and "the envelope got
        smaller" are both true of a run that simply failed.
        """
        from servicemind.context.builder import ContextBuilder
        from servicemind.context.contracts import (
            ContextAgent,
            ContextItem,
            ContextSource,
            TrustLabel,
        )

        started = now()
        max_input_tokens = 2000
        system_reserve = 0
        output_reserve = 0
        usable = max_input_tokens - system_reserve - output_reserve
        control = ContextItem(
            item_id="probe-control",
            source=ContextSource.POLICY,
            content="Envelope control row: the run's own task statement.",
            allowed_agents=frozenset({ContextAgent.ANALYSIS}),
            trust=TrustLabel.TRUSTED_CONTROL,
            authority=1.0,
            relevance=1.0,
            required=True,
            provenance_ref="probe://control",
        )
        # Six rows at roughly 800 tokens each against 2000 usable: two fit beside the
        # control row and four cannot. Each row's content is distinct on purpose -- equal
        # content is dropped as ``exact_duplicate``, which is the dedupe rule and not the
        # budget rule, and a payload that trips both would not say which one the case
        # tested. Measured on this payload: control 23, two rows 799 each (1621 used),
        # four rows pruned at 799 each (3196 pruned).
        evidence_rows = [
            ContextItem(
                item_id=f"probe-evidence-{index}",
                source=ContextSource.EVIDENCE,
                content=(
                    f"Runbook {index}. Rebind the registered authenticator and confirm "
                    "the binding holds before the ticket is closed. "
                )
                * 17,
                allowed_agents=frozenset({ContextAgent.ANALYSIS}),
                trust=TrustLabel.UNTRUSTED,
                authority=0.7,
                relevance=0.9 - index / 100,
                provenance_ref=f"probe://runbook-{index}",
            )
            for index in range(6)
        ]
        envelope = ContextBuilder().build(
            tenant_id=self.case.tenant_id,
            run_id=uuid4(),
            task_id="probe-context-pruning",
            agent=ContextAgent.ANALYSIS,
            items=[control, *evidence_rows],
            max_input_tokens=max_input_tokens,
            system_reserve=system_reserve,
            output_reserve=output_reserve,
        )
        manifest = envelope.selection_manifest
        selected = [
            entry
            for entry in manifest
            if entry.decision == "selected" and entry.source is ContextSource.EVIDENCE
        ]
        dropped = [
            entry
            for entry in manifest
            if entry.decision in {"pruned", "rejected"}
            and entry.reason
            and entry.source is ContextSource.EVIDENCE
        ]
        # Both halves are required, and both are about *evidence*: "something survived"
        # alone is satisfied by the control row the packer may never drop, and "something
        # was dropped" alone is satisfied by a run that dropped everything. The rule under
        # test is that a payload too large to fit is still delivered in part, and says
        # which part it lost.
        passed = bool(selected) and bool(dropped)
        self.step(
            step,
            started=started,
            outcome="passed" if passed else "failed",
            detail=json.dumps(
                {
                    "step": step.id,
                    "expectation": (
                        "an over-budget payload is delivered with at least one evidence "
                        "row selected and at least one dropped with a stated reason"
                    ),
                    "budget": {
                        "max_input_tokens": max_input_tokens,
                        "system_reserve": system_reserve,
                        "output_reserve": output_reserve,
                        "usable_tokens": usable,
                        "tokens_used": envelope.budget.tokens_used,
                        "tokens_pruned": envelope.budget.tokens_pruned,
                    },
                    "items": [
                        {
                            "item_id": item.item_id,
                            "source": item.source.value,
                            "required": item.required,
                            "content_chars": len(item.content),
                        }
                        for item in (control, *evidence_rows)
                    ],
                    # The manifest is the whole observation: the numbers in it are what
                    # a reader checks the arithmetic against, and the reason strings are
                    # what the assertion is about.
                    "manifest": [
                        {
                            "item_id": entry.item_id,
                            "source": entry.source.value,
                            "tokens": entry.tokens,
                            "decision": entry.decision,
                            "reason": entry.reason,
                        }
                        for entry in manifest
                    ],
                    "selected": [entry.item_id for entry in selected],
                    "dropped_with_reason": [entry.item_id for entry in dropped],
                },
                ensure_ascii=False,
            ),
        )

    async def corpus_scope_producer(self, step: CaseStep, subject: str) -> None:
        """Read the acceptance corpus twice, as a group member and as nobody.

        The claim is the one the user stated on 2026-09-22: once the requester's group is
        taken away the run must not be able to read that group's documents. Reading it off
        the case's own run does not hold it -- measured across this acceptance, an analyst
        holding group 3 cites the restricted runbook in about half of their runs even with
        nothing revoked, so "the restricted document is absent" is satisfied half the time
        by a platform where the narrowing does nothing. See
        ``servicemind.evaluation.knowledge_probe`` for why the fixed query exists.

        The readings are printed in full rather than reduced to a verdict, because the
        interesting failure of this probe is not "the restricted document was returned" --
        that one is unambiguous -- but "nothing was returned", which only the whole list
        shows.
        """
        from servicemind.evaluation.knowledge_probe import (
            CORPUS_RESTRICTED_BY_GROUP,
            PUBLIC_CONTROL_DOCUMENT,
            corpus_scope_problems,
            corpus_scope_reading,
            principal_for,
        )
        from servicemind.rag.service import build_enterprise_rag

        started = now()
        observed = await self.stack.observed_subject(subject)
        # The entity is the subject's own, read from the identity provider rather than
        # written into the case: the two readings must differ in the group axis and in
        # nothing else, and a hardcoded entity would keep comparing two principals even
        # if the subject's entity had moved.
        entity_id = min(observed.entity_ids)
        rag = build_enterprise_rag()
        readings: dict[str, list[str]] = {}
        for label, group_ids in (
            ("holding_its_group", set(CORPUS_RESTRICTED_BY_GROUP.values())),
            ("holding_no_group", set()),
        ):
            readings[label] = await corpus_scope_reading(
                rag,
                principal_for(
                    self.stack.tenant_id,
                    user_id=f"acceptance-corpus-probe-{label}",
                    entity_ids={entity_id},
                    group_ids=group_ids,
                ),
                query=self.case.question,
            )
        problems = await corpus_scope_problems(
            rag,
            self.stack.tenant_id,
            entity_id=entity_id,
            query=self.case.question,
        )
        self.step(
            step,
            started=started,
            outcome="failed" if problems else "passed",
            detail=json.dumps(
                {
                    "step": step.id,
                    "expectation": (
                        "the same query over the same corpus returns the group-restricted "
                        "document to a principal holding that group and not to one holding "
                        "none, with the unrestricted control document returned to both"
                    ),
                    "query": self.case.question,
                    "restricted_documents": CORPUS_RESTRICTED_BY_GROUP,
                    "control_document": PUBLIC_CONTROL_DOCUMENT,
                    "readings": readings,
                    "problems": problems,
                },
                ensure_ascii=False,
            ),
        )

    async def mcp_list(self, step: CaseStep, subject: str) -> None:
        from servicemind.mcp.server import TASK_EXTENSION

        started = now()
        response = await self._mcp_call(subject, "tools/list", None, None)
        payload = response.json()
        listed = sorted(
            str(item["name"])
            for item in (payload.get("result", {}).get("tools") or [])
            if isinstance(item, dict) and "name" in item
        )
        # The native read path for the same principal: the same gateway the service
        # builds, asked the same question with the same roles and entity ids. The driver
        # sends the governed-execution capability, so the write tool is in scope on both
        # sides and the comparison is not narrowed by a capability this call happens to
        # omit.
        context = await self.stack.context(subject)
        native = sorted(
            definition.name.removeprefix("glpi.")
            for definition in build_tool_gateway().registry.visible(
                roles=frozenset(context.roles),
                entity_ids=frozenset(context.allowed_glpi_entity_ids),
            )
        )
        passed = response.status_code == 200 and "result" in payload and listed == native
        self.step(
            step,
            started=started,
            http_status=response.status_code,
            outcome="passed" if passed else "failed",
            detail=json.dumps(
                {
                    "mcp": listed,
                    "native": native,
                    "agrees": listed == native,
                    "supports_tasks": TASK_EXTENSION in json.dumps(payload),
                },
                ensure_ascii=False,
            ),
        )

    async def mcp_call(self, step: CaseStep, subject: str) -> None:
        started = now()
        ticket_id = self.ticket_id(step)
        response = await self._mcp_call(
            subject, "tools/call", "get_ticket_context", {"ticket_id": ticket_id}
        )
        payload = response.json()
        result = payload.get("result")
        # A JSON-RPC error still travels over HTTP 200, so the status alone is not the
        # observation: what is read is whether a result came back, and whether it is the
        # ticket that was asked for.
        body: str | None = None
        if isinstance(result, dict):
            content = result.get("content")
            if isinstance(content, list) and content and isinstance(content[0], dict):
                body = str(content[0].get("text", ""))[:2000]
        passed = (
            response.status_code == 200
            and isinstance(result, dict)
            and not result.get("isError", False)
            and body is not None
            and str(ticket_id) in body
        )
        self.step(
            step,
            started=started,
            http_status=response.status_code,
            outcome="passed" if passed else "failed",
            detail=json.dumps(
                {
                    "ticket_id": ticket_id,
                    "is_error": (result.get("isError") if isinstance(result, dict) else None),
                    "body": body,
                    "error": payload.get("error"),
                },
                ensure_ascii=False,
            ),
        )

    async def _mcp_call(
        self, subject: str, method: str, name: str | None, arguments: dict[str, Any] | None
    ) -> httpx.Response:
        params: dict[str, Any] = {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientInfo": {
                    "name": "phase7-acceptance",
                    "version": "1.0",
                },
                "io.modelcontextprotocol/clientCapabilities": {
                    "extensions": {GOVERNED_EXECUTION_EXTENSION: {}}
                },
            }
        }
        if name is not None:
            params["name"] = name
            params["arguments"] = arguments or {}
        headers = {
            "Mcp-Protocol-Version": MCP_PROTOCOL_VERSION,
            "Mcp-Method": method,
        }
        if name is not None:
            headers["Mcp-Name"] = name
        return await self.stack.request(
            subject,
            "POST",
            f"{self.stack.base_url}/v1/servicemind/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )

    async def activate_memory(self, step: CaseStep, subject: str) -> None:
        """Move the run's quarantined procedure through human review, over HTTP.

        The row is found in the database by ``source_run_id`` rather than taken from the
        review queue's payload: the queue item states ``provenance`` but not the run it
        came from, so a queue holding several quarantined procedures would be resolved by
        whichever one happened to be listed first.

        Searched across every run *this case* submitted, not only the one it is named
        after, and the producer is recorded in the step. A cross-ticket procedure is
        proposed by the post-run step of the run at which corroboration completes, which
        for a case that submits a pair means whichever of the two wrote last: on a tenant
        already holding an episode for this pattern, the platform can legitimately
        attribute it to the first submission -- ACC-12b, where a procedure proposed from
        run A's post-run step is the same row run B would have proposed, and run B's write
        is the duplicate the idempotency key exists to absorb.
        """
        started = now()
        produced = list(self.runs.values()) or ([self.run_id] if self.run_id else [])
        async with tenant_session(self.stack.tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        select(MemoryRecordRow).where(
                            MemoryRecordRow.source_run_id.in_(produced),
                            MemoryRecordRow.memory_type == "procedural",
                        )
                    )
                )
                .scalars()
                .all()
            )
        if not rows:
            self.errors.append(
                f"{self.case.id}/{step.id}: no procedural memory is attributed to any run "
                f"of this case ({produced}), so there is nothing for a human to activate"
            )
            self.step(step, started=started)
            return
        # The case's own run first when both are present, so the ordinary path is
        # unchanged and only the pair case falls through to the earlier submission.
        row = next(
            (item for item in rows if item.source_run_id == self.run_id),
            rows[0],
        )
        response = await self.stack.request(
            subject,
            "POST",
            f"{self.stack.base_url}/v1/servicemind/memories/{row.id}/review",
            json={
                "decision": "activate",
                "expected_version": row.version,
                "expected_content_hash": row.content_hash,
                "review_ref": f"P7.6 {self.case.id}",
                "comment": f"P7.6 {self.case.id}: operator activation for the acceptance case",
            },
        )
        if response.status_code >= 300:
            self.errors.append(
                f"{self.case.id}/{step.id}: activation returned {response.status_code} "
                f"{response.text[:400]}"
            )
        self.step(
            step,
            started=started,
            http_status=response.status_code,
            detail=json.dumps(
                {
                    "memory_id": str(row.id),
                    "status_before": str(row.status),
                    "version": row.version,
                    # Which submission proposed it, stated rather than assumed: the
                    # activation is about the row, and a reader checking the case against
                    # its evidence needs to see when the producer was not the named run.
                    "proposed_by_run": str(row.source_run_id),
                    "proposed_by_the_cases_named_run": row.source_run_id == self.run_id,
                    "response": (
                        response.json() if response.status_code < 300 else response.text[:400]
                    ),
                },
                ensure_ascii=False,
            ),
        )

    async def outbox_tally(self, step: CaseStep, subject: str) -> None:
        """Count what is in the queue, and measure the two things that bound it.

        The counts answer "are the approved events delivered", which the relay's own
        ``published`` transitions are the evidence for. They do not answer "does this
        table grow forever", and neither does any single reading of it: an unbounded
        table and a bounded one look identical on the day it is measured. What separates
        them is the age of the oldest row still held under the retention -- a table whose
        oldest delivered row is older than the retention is a table no sweep has reached,
        and it will keep growing whatever today's count says.
        """
        started = now()
        try:
            async with tenant_session(self.stack.tenant_id) as session:
                rows = (
                    await session.execute(
                        select(ToolOutboxRecord.status, ToolOutboxRecord.updated_at).where(
                            ToolOutboxRecord.event_type == "action.approved"
                        )
                    )
                ).all()
        except Exception as exc:  # noqa: BLE001 - a probe that cannot read says so
            self.errors.append(f"{self.case.id}/{step.id}: the outbox could not be read: {exc!r}")
            self.step(step, started=started, detail=repr(exc))
            return
        counts: dict[str, int] = {}
        for status_value, _ in rows:
            counts[str(status_value)] = counts.get(str(status_value), 0) + 1
        consumed = counts.get("published", 0)
        delivered = [updated for status_value, updated in rows if str(status_value) == "published"]
        # How many delivered rows are already older than a day. Nothing is deleted for
        # this -- it is a read -- but it is the number that says what the table was doing
        # before it had a retention: a deployment without one holds every one of these
        # forever, and the count only ever moves one way.
        day_ago = datetime.now(UTC) - timedelta(days=1)
        oldest = min(delivered) if delivered else None
        retention = PUBLISHED_RETENTION
        # One day of slack past the retention: the sweep runs hourly, and a worker that
        # was down for an afternoon must not read as an unbounded table. Anything older
        # than retention plus that slack is a row the sweep has never reached.
        ceiling = retention + timedelta(days=1)
        oldest_age = None if oldest is None else datetime.now(UTC) - oldest
        retained = oldest_age is None or oldest_age <= ceiling
        detail = {
            "event_type": "action.approved",
            "total": len(rows),
            "by_status": counts,
            "unconsumed": len(rows) - consumed,
            "retention_days": retention.days,
            "oldest_delivered_row": None if oldest is None else oldest.isoformat(),
            "oldest_delivered_age_seconds": (
                None if oldest_age is None else round(oldest_age.total_seconds(), 1)
            ),
            "delivered_rows_within_retention": retained,
            "delivered_rows_older_than_one_day": len(
                [updated for updated in delivered if updated < day_ago]
            ),
            "redis": await self._stream_length(),
            "note": (
                "PASS here means the tally was taken and the retention read, not that a "
                "consumer outside this repository drained the queue: no consumer exists "
                "in the platform, which is recorded in the baseline document"
            ),
        }
        self.step(
            step,
            started=started,
            outcome="passed" if retained else "failed",
            detail=json.dumps(detail, ensure_ascii=False),
        )

    async def _stream_length(self) -> dict[str, Any]:
        """How long the delivery stream is, and against what bound.

        Recorded rather than asserted: at the scale this acceptance run produces, an
        unbounded stream and a bounded one are the same length, so a bound check here
        would pass for the wrong reason. The bound is asserted where it is decidable --
        the unit test that the publisher passes ``maxlen`` at all.
        """
        from redis.asyncio import Redis

        url = settings.SERVICEMIND_REDIS_URL
        if url is None:
            return {"readable": False, "reason": "SERVICEMIND_REDIS_URL is not configured"}
        client = Redis.from_url(
            url.get_secret_value(),
            decode_responses=False,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        stream = RedisStreamPublisher(client).stream
        try:
            return {
                "readable": True,
                "stream": stream,
                "length": int(await client.xlen(stream)),
                "bound": STREAM_MAXLEN,
            }
        except Exception as exc:  # noqa: BLE001 - an unreadable stream is recorded as such
            return {"readable": False, "reason": type(exc).__name__}
        finally:
            await client.aclose()

    async def playwright(self, step: CaseStep, subject: str) -> None:
        started = now()
        variable = SUBJECT_PASSWORD_VARS.get(subject)
        if variable is None:
            self.errors.append(
                f"{self.case.id}/{step.id}: no password variable is declared for {subject!r}"
            )
            self.step(step, started=started)
            return
        command = [
            "docker",
            "run",
            "--rm",
            "--network",
            "host",
            "-v",
            f"{FRONTEND}:/app",
            "-w",
            "/app",
            "-e",
            f"SERVICEMIND_ACCEPTANCE_SUBJECT={subject}",
            "-e",
            f"SERVICEMIND_ACCEPTANCE_PASSWORD_VAR={variable}",
            "-e",
            f"SERVICEMIND_ACCEPTANCE_TICKET={self.ticket_id(step)}",
            "-e",
            f"SERVICEMIND_ACCEPTANCE_MODE={'login' if step.id == 'browser-login' else 'submit'}",
            "-e",
            "SERVICEMIND_FRONTEND_URL=http://127.0.0.1:3000",
            PLAYWRIGHT_IMAGE,
            "npx",
            "playwright",
            "test",
            "e2e/acceptance-console.spec.ts",
        ]
        completed = None
        output = ""
        try:
            # ``--env-file`` and not ``-e VAR=value``: an argument is in the process table
            # for anyone on the host to read, so a password passed as one is disclosed by
            # the probe whose whole point is to be run and seen. The file is 0600, holds
            # the one variable, and is unlinked as soon as docker has read it.
            with _password_env_file(variable, self.stack.password(subject)) as env_file:
                completed = subprocess.run(
                    [*command[:2], "--env-file", str(env_file), *command[2:]],
                    capture_output=True,
                    text=True,
                    timeout=900,
                    check=False,
                )
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            self.errors.append(
                f"{self.case.id}/{step.id}: the browser probe could not be launched: {exc!r}"
            )
            self.step(step, started=started, outcome="failed", detail=repr(exc))
            return
        output = (completed.stdout + completed.stderr).strip()
        passed = completed.returncode == 0
        if not passed:
            self.errors.append(
                f"{self.case.id}/{step.id}: the browser probe exited "
                f"{completed.returncode}; see the step detail"
            )
        self.step(
            step,
            started=started,
            outcome="passed" if passed else "failed",
            detail=output[-4000:],
        )

    async def pytest_probe(self, step: CaseStep, subject: str) -> None:
        started = now()
        target = step.action.removeprefix("pytest ").split()
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", *target],
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
            cwd=REPO_ROOT,
        )
        output = (completed.stdout + completed.stderr).strip()
        passed = completed.returncode == 0
        if not passed:
            self.errors.append(
                f"{self.case.id}/{step.id}: {step.action!r} exited {completed.returncode}"
            )
        self.step(
            step,
            started=started,
            outcome="passed" if passed else "failed",
            detail=output[-4000:],
        )

    # --------------------------------------------------------------- observation

    async def snapshot_memory(self, step_id: str) -> None:
        """Record the case's memory rows as they stand, labelled with the step.

        Rows are keyed to this case by ``source_run_id`` against every run the case
        submitted, so a run that produced no memory contributes nothing rather than being
        represented by an unrelated row.
        """
        run_ids = list(self.runs.values())
        if not run_ids:
            return
        try:
            async with tenant_session(self.stack.tenant_id) as session:
                rows = (
                    (
                        await session.execute(
                            select(MemoryRecordRow).where(
                                MemoryRecordRow.source_run_id.in_(run_ids)
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            self.errors.append(
                f"{self.case.id}/{step_id}: the memory table could not be read: {exc!r}"
            )
            return
        for row in rows:
            self.memory.append(
                ObservedMemoryRecord(
                    memory_id=row.id,
                    memory_type=str(row.memory_type),
                    status=str(row.status),
                    source_run_id=row.source_run_id,
                    procedure_pattern_key=(row.provenance or {}).get("procedure_pattern_key"),
                    content=str(row.content or "")[:2000],
                    observed_at_step=step_id,
                )
            )

    async def collect_run_evidence(self) -> None:
        """Everything about the finished run that is not in a step response."""
        if self.run_id is None:
            return
        try:
            self.timeline = await self.stack.timeline_events(self.case.subject, self.run_id)
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            self.errors.append(f"{self.case.id}: the event log could not be read: {exc!r}")
        try:
            async with tenant_session(self.stack.tenant_id) as session:
                audit = (
                    await session.execute(
                        select(AuditEvent.event_type).where(AuditEvent.run_id == self.run_id)
                    )
                ).all()
                manifests = (
                    await session.execute(
                        select(ContextArtifactRecord.selection_manifest)
                        .where(ContextArtifactRecord.run_id == self.run_id)
                        .order_by(ContextArtifactRecord.created_at)
                    )
                ).all()
            self.audit = [str(row[0]) for row in audit]
            for (items,) in manifests:
                self.manifest.extend(items or [])
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            self.errors.append(
                f"{self.case.id}: the audit or context-artifact tables failed: {exc!r}"
            )

    def _evidence_rows(self) -> list[Any]:
        """The result's evidence rows, read through the platform's own accessor.

        This used to branch on the shape itself, because the platform published two: the
        ``{items: [...], tenant_id: ...}`` envelope from the supervisor and a bare list
        from the fast paths. The fast paths now publish the envelope too, so the driver
        asks ``evidence_items`` rather than carrying a second opinion about the encoding
        -- which is the same tolerance, minus the part where every reader has to remember
        it exists.
        """
        return evidence_items(self.result.get("evidence"))

    def evidence_view(self) -> EvidenceView:
        """The persisted result parsed once, with a note for each row that would not."""
        view = EvidenceView()
        for raw in self._evidence_rows():
            try:
                view.evidence.append(Evidence.model_validate(raw))
            except Exception:  # noqa: BLE001
                view.notes.append(f"{self.case.id}: an evidence row did not validate")
        if isinstance(self.result.get("analysis"), dict):
            try:
                view.analysis = AnalysisResult.model_validate(self.result["analysis"])
            except Exception:  # noqa: BLE001
                view.notes.append(f"{self.case.id}: the analysis did not validate")
        if isinstance(self.result.get("review"), dict):
            try:
                view.review = ReviewResult.model_validate(self.result["review"])
            except Exception:  # noqa: BLE001
                view.notes.append(f"{self.case.id}: the review did not validate")
        seen: set[str] = set()
        for evidence in view.evidence:
            raw_citation = evidence.metadata.get(CITATION_KEY)
            if not isinstance(raw_citation, dict):
                continue
            try:
                citation = Citation.model_validate(raw_citation)
            except Exception:  # noqa: BLE001
                view.notes.append(f"{self.case.id}: a citation did not validate")
                continue
            if citation.source_record_id and citation.source_record_id not in seen:
                seen.add(citation.source_record_id)
                view.citations.append(citation)
        return view

    def execution(
        self,
        environment: ObservedEnvironment,
        subject: ObservedSubject | None,
        *,
        cases_digest: str,
    ) -> CaseExecution:
        view = self.evidence_view()
        for note in view.notes:
            if note not in self.notes:
                self.notes.append(note)
        return CaseExecution(
            case_id=self.case.id,
            cases_digest=cases_digest,
            environment=environment,
            subject=subject,
            steps=self.steps,
            run_id=self.run_id,
            # Stamped, because the runs a case submitted are part of what was observed
            # and not derivable from the steps afterwards -- see ``case_run_ids``.
            case_run_ids=list(self.runs.values()),
            terminal_status=self.status,
            total_seconds=self.total_seconds,
            action_intent=self.intent,
            citations=view.citations,
            evidence=view.evidence,
            analysis=view.analysis,
            review=view.review,
            selection_manifest=self.manifest,
            followups_before=self.before,
            followups_after=self.after,
            timeline_events=self.timeline,
            audit_events=self.audit,
            memory_records=self.memory,
            graph_readings=self.graph,
            foreign_run_status=self.foreign_status,
            listed_for_own_tenant=self.listed_own,
            errors=self.errors,
        )


# --------------------------------------------------------------------------- dispatch


async def perform(case_run: CaseRun, step: CaseStep, stack: Stack) -> None:
    subject = step.as_subject or case_run.case.subject
    action = step.action
    if action == "POST /v1/servicemind/runs":
        await case_run.submit(step, subject)
    elif action == "GET /v1/servicemind/runs/{run_id}":
        if step.id not in RUN_READ_STEPS:
            raise RuntimeError(f"unknown run-read step id: {step.id!r}")
        await case_run.read_run(step, subject)
    elif action == "POST /v1/servicemind/runs/{run_id}/approval":
        if step.id not in APPROVAL_STEPS:
            raise RuntimeError(f"unknown approval step id: {step.id!r}")
        await case_run.approval(step, subject)
    elif action == "GLPI list_ticket_followups":
        await case_run.read_followups(step, subject)
    elif action == "GRAPH retrieve":
        await case_run.graph_read(step, subject)
    elif action == "procedural memory pattern_key producer":
        await case_run.pattern_key_producer(step, subject)
    elif action == "context envelope pruning producer":
        await case_run.context_pruning_producer(step, subject)
    elif action == "corpus scope producer":
        await case_run.corpus_scope_producer(step, subject)
    elif action == "memory review activation":
        await case_run.activate_memory(step, subject)
    elif action == "POST /v1/servicemind/mcp tools/list":
        await case_run.mcp_list(step, subject)
    elif action == "POST /v1/servicemind/mcp tools/call":
        await case_run.mcp_call(step, subject)
    elif action == "outbox tally":
        await case_run.outbox_tally(step, subject)
    elif action in {"KEYCLOAK entitlement", "KEYCLOAK verifier outage"}:
        if step.id not in KEYCLOAK_STEPS:
            raise RuntimeError(f"unknown keycloak step id: {step.id!r}")
        await case_run.keycloak(step, KEYCLOAK_STEPS[step.id])
    elif action.startswith("Playwright "):
        await case_run.playwright(step, subject)
    elif action.startswith("pytest "):
        await case_run.pytest_probe(step, subject)
    else:
        raise RuntimeError(f"unknown step action: {action!r}")


async def restore_realm(case_run: CaseRun, stack: Stack) -> None:
    """Put the identity provider and the serving process back, whatever the case did.

    Run after every case rather than only by the ones that change something: the cost of
    a no-op is one list comparison, and the cost of a missed restore is a realm left
    revoked, which the next case would then run against while reporting a clean premise.
    The restore is recorded on the execution when it did something the case did not ask
    for, so a case that died mid-revocation says so in its own replay rather than leaving
    it to be inferred from the next case's odd result.
    """
    for username in await stack.entitlements.has_unrestored():
        try:
            detail = await stack.entitlements.restore(username)
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            case_run.errors.append(
                f"{case_run.case.id}: the realm was left revoked for {username!r}: {exc!r}"
            )
        else:
            case_run.notes.append(f"{case_run.case.id}: cleanup {detail}")
    if stack.outage_active:
        try:
            detail = await stack.end_outage()
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            case_run.errors.append(f"{case_run.case.id}: the outage was left in place: {exc!r}")
        else:
            case_run.notes.append(f"{case_run.case.id}: cleanup {detail}")


async def run_case(
    case: AcceptanceCase,
    stack: Stack,
    tickets: dict[str, int],
    environment: ObservedEnvironment,
    cases_digest: str,
) -> CaseExecution:
    case_run = CaseRun(case, stack, tickets)
    subject: ObservedSubject | None = None
    try:
        subject = await stack.observed_subject(case.subject)
    except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
        case_run.errors.append(f"{case.id}: the subject could not be authenticated: {exc!r}")
    for step in case.steps:
        started = now()
        try:
            await perform(case_run, step, stack)
        except Exception as exc:  # noqa: BLE001 - one step's failure stops the case, not the run
            case_run.errors.append(f"{case.id}/{step.id}: driver error {exc!r}")
            # The traceback, because a driver error is a statement about *this code* and
            # ``repr(exc)`` alone does not say where. Reading "KeyError('globex-analyst-g3')"
            # out of a replay with no frame is a search through the whole file; the last
            # frames make it a line number.
            case_run.step(step, started=started, detail=traceback.format_exc(limit=6)[-2000:])
            break
        if step.id in MEMORY_SNAPSHOT_STEPS:
            await case_run.snapshot_memory(step.id)
    try:
        await case_run.collect_run_evidence()
    finally:
        # In a ``finally`` rather than after the loop: a failure while reading the
        # evidence would otherwise be the failure that leaves the realm revoked.
        await restore_realm(case_run, stack)
    execution = case_run.execution(environment, subject, cases_digest=cases_digest)
    REPLAYS.mkdir(parents=True, exist_ok=True)
    (REPLAYS / f"{case.id}.json").write_text(execution.model_dump_json(indent=2), encoding="utf-8")
    return execution


def environment_of(stack: Stack, revision: str | None) -> ObservedEnvironment:
    verifier = current_entitlement_verifier()
    return ObservedEnvironment(
        entitlement_verifier_configured=verifier is not None,
        base_url=stack.base_url,
        tenant_id=stack.tenant_id,
        recorded_at=datetime.now(UTC),
        deployed_revision=revision,
        notes=[
            "The entitlement verifier's presence is read from this process's own "
            "registry, after importing servicemind.security.auth -- the module whose "
            "import installs it. Importing the registry alone reads an empty slot that "
            "no deployment ever fills, which would report 'not configured' for a "
            "deployment that had one.",
            "Whether that answer describes the *serving* process is a separate question, "
            "and the driver refuses to run when the unit predates the tree.",
        ],
    )


def source_revision() -> str | None:
    """The working tree the running unit was started from, and whether it was clean.

    Recorded as ``<sha>`` or ``<sha>+dirty(N files)`` because the deployed code is the
    tree as it stood when the unit started, and a report naming only the commit would
    describe a revision the process may never have loaded.
    """
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if not head:
        return None
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return f"{head}+dirty({len(dirty.splitlines())} files)" if dirty else head


def unit_started_at() -> str | None:
    completed = subprocess.run(
        ["systemctl", "--user", "show", "servicemind-api", "-p", "ActiveEnterTimestamp"],
        capture_output=True,
        text=True,
        check=False,
    )
    parts = completed.stdout.strip().split("=", 1)
    return parts[1].strip() if len(parts) == 2 and parts[1].strip() else None


def _unit_main_pid() -> int | None:
    completed = subprocess.run(
        ["systemctl", "--user", "show", "servicemind-api", "-p", "MainPID"],
        capture_output=True,
        text=True,
        check=False,
    )
    parts = completed.stdout.strip().split("=", 1)
    if len(parts) != 2 or not parts[1].strip().isdigit():
        return None
    return int(parts[1])


def _process_started_at(pid: int) -> float | None:
    """When a process began, as a Unix timestamp.

    From the boot clock plus the process's own start tick, rather than from systemd's
    formatted timestamp: that one is rendered in the machine's locale, and this value
    has to be compared with a file's mtime, not read by a person.
    """
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        ticks = int(fields[21])
    except (OSError, IndexError, ValueError):
        return None
    boot = time.time() - time.clock_gettime(time.CLOCK_BOOTTIME)
    return boot + ticks / os.sysconf("SC_CLK_TCK")


def stale_deployment() -> dict[str, Any] | None:
    """Whether the serving process predates the code in the tree, and by how much.

    The venv is an editable install of ``src``, which makes it easy to believe a source
    edit is live. It is not: a running process holds the modules it imported, and this
    stack had been up since 17:02 the previous day while ``orchestration/`` was edited
    at 00:44 -- so a whole sweep observed pre-fix behaviour and reported it as the
    revision in the working tree. ``deployed_revision`` describes the *tree*; this
    describes whether the tree is what answered, and the two have to be checked
    separately because the interesting failure is exactly the case where they differ.
    """
    pid = _unit_main_pid()
    started = None if pid is None else _process_started_at(pid)
    if started is None:
        return None
    newest_mtime, newest_path = max(
        (path.stat().st_mtime, path) for path in (REPO_ROOT / "src").rglob("*.py")
    )
    if newest_mtime <= started:
        return None
    return {
        "unit_main_pid": pid,
        "unit_started_epoch": round(started, 1),
        "newest_source": str(newest_path.relative_to(REPO_ROOT)),
        "newest_source_epoch": round(newest_mtime, 1),
        "seconds_behind": round(newest_mtime - started, 1),
        "remedy": "systemctl --user restart servicemind-api, then re-run",
    }


def _case_tickets(case_set: AcceptanceCaseSet, recorded: dict[str, Any]) -> dict[str, int]:
    declared = {str(name): int(value) for name, value in recorded["tickets"].items()}
    missing = {
        reference
        for case in case_set.cases
        for reference in {case.ticket_ref} | {step.ticket_ref for step in case.steps}
        if reference is not None
    } - set(declared)
    if missing:
        raise SystemExit(
            json.dumps(
                {
                    "unresolved_tickets": sorted(missing),
                    "hint": "run scripts/seed_phase7_acceptance_fixtures.py first",
                },
                ensure_ascii=False,
            )
        )
    return declared


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", default="22222222-2222-4222-8222-222222222222")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--only", default="", help="comma-separated case ids to run")
    parser.add_argument(
        "--timeout-scale",
        type=float,
        default=1.0,
        help="multiplier on each case's polling budget; the frozen budget is 1.0",
    )
    parser.add_argument(
        "--allow-stale-deployment",
        action="store_true",
        help=(
            "run even though the serving process predates the tree, recording the gap "
            "as a note instead of refusing; the evidence will describe the older code"
        ),
    )
    args = parser.parse_args()

    # Refused by default, because this is the failure that does not announce itself: a
    # stale process answers every request competently, so the sweep completes and every
    # replay carries the revision of a tree the platform was not running.
    staleness = stale_deployment()
    if staleness is not None and not args.allow_stale_deployment:
        print(json.dumps({"stale_deployment": staleness}, ensure_ascii=False, indent=2))
        return 3
    if staleness is not None:
        print(json.dumps({"stale_deployment": staleness}, ensure_ascii=False))

    case_set = AcceptanceCaseSet.model_validate(json.loads(CASES.read_text(encoding="utf-8")))
    # Stamped onto every replay: the gate reads it back to decide whether the observation
    # still describes the expectations it sits beside.
    cases_digest = case_set_digest(case_set)
    recorded = json.loads(FIXTURE_TICKETS.read_text(encoding="utf-8"))
    tickets = _case_tickets(case_set, recorded)
    selected = {item.strip() for item in args.only.split(",") if item.strip()}
    unknown = selected - {case.id for case in case_set.cases}
    if unknown:
        print(json.dumps({"unknown_cases": sorted(unknown)}, ensure_ascii=False))
        return 2
    cases = [case for case in case_set.cases if not selected or case.id in selected]

    stack = Stack(
        base_url=args.base_url,
        tenant_id=UUID(args.tenant_id),
        timeout_scale=args.timeout_scale,
    )
    environment = environment_of(stack, source_revision())
    print(
        json.dumps(
            {
                "base_url": stack.base_url,
                "tenant_id": str(stack.tenant_id),
                "deployed_revision": environment.deployed_revision,
                "api_unit_started_at": unit_started_at(),
                "entitlement_verifier_configured": (environment.entitlement_verifier_configured),
                "cases": [case.id for case in cases],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    failures: list[str] = []
    try:
        for case in cases:
            started = now()
            try:
                execution = await run_case(case, stack, tickets, environment, cases_digest)
            except Exception as exc:  # noqa: BLE001 - the next case is still worth running
                failures.append(f"{case.id}: {exc!r}")
                print(f"{case.id}: DRIVER FAILURE {exc!r}", flush=True)
                continue
            print(
                json.dumps(
                    {
                        "case": case.id,
                        # Named for what it is: a case with no run at all (ACC-12a
                        # evaluates the pattern producer directly) reports null here, and
                        # a bare "status" beside "errors" reads as a verdict that never
                        # arrived. The verdict is the grader's, not this line's.
                        "terminal_status": execution.terminal_status,
                        "elapsed_seconds": round(time.monotonic() - started.monotonic, 1),
                        "errors": execution.errors,
                        "failed_probes": [
                            {"step": step.id, "detail": (step.detail or "")[:300]}
                            for step in execution.steps
                            if step.outcome == "failed"
                        ],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    finally:
        await stack.aclose()
        await close_database()
    if failures:
        print(json.dumps({"driver_failures": failures}, ensure_ascii=False), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(
        asyncio.run(
            main(),
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
    )
