from typing import Annotated

from langchain_core.tools import tool
from pydantic import Field

from servicemind.integrations.glpi.client import GlpiClient


@tool
async def glpi_get_ticket(ticket_id: Annotated[int, Field(ge=1)]) -> dict[str, object]:
    """Read one GLPI ticket by its numeric ID and return a sanitized ITSM record."""
    async with GlpiClient.from_settings() as client:
        ticket = await client.get_ticket(ticket_id)
    return ticket.to_agent_payload()


@tool
async def glpi_list_recent_tickets(
    limit: Annotated[int, Field(ge=1, le=20)] = 5,
) -> list[dict[str, object]]:
    """List the most recently created GLPI tickets, up to 20 records."""
    async with GlpiClient.from_settings() as client:
        tickets = await client.list_recent_tickets(limit)
    return [ticket.to_agent_payload() for ticket in tickets]


GLPI_READ_TOOLS = [glpi_get_ticket, glpi_list_recent_tickets]
