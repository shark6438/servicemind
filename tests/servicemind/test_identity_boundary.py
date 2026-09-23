"""Where a token becomes an identity, and what happens to one that cannot be one.

An identity is bounded by what the platform can act on, not by how large a token can be:
the subject by a column width, and a grant set by the number of terms its retrieval can
filter by.

``user_id`` is the token's ``sub`` claim verbatim. It is written to
``agent_runs.user_id`` -- a ``varchar(255)`` -- and to every audit row that records who
decided. The column is the contract; these tests are about which layer is allowed to be
the one that enforces it. Left to the column, an over-long subject authenticated cleanly
and then failed on ``INSERT``, which is an HTTP 500 on a request the platform had
already accepted and identified.
"""

from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import ValidationError
from starlette.requests import Request

from servicemind.domain.knowledge import ACL_SET_MAX_ENTRIES, RetrievalPrincipal
from servicemind.security import auth as auth_module
from servicemind.security.auth import IDENTITY_MAX_LENGTH, TenantContext, get_tenant_context

TENANT = "11111111-1111-4111-8111-111111111111"


def claims(**overrides) -> dict:
    """A complete, well-formed token body; ``overrides`` change exactly one thing."""
    body = {
        "tenant_id": TENANT,
        "sub": "analyst-1",
        "preferred_username": "analyst",
        "realm_access": {"roles": ["analyst"]},
    }
    body.update(overrides)
    return body


def stubbed_verifier(
    monkeypatch: pytest.MonkeyPatch, token_claims: dict
) -> auth_module.OIDCVerifier:
    """An ``OIDCVerifier`` whose signature check is skipped and whose claims are ours."""
    verifier = auth_module.OIDCVerifier()

    async def jwks(*, force: bool = False):
        del force
        return {"keys": [{"kid": "test"}]}

    monkeypatch.setattr(verifier, "_load_jwks", jwks)
    monkeypatch.setattr(verifier, "_configuration", lambda: ("issuer", "jwks", "audience"))
    monkeypatch.setattr(auth_module.jwt, "get_unverified_header", lambda token: {"kid": "test"})
    monkeypatch.setattr(
        auth_module.jwt.PyJWK, "from_dict", lambda value: SimpleNamespace(key="public-key")
    )
    monkeypatch.setattr(auth_module.jwt, "decode", lambda *args, **kwargs: token_claims)
    return verifier


def http_request(path: str = "/v1/servicemind/runs") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 1),
        }
    )


def test_an_identity_longer_than_its_column_is_not_an_identity() -> None:
    over_long = "u" * (IDENTITY_MAX_LENGTH + 1)
    with pytest.raises(ValidationError, match="user_id"):
        TenantContext(tenant_id=TENANT, user_id=over_long, username="analyst")


def test_the_longest_identity_the_column_holds_is_still_accepted() -> None:
    exactly = "u" * IDENTITY_MAX_LENGTH
    context = TenantContext(tenant_id=TENANT, user_id=exactly, username=exactly)
    assert len(context.user_id) == IDENTITY_MAX_LENGTH


@pytest.mark.asyncio
async def test_an_over_long_subject_is_a_401_not_a_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claim arrives signed and valid; it is the platform that cannot hold it.

    ``ValidationError`` subclasses ``ValueError``, which ``get_tenant_context`` already
    maps to a 401 for every other unusable claim, so the rejection needs no new handler
    -- only a contract for the value to violate.
    """
    verifier = stubbed_verifier(monkeypatch, claims(sub="s" * (IDENTITY_MAX_LENGTH + 1)))
    monkeypatch.setattr(auth_module, "oidc_verifier", verifier)

    with pytest.raises(HTTPException) as raised:
        await get_tenant_context(
            http_request(),
            HTTPAuthorizationCredentials(scheme="Bearer", credentials="token"),
        )

    assert raised.value.status_code == 401


@pytest.mark.asyncio
async def test_an_over_long_username_is_a_401_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """The display name is stored beside the subject; it carries the same contract."""
    verifier = stubbed_verifier(
        monkeypatch, claims(preferred_username="n" * (IDENTITY_MAX_LENGTH + 1))
    )
    monkeypatch.setattr(auth_module, "oidc_verifier", verifier)

    with pytest.raises(HTTPException) as raised:
        await get_tenant_context(
            http_request(),
            HTTPAuthorizationCredentials(scheme="Bearer", credentials="token"),
        )

    assert raised.value.status_code == 401


@pytest.mark.asyncio
async def test_a_well_formed_token_still_reaches_its_tenant_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound must not reject the identities the platform actually serves."""
    verifier = stubbed_verifier(monkeypatch, claims())
    monkeypatch.setattr(auth_module, "oidc_verifier", verifier)

    context = await get_tenant_context(
        http_request(),
        HTTPAuthorizationCredentials(scheme="Bearer", credentials="token"),
    )

    assert context.user_id == "analyst-1"
    assert context.username == "analyst"
    assert context.roles == {"analyst"}


def test_a_grant_set_larger_than_an_identity_can_carry_is_not_an_identity() -> None:
    """``glpi_group_ids`` is a claim, so its length is the token's to choose.

    Every entry becomes a ``terms`` clause in the ACL filter of every retrieval that
    identity makes, which makes the size of the search body a number the client supplies.
    The bound belongs where the claim becomes an identity, not at the query that has to
    carry it.
    """
    with pytest.raises(ValueError, match="glpi_group_ids"):
        auth_module._integer_claim_set(
            {"glpi_group_ids": list(range(ACL_SET_MAX_ENTRIES + 1))}, "glpi_group_ids"
        )


def test_the_largest_grant_set_an_identity_can_carry_is_still_accepted() -> None:
    """The bound must not refuse the identities the platform actually serves."""
    granted = auth_module._integer_claim_set(
        {"glpi_entity_ids": list(range(ACL_SET_MAX_ENTRIES))}, "glpi_entity_ids"
    )
    assert len(granted) == ACL_SET_MAX_ENTRIES


@pytest.mark.asyncio
async def test_an_over_large_grant_set_is_a_401_not_a_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same layer as an over-long subject: the token is valid, the identity is not."""
    verifier = stubbed_verifier(
        monkeypatch, claims(glpi_group_ids=list(range(ACL_SET_MAX_ENTRIES + 1)))
    )
    monkeypatch.setattr(auth_module, "oidc_verifier", verifier)

    with pytest.raises(HTTPException) as raised:
        await get_tenant_context(
            http_request(),
            HTTPAuthorizationCredentials(scheme="Bearer", credentials="token"),
        )

    assert raised.value.status_code == 401


def test_a_principal_cannot_hold_an_acl_the_retrieval_cannot_filter_by() -> None:
    """The port states the bound its own ``terms`` filters depend on.

    Built by any other route -- a GLPI profile list, a restored state -- a principal
    still cannot reach a retrieval carrying more grants than the contract allows.
    """
    with pytest.raises(ValidationError, match="entity_ids"):
        RetrievalPrincipal(
            tenant_id=UUID(TENANT),
            user_id="analyst-1",
            entity_ids=frozenset(range(ACL_SET_MAX_ENTRIES + 1)),
        )

    principal = RetrievalPrincipal(
        tenant_id=UUID(TENANT),
        user_id="analyst-1",
        entity_ids=frozenset(range(ACL_SET_MAX_ENTRIES)),
    )
    assert len(principal.entity_ids) == ACL_SET_MAX_ENTRIES
