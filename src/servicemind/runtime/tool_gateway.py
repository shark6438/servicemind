from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import uuid4

from core import settings
from servicemind.integrations.glpi.client import GlpiClient
from servicemind.integrations.glpi.resolver import resolve_glpi_config
from servicemind.runtime.contracts import AgentInvocationContext
from servicemind.security.auth import TenantContext
from servicemind.tool_platform.contracts import ToolCall
from servicemind.tool_platform.runtime import build_tool_gateway


class DataToolName(StrEnum):
    GET_TICKET = "glpi.read.ticket"
    LIST_GROUPS = "glpi.read.groups"
    LIST_FOLLOWUPS = "glpi.read.ticket_followups"


@dataclass(frozen=True)
class TenantGlpiReadGateway:
    """Server-side GLPI read boundary; credentials never enter model-visible state."""

    async def execute(
        self,
        *,
        invocation: AgentInvocationContext,
        tenant_context: TenantContext,
        tool_name: DataToolName,
        ticket_id: int,
    ) -> Any:
        invocation.ensure_active()
        invocation.require_capability(tool_name.value)
        if tenant_context.tenant_id != invocation.tenant_id:
            raise PermissionError("Tool context tenant does not match invocation tenant")

        if settings.SERVICEMIND_TOOL_PLATFORM_ENABLED:
            result = await build_tool_gateway().execute(
                ToolCall(
                    request_id=uuid4(),
                    tenant_id=invocation.tenant_id,
                    run_id=invocation.run_id,
                    task_id=invocation.task_id,
                    user_id=tenant_context.user_id,
                    roles=frozenset(tenant_context.roles),
                    entity_ids=frozenset(tenant_context.allowed_glpi_entity_ids),
                    capabilities=invocation.allowed_capabilities,
                    tool_name=tool_name.value,
                    tool_version="1.0.0",
                    arguments={"ticket_id": ticket_id},
                    deadline=invocation.deadline,
                )
            )
            return result.output

        config = await resolve_glpi_config(tenant_context)
        async with GlpiClient(config) as client:
            if tool_name is DataToolName.GET_TICKET:
                return (await client.get_ticket(ticket_id)).to_agent_payload()
            if tool_name is DataToolName.LIST_GROUPS:
                return [item.to_agent_payload() for item in await client.list_groups()]
            if tool_name is DataToolName.LIST_FOLLOWUPS:
                followups = await client.list_ticket_followups(ticket_id)
                return [item.model_dump(mode="json") for item in followups]
        raise PermissionError(f"Unsupported read tool: {tool_name}")


tenant_glpi_read_gateway = TenantGlpiReadGateway()
