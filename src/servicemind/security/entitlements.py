"""Re-establish an identity's authority at the moment a run wants to act again.

A run is resumed long after it was started, and by somebody else -- an approver, or a
recovery process after a restart. Its checkpoint records the scope its requester held
*when it started*, and a checkpoint is not a claim about the present: a group granted
then may have been taken away since, and the resumed run is about to spend that stale
grant against the retrieval index and the tool gateway again. The token that started the
run is gone by then, so there is nothing in the request to re-read; this module is the
out-of-band answer.

Four outcomes, and they are not interchangeable:

* ``VERIFIED`` -- the subject exists, is enabled, and holds these grants. The only
  outcome that lets a run proceed, and even then only within the intersection.
* ``USER_UNKNOWN`` / ``USER_DISABLED`` -- the subject is gone or switched off. Nothing
  this run does on their behalf is authorised any more, so it is refused outright
  rather than narrowed.
* ``UNAVAILABLE`` / ``NOT_CONFIGURED`` -- the question could not be answered. This is
  *not* "holds nothing": an unanswered question means the authority is unknown, and an
  unknown authority cannot be spent. Callers pause rather than continue.

An empty grant set that was actually observed (``VERIFIED`` with nothing in it) is a
fifth case and is genuinely different from every failure above. Collapsing "we asked
and the answer was nothing" into "we could not ask" is how a verification outage turns
into a silently permissive deployment.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Protocol
from uuid import UUID

import httpx

logger = logging.getLogger(__name__)


class EntitlementOutcome(StrEnum):
    VERIFIED = "verified"
    USER_UNKNOWN = "user_unknown"
    USER_DISABLED = "user_disabled"
    UNAVAILABLE = "unavailable"
    NOT_CONFIGURED = "not_configured"


@dataclass(frozen=True)
class EntitlementResult:
    """What is currently held, or why that could not be established."""

    outcome: EntitlementOutcome
    roles: frozenset[str] = field(default_factory=frozenset)
    entity_ids: frozenset[int] = field(default_factory=frozenset)
    group_ids: frozenset[int] = field(default_factory=frozenset)
    detail: str | None = None
    #: True when this is a remembered answer rather than one just obtained. A cached
    #: answer is a claim about the past, up to ``cache_window_seconds`` old, and it is
    #: what a decision taken on it actually rests on -- so it is carried with the
    #: answer rather than left for a reader to assume. A refusal is never cached, so
    #: this can only ever soften a *grant*, never a block.
    from_cache: bool = False
    #: How stale ``from_cache`` is allowed to be. Recorded alongside the answer so the
    #: revocation window a run moved inside is a number in the audit, not an
    #: undocumented property of the deployment.
    cache_window_seconds: float | None = None

    @property
    def established(self) -> bool:
        """True only for an answer, not for a failure to get one."""
        return self.outcome is EntitlementOutcome.VERIFIED


class AuthorityWithdrawn(RuntimeError):
    """The authority a run is about to spend is not there to spend.

    Raised at the point of use, not at the point of decision: a run whose grants
    changed between approval and execution must stop before the write, not after.

    Two reasons, and they are different findings:

    * ``"revoked"`` -- something the requester held has since been taken away.
    * ``"insufficient"`` -- nothing was taken away, but what they hold was never enough
      for this step. Verifying an identity and authorising an operation are separate
      questions, and a verified subject with no role for the action is the case where
      answering the first makes the second look answered too.
    """

    def __init__(
        self,
        reason: str,
        detail: str | None = None,
        revoked: dict[str, list] | None = None,
        missing: dict[str, list] | None = None,
    ) -> None:
        super().__init__(f"authority withdrawn: {reason}")
        self.reason = reason
        self.detail = detail
        self.revoked = revoked or {}
        self.missing = missing or {}


class EntitlementVerifier(Protocol):
    async def verify(
        self, tenant_id: UUID, user_id: str, *, fresh: bool = False
    ) -> EntitlementResult:
        """Look the subject up *by immutable subject id* and report what they hold.

        ``fresh=True`` must bypass any cache of previous answers. A cached answer is
        good enough to read with; it is not good enough to write with, because the
        window it leaves open is exactly the window in which a revocation is ignored.
        """


def _int_set(raw: object) -> frozenset[int]:
    if not isinstance(raw, list):
        return frozenset()
    values: set[int] = set()
    for item in raw:
        text = str(item)
        if text.isdecimal():
            values.add(int(text))
    return frozenset(values)


class KeycloakEntitlementVerifier:
    """Read a subject's realm roles and GLPI claims from the Keycloak Admin REST API.

    Deployments should give this a dedicated service identity with read-only user
    lookup -- not the realm administrator. A process that can read the admin API can
    read every identity in the realm, so the credential's blast radius is the whole
    tenant population rather than the one subject being resumed.

    It never raises: an unreachable identity provider must not fail an approval, it
    must fail to *authorise* one, which ``UNAVAILABLE`` achieves.
    """

    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        realm: str,
        cache_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._realm = realm
        self._cache_seconds = cache_seconds
        #: Injection point for tests, which need to drive the realm's answers --
        #: including the ones that are HTTP status codes -- without a realm.
        self._transport = transport
        #: Keyed by ``(tenant_id, subject)``. A subject is only meaningful inside the
        #: tenant that issued it, so the tenant is part of the key even though the
        #: realm is not.
        self._cache: dict[tuple[str, str], tuple[float, EntitlementResult]] = {}
        self._admin_token: tuple[float, str] | None = None

    @property
    def cache_seconds(self) -> float:
        return self._cache_seconds

    async def _token(self, client: httpx.AsyncClient) -> str:
        now = time.monotonic()
        if self._admin_token is not None and self._admin_token[0] > now:
            return self._admin_token[1]
        response = await client.post(
            "/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": self._username,
                "password": self._password,
            },
        )
        response.raise_for_status()
        body = response.json()
        token = str(body["access_token"])
        # Renew early: a token that expires mid-call produces an unanswered question,
        # and unanswered questions pause runs for no reason.
        self._admin_token = (now + max(float(body.get("expires_in", 60)) - 15.0, 1.0), token)
        return token

    async def verify(
        self, tenant_id: UUID, user_id: str, *, fresh: bool = False
    ) -> EntitlementResult:
        key = (str(tenant_id), user_id)
        now = time.monotonic()
        if not fresh:
            cached = self._cache.get(key)
            if cached is not None and cached[0] > now:
                # Stamped on the way out, not stored: the record is "this decision was
                # made on a remembered answer", and the caller has to be able to say so.
                return replace(
                    cached[1],
                    from_cache=True,
                    cache_window_seconds=self._cache_seconds,
                )
        try:
            result = await self._fetch(user_id)
        except Exception as exc:
            logger.warning(
                "Entitlement lookup failed for subject=%s; runs for this subject will "
                "pause rather than proceed on unconfirmed authority",
                user_id,
                exc_info=True,
            )
            result = EntitlementResult(
                EntitlementOutcome.UNAVAILABLE, detail=f"{type(exc).__name__}"
            )
        # Only answers are cached. A failure is not an answer, and caching it would
        # extend an outage by the cache window on top of the outage itself.
        if result.established or result.outcome in (
            EntitlementOutcome.USER_UNKNOWN,
            EntitlementOutcome.USER_DISABLED,
        ):
            self._cache[key] = (now + self._cache_seconds, result)
        return result

    async def _fetch(self, user_id: str) -> EntitlementResult:
        async with httpx.AsyncClient(
            base_url=self._base_url, timeout=10.0, transport=self._transport
        ) as client:
            bearer = await self._token(client)
            headers = {"Authorization": f"Bearer {bearer}"}
            # Look up by immutable subject id, never by username: a renamed account
            # must not resolve to a different person, and a recycled username must not
            # inherit the previous holder's grants.
            user = await client.get(f"/admin/realms/{self._realm}/users/{user_id}", headers=headers)
            if user.status_code == 404:
                return EntitlementResult(
                    EntitlementOutcome.USER_UNKNOWN, detail="subject is not in the realm"
                )
            user.raise_for_status()
            payload = user.json()
            if not payload.get("enabled", True):
                return EntitlementResult(
                    EntitlementOutcome.USER_DISABLED, detail="subject is disabled"
                )
            attributes = payload.get("attributes") or {}
            roles = await client.get(
                f"/admin/realms/{self._realm}/users/{user_id}/role-mappings/realm",
                headers=headers,
            )
            roles.raise_for_status()
            return EntitlementResult(
                EntitlementOutcome.VERIFIED,
                roles=frozenset(str(role.get("name")) for role in roles.json() if role.get("name")),
                entity_ids=_int_set(attributes.get("glpi_entity_ids")),
                group_ids=_int_set(attributes.get("glpi_group_ids")),
            )


_verifier: EntitlementVerifier | None = None


def current_entitlement_verifier() -> EntitlementVerifier | None:
    """The verifier this process was wired with, if any.

    This module deliberately reads no configuration. Constructing a verifier means
    reading ``core.settings``, and the product's scaffold budget counts every import
    site against ``core`` and may only shrink -- so the construction lives in
    ``security.auth``, which already pays for that import, and this stays a plain slot.

    Absence is not a degraded mode that still works: with no verifier the authority of
    a resumed run cannot be established at all, and the resume boundary pauses.
    """
    return _verifier


def configure_entitlement_verifier(verifier: EntitlementVerifier | None) -> None:
    """Install a verifier (the wiring in ``security.auth``, tests, and callers)."""
    global _verifier
    _verifier = verifier


async def revalidate(tenant_id: UUID, user_id: str, *, fresh: bool) -> EntitlementResult:
    """Ask what ``user_id`` currently holds."""
    verifier = current_entitlement_verifier()
    if verifier is None:
        return EntitlementResult(
            EntitlementOutcome.NOT_CONFIGURED,
            detail="no entitlement verifier is configured",
        )
    return await verifier.verify(tenant_id, user_id, fresh=fresh)


def intersect(recorded: set, current: frozenset):
    """What survives: the run keeps only what it was granted *and* still holds."""
    return recorded & set(current)


def revoked(recorded: set, current: frozenset) -> list:
    return sorted(recorded - set(current))


def step_shortfall(
    *,
    roles: set[str],
    entity_ids: set[int],
    required_roles: frozenset[str],
) -> dict[str, list[str]]:
    """What the requester lacks for the step, in the same vocabulary as ``revoked``.

    A step needs an action-addressed entity as well as a role: the only action this
    platform has is a ticket write, and a ticket lives in a GLPI entity. An empty entity
    set is reported as a shortfall rather than as an empty list, because an empty list
    reads as "nothing missing" -- which is the exact confusion this function exists to
    remove. ``revoked`` above is about *losing* a grant; this is about never having had
    enough of one, and a run whose requester holds nothing must not execute merely
    because there was nothing left to take away.
    """
    shortfall: dict[str, list[str]] = {}
    if missing_roles := sorted(required_roles - roles):
        shortfall["roles"] = missing_roles
    if not entity_ids:
        shortfall["entity_ids"] = ["<no GLPI entity in scope>"]
    return shortfall


async def require_still_held(
    *,
    tenant_id: UUID,
    user_id: str,
    roles: set[str],
    entity_ids: set[int],
    group_ids: set[int],
    required_roles: frozenset[str] = frozenset(),
) -> EntitlementResult:
    """Fresh, cache-bypassing proof that the subject may perform the step *now*.

    Called immediately before a write. Unlike the resume boundary -- which narrows and
    continues, because a run that lost one group is still a run -- this refuses. An
    action that a human approved on the strength of evidence gathered under a scope the
    requester no longer has is a stale approval, and the moment to notice is before the
    write, not after it.
    """
    result = await revalidate(tenant_id, user_id, fresh=True)
    if not result.established:
        raise AuthorityWithdrawn(result.outcome.value, result.detail)
    withdrawn = {
        "roles": revoked(roles, result.roles),
        "entity_ids": revoked(entity_ids, result.entity_ids),
        "group_ids": revoked(group_ids, result.group_ids),
    }
    if any(withdrawn.values()):
        raise AuthorityWithdrawn("revoked", revoked=withdrawn)
    missing = step_shortfall(roles=roles, entity_ids=entity_ids, required_roles=required_roles)
    if missing:
        raise AuthorityWithdrawn("insufficient", missing=missing)
    return result
