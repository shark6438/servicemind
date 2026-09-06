import asyncio
import time
from dataclasses import dataclass
from typing import Any, Self

import httpx

from core import settings
from servicemind.integrations.glpi.models import (
    GlpiFollowup,
    GlpiGroup,
    GlpiKnowbaseItem,
    GlpiTicket,
    GlpiTokenResponse,
)


class GlpiAPIError(RuntimeError):
    """A sanitized GLPI integration error suitable for service logs and tools."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"GLPI API request failed ({status_code}): {detail}")
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class GlpiClientConfig:
    base_url: str
    api_version: str
    client_id: str
    client_secret: str
    username: str
    password: str
    entity_id: int = 0
    profile_id: int = 6
    timeout_seconds: float = 15.0
    trust_env: bool = False

    @classmethod
    def from_settings(cls) -> Self:
        required = {
            "GLPI_BASE_URL": settings.GLPI_BASE_URL,
            "GLPI_CLIENT_ID": settings.GLPI_CLIENT_ID,
            "GLPI_CLIENT_SECRET": settings.GLPI_CLIENT_SECRET,
            "GLPI_USERNAME": settings.GLPI_USERNAME,
            "GLPI_PASSWORD": settings.GLPI_PASSWORD,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(f"Missing GLPI settings: {', '.join(missing)}")

        assert settings.GLPI_BASE_URL is not None
        assert settings.GLPI_CLIENT_ID is not None
        assert settings.GLPI_CLIENT_SECRET is not None
        assert settings.GLPI_USERNAME is not None
        assert settings.GLPI_PASSWORD is not None
        return cls(
            base_url=settings.GLPI_BASE_URL.rstrip("/"),
            api_version=settings.GLPI_API_VERSION.strip("/"),
            client_id=settings.GLPI_CLIENT_ID.get_secret_value(),
            client_secret=settings.GLPI_CLIENT_SECRET.get_secret_value(),
            username=settings.GLPI_USERNAME,
            password=settings.GLPI_PASSWORD.get_secret_value(),
            entity_id=settings.GLPI_ENTITY_ID,
            profile_id=settings.GLPI_PROFILE_ID,
            timeout_seconds=settings.GLPI_TIMEOUT_SECONDS,
            trust_env=settings.GLPI_TRUST_ENV,
        )


class GlpiClient:
    """Async client for the GLPI 11 High-Level API with OAuth token caching."""

    def __init__(
        self,
        config: GlpiClientConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            transport=transport,
            trust_env=config.trust_env,
        )
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    @classmethod
    def from_settings(cls) -> Self:
        return cls(GlpiClientConfig.from_settings())

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _access_token(self, *, force_refresh: bool = False) -> str:
        if not force_refresh and self._token and time.monotonic() < self._token_expires_at:
            return self._token

        async with self._token_lock:
            if not force_refresh and self._token and time.monotonic() < self._token_expires_at:
                return self._token

            response = await self._client.post(
                "/api.php/token",
                data={
                    "grant_type": "password",
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "username": self.config.username,
                    "password": self.config.password,
                    "scope": "api",
                },
            )
            data = self._response_json(response)
            token = GlpiTokenResponse.model_validate(data)
            self._token = token.access_token
            self._token_expires_at = time.monotonic() + max(token.expires_in - 30, 1)
            return self._token

    def _response_json(self, response: httpx.Response) -> Any:
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.is_error:
            detail = "Unexpected response"
            if isinstance(data, dict):
                detail = str(data.get("detail") or data.get("title") or detail)
            raise GlpiAPIError(response.status_code, detail)
        return data

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        for attempt in range(2):
            token = await self._access_token(force_refresh=attempt == 1)
            headers = {
                "Authorization": f"Bearer {token}",
                "GLPI-Entity": str(self.config.entity_id),
                "GLPI-Profile": str(self.config.profile_id),
                "GLPI-Entity-Recursive": "false",
                "Accept-Language": "en_GB",
                **kwargs.pop("headers", {}),
            }
            response = await self._client.request(method, path, headers=headers, **kwargs)
            if response.status_code != 401 or attempt == 1:
                return self._response_json(response)
        raise AssertionError("unreachable")

    @property
    def api_prefix(self) -> str:
        return f"/api.php/{self.config.api_version}"

    async def get_session(self) -> dict[str, Any]:
        data = await self._request("GET", f"{self.api_prefix}/session")
        if not isinstance(data, dict):
            raise GlpiAPIError(502, "Unexpected session response")
        return data

    async def get_ticket(self, ticket_id: int) -> GlpiTicket:
        if ticket_id < 1:
            raise ValueError("ticket_id must be positive")
        data = await self._request("GET", f"{self.api_prefix}/Assistance/Ticket/{ticket_id}")
        return GlpiTicket.model_validate(data)

    async def list_recent_tickets(self, limit: int = 5) -> list[GlpiTicket]:
        if not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        data = await self._request(
            "GET",
            f"{self.api_prefix}/Assistance/Ticket",
            params={"limit": limit, "sort": "id:desc"},
        )
        if not isinstance(data, list):
            raise GlpiAPIError(502, "Unexpected ticket list response")
        return [GlpiTicket.model_validate(item) for item in data]

    async def list_groups(self, limit: int = 50) -> list[GlpiGroup]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        data = await self._request(
            "GET",
            f"{self.api_prefix}/Administration/Group",
            params={"limit": limit, "sort": "name:asc"},
        )
        if not isinstance(data, list):
            raise GlpiAPIError(502, "Unexpected group list response")
        return [GlpiGroup.model_validate(item) for item in data]

    async def list_knowledge_items(self, limit: int = 100) -> list[GlpiKnowbaseItem]:
        data = await self._request(
            "GET", f"{self.api_prefix}/Tools/KnowbaseItem", params={"limit": limit}
        )
        if not isinstance(data, list):
            raise GlpiAPIError(502, "Unexpected knowledge item list response")
        return [GlpiKnowbaseItem.model_validate(item) for item in data]

    async def append_ticket_followup(
        self, ticket_id: int, content: str, *, is_private: bool = True
    ) -> GlpiFollowup:
        if ticket_id < 1:
            raise ValueError("ticket_id must be positive")
        if not content.strip():
            raise ValueError("followup content cannot be empty")
        created = await self._request(
            "POST",
            f"{self.api_prefix}/Assistance/Ticket/{ticket_id}/Timeline/Followup",
            json={"content": content, "is_private": is_private},
        )
        followup_id = int(created["id"])
        return await self.get_ticket_followup(ticket_id, followup_id)

    async def get_ticket_followup(self, ticket_id: int, followup_id: int) -> GlpiFollowup:
        data = await self._request(
            "GET",
            f"{self.api_prefix}/Assistance/Ticket/{ticket_id}/Timeline/Followup/{followup_id}",
        )
        return GlpiFollowup.model_validate(data)

    async def list_ticket_followups(self, ticket_id: int) -> list[GlpiFollowup]:
        data = await self._request(
            "GET",
            f"{self.api_prefix}/Assistance/Ticket/{ticket_id}/Timeline/Followup",
        )
        if not isinstance(data, list):
            raise GlpiAPIError(502, "Unexpected followup list response")
        return [
            GlpiFollowup.model_validate(item.get("item", item) if isinstance(item, dict) else item)
            for item in data
        ]
