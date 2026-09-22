import asyncio
import time
from typing import Annotated, Any
from uuid import UUID

import httpx
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from core import settings


def _integer_claim_set(claims: dict[str, Any], name: str) -> set[int]:
    raw = claims.get(name, [])
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError(f"OIDC claim {name!r} must be a list of non-negative integers")
    values: set[int] = set()
    for value in raw:
        if isinstance(value, bool) or not (
            (isinstance(value, int) and value >= 0)
            or (isinstance(value, str) and value.isdecimal())
        ):
            raise ValueError(f"OIDC claim {name!r} must be a list of non-negative integers")
        values.add(int(value))
    return values


class TenantContext(BaseModel):
    tenant_id: UUID
    user_id: str
    username: str
    roles: set[str] = Field(default_factory=set)
    allowed_glpi_entity_ids: set[int] = Field(default_factory=set)
    allowed_glpi_group_ids: set[int] = Field(default_factory=set)

    def require_role(self, role: str) -> None:
        if role not in self.roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role {role!r} is required",
            )

    def require_any_role(self, *roles: str) -> None:
        """Require at least one role without leaking which extra roles a user has."""
        if not self.roles.intersection(roles):
            expected = ", ".join(repr(role) for role in roles)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"One of roles {expected} is required",
            )


class OIDCVerifier:
    def __init__(self) -> None:
        self._jwks: dict[str, Any] | None = None
        self._jwks_expires_at = 0.0
        self._lock = asyncio.Lock()

    def _configuration(self) -> tuple[str, str, str]:
        issuer = settings.SERVICEMIND_OIDC_ISSUER
        jwks_url = settings.SERVICEMIND_OIDC_JWKS_URL
        audience = settings.SERVICEMIND_OIDC_AUDIENCE
        if not issuer or not jwks_url:
            raise RuntimeError("ServiceMind OIDC settings are incomplete")
        return issuer, jwks_url, audience

    async def _load_jwks(self, *, force: bool = False) -> dict[str, Any]:
        if not force and self._jwks and time.monotonic() < self._jwks_expires_at:
            return self._jwks
        async with self._lock:
            if not force and self._jwks and time.monotonic() < self._jwks_expires_at:
                return self._jwks
            _, jwks_url, _ = self._configuration()
            async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                response = await client.get(jwks_url)
                response.raise_for_status()
                jwks = response.json()
            if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
                raise RuntimeError("OIDC server returned an invalid JWKS document")
            self._jwks = jwks
            self._jwks_expires_at = time.monotonic() + 300
            return jwks

    async def verify(self, token: str) -> TenantContext:
        issuer, _, audience = self._configuration()
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        jwks = await self._load_jwks()
        key_data = next((key for key in jwks["keys"] if key.get("kid") == kid), None)
        if key_data is None:
            jwks = await self._load_jwks(force=True)
            key_data = next((key for key in jwks["keys"] if key.get("kid") == kid), None)
        if key_data is None:
            raise jwt.InvalidKeyError("Token signing key is unknown")

        signing_key = jwt.PyJWK.from_dict(key_data).key
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "iat", "iss", "sub", "tenant_id"]},
        )
        realm_access = claims.get("realm_access", {})
        if not isinstance(realm_access, dict):
            raise ValueError("OIDC realm_access claim must be an object")
        raw_roles = realm_access.get("roles", [])
        if not isinstance(raw_roles, list) or any(not isinstance(role, str) for role in raw_roles):
            raise ValueError("OIDC roles claim must be a list of strings")
        roles = set(raw_roles)
        return TenantContext(
            tenant_id=UUID(str(claims["tenant_id"])),
            user_id=str(claims["sub"]),
            username=str(claims.get("preferred_username", claims["sub"])),
            roles=roles,
            allowed_glpi_entity_ids=_integer_claim_set(claims, "glpi_entity_ids"),
            allowed_glpi_group_ids=_integer_claim_set(claims, "glpi_group_ids"),
        )


oidc_verifier = OIDCVerifier()
bearer_scheme = HTTPBearer(description="ServiceMind Keycloak access token", auto_error=False)


async def get_tenant_context(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> TenantContext:
    challenge = "Bearer"
    if request.url.path.startswith("/v1/servicemind/mcp"):
        configured = settings.SERVICEMIND_MCP_PUBLIC_URL
        resource = (
            configured.rstrip("/")
            if configured
            else (f"{str(request.base_url).rstrip('/')}/v1/servicemind/mcp")
        )
        metadata = (
            f"{resource.split('/v1/servicemind/mcp', 1)[0]}"
            "/.well-known/oauth-protected-resource/v1/servicemind/mcp"
        )
        challenge = f'Bearer resource_metadata="{metadata}"'
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Access token is required",
            headers={"WWW-Authenticate": challenge},
        )
    try:
        return await oidc_verifier.verify(credentials.credentials)
    except (jwt.PyJWTError, httpx.HTTPError, RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token",
            headers={"WWW-Authenticate": challenge},
        ) from exc


TenantContextDependency = Annotated[TenantContext, Depends(get_tenant_context)]
