import httpx
import pytest

from servicemind.integrations.glpi.client import GlpiClient, GlpiClientConfig
from servicemind.integrations.glpi.models import html_to_text


def config() -> GlpiClientConfig:
    return GlpiClientConfig(
        base_url="http://glpi.test",
        api_version="v2.3",
        client_id="client-id",
        client_secret="client-secret",
        username="agent-user",
        password="agent-password",
        entity_id=12,
        profile_id=6,
    )


def ticket_payload(ticket_id: int = 42) -> dict:
    return {
        "id": ticket_id,
        "name": "VPN MFA failure",
        "content": "<p>User cannot connect after <strong>MFA</strong>.</p>",
        "type": 1,
        "urgency": 4,
        "impact": 3,
        "priority": 4,
        "external_id": "SERVICEMIND-42",
        "status": {"id": 1, "name": "New"},
        "entity": {"id": 12, "name": "Shanghai"},
        "team": [],
        "ignored_glpi_field": "not exposed to the agent",
    }


@pytest.mark.asyncio
async def test_get_ticket_authenticates_and_applies_scope_headers() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api.php/token":
            return httpx.Response(
                200,
                json={"token_type": "Bearer", "expires_in": 3600, "access_token": "token-1"},
            )
        assert request.headers["Authorization"] == "Bearer token-1"
        assert request.headers["GLPI-Entity"] == "12"
        assert request.headers["GLPI-Profile"] == "6"
        assert request.headers["GLPI-Entity-Recursive"] == "false"
        return httpx.Response(200, json=ticket_payload())

    async with GlpiClient(config(), transport=httpx.MockTransport(handler)) as client:
        ticket = await client.get_ticket(42)

    assert [request.url.path for request in requests] == [
        "/api.php/token",
        "/api.php/v2.3/Assistance/Ticket/42",
    ]
    assert ticket.to_agent_payload()["content"] == "User cannot connect after MFA ."
    assert "ignored_glpi_field" not in ticket.to_agent_payload()


@pytest.mark.asyncio
async def test_list_recent_tickets_bounds_and_sorting() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api.php/token":
            return httpx.Response(
                200,
                json={"token_type": "Bearer", "expires_in": 3600, "access_token": "token-1"},
            )
        assert request.url.params["limit"] == "2"
        assert request.url.params["sort"] == "id:desc"
        return httpx.Response(200, json=[ticket_payload(2), ticket_payload(1)])

    async with GlpiClient(config(), transport=httpx.MockTransport(handler)) as client:
        tickets = await client.list_recent_tickets(2)
        with pytest.raises(ValueError, match="between 1 and 20"):
            await client.list_recent_tickets(21)

    assert [ticket.id for ticket in tickets] == [2, 1]


@pytest.mark.asyncio
async def test_unauthorized_response_refreshes_token_once() -> None:
    token_calls = 0
    ticket_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls, ticket_calls
        if request.url.path == "/api.php/token":
            token_calls += 1
            return httpx.Response(
                200,
                json={
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "access_token": f"token-{token_calls}",
                },
            )
        ticket_calls += 1
        if ticket_calls == 1:
            return httpx.Response(401, json={"detail": "Expired token"})
        assert request.headers["Authorization"] == "Bearer token-2"
        return httpx.Response(200, json=ticket_payload())

    async with GlpiClient(config(), transport=httpx.MockTransport(handler)) as client:
        ticket = await client.get_ticket(42)

    assert ticket.id == 42
    assert token_calls == 2
    assert ticket_calls == 2


def test_html_to_text_discards_markup() -> None:
    assert html_to_text("<p>Hello &amp; <script>ignore()</script>world</p>") == "Hello & world"


@pytest.mark.asyncio
async def test_followup_list_unwraps_glpi_timeline_items() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api.php/token":
            return httpx.Response(
                200,
                json={"token_type": "Bearer", "expires_in": 3600, "access_token": "token-1"},
            )
        return httpx.Response(
            200,
            json=[
                {
                    "type": "Followup",
                    "item": {
                        "id": 9,
                        "items_id": 42,
                        "content": "<p>Verified</p>",
                        "is_private": True,
                    },
                }
            ],
        )

    async with GlpiClient(config(), transport=httpx.MockTransport(handler)) as client:
        followups = await client.list_ticket_followups(42)

    assert followups[0].id == 9
    assert followups[0].to_agent_payload()["content"] == "Verified"


@pytest.mark.asyncio
async def test_list_groups_is_tenant_scoped_and_typed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api.php/token":
            return httpx.Response(
                200,
                json={"token_type": "Bearer", "expires_in": 3600, "access_token": "token"},
            )
        assert request.headers["GLPI-Entity"] == "12"
        assert request.url.path.endswith("/Administration/Group")
        return httpx.Response(
            200,
            json=[{"id": 5, "name": "Network Team", "entity": {"id": 1}}],
        )

    async with GlpiClient(config(), transport=httpx.MockTransport(handler)) as client:
        groups = await client.list_groups()
    assert groups[0].id == 5
    assert groups[0].name == "Network Team"
    assert groups[0].entity and groups[0].entity.id == 1
