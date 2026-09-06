import asyncio
import time
from typing import Annotated, Any
from uuid import UUID

import httpx
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from core import settings


class TenantContext(BaseModel):
    tenant_id: UUID
    user_id: str
    username: str
    roles: set[str] = Field(default_factory=set)
    allowed_glpi_entity_ids: set[int] = Field(default_factory=set)

    def require_role(self, role: str) -> None:
        if role not in self.roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role {role!r} is required",
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
        entity_claim = claims.get("glpi_entity_ids", [])
        if isinstance(entity_claim, str):
            entity_claim = [entity_claim]
        roles = set(claims.get("realm_access", {}).get("roles", []))
        return TenantContext(
            tenant_id=UUID(str(claims["tenant_id"])),
            user_id=str(claims["sub"]),
            username=str(claims.get("preferred_username", claims["sub"])),
            roles=roles,
            allowed_glpi_entity_ids={int(value) for value in entity_claim},
        )


oidc_verifier = OIDCVerifier()
bearer_scheme = HTTPBearer(description="ServiceMind Keycloak access token")


async def get_tenant_context(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
) -> TenantContext:
    try:
        return await oidc_verifier.verify(credentials.credentials)
    except (jwt.PyJWTError, httpx.HTTPError, RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


TenantContextDependency = Annotated[TenantContext, Depends(get_tenant_context)]
