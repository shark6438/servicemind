from __future__ import annotations

from typing import Any, Protocol

from servicemind.domain.models import ActionIntent
from servicemind.graphrag.build import build_graph_store
from servicemind.integrations.glpi.client import GlpiClient
from servicemind.integrations.glpi.resolver import resolve_glpi_config
from servicemind.persistence.repository import ServiceMindRepository
from servicemind.security.auth import TenantContext
from servicemind.tool_platform.contracts import ProviderResult, ToolCall, ToolDefinition


class GlpiOperationBackend(Protocol):
    async def invoke(self, name: str, arguments: dict[str, Any], call: ToolCall) -> Any: ...

    async def verify(
        self, name: str, arguments: dict[str, Any], output: Any, call: ToolCall
    ) -> bool: ...


class NativeGlpiProvider:
    name = "native_glpi"

    def __init__(self, backend: GlpiOperationBackend | None = None) -> None:
        self.backend = backend or ProductionGlpiBackend()

    async def execute(self, definition: ToolDefinition, call: ToolCall) -> ProviderResult:
        return ProviderResult(
            output=await self.backend.invoke(definition.name, call.arguments, call)
        )

    async def verify(
        self, definition: ToolDefinition, call: ToolCall, result: ProviderResult
    ) -> bool:
        return await self.backend.verify(definition.name, call.arguments, result.output, call)


class ProductionGlpiBackend:
    def _context(self, call: ToolCall) -> TenantContext:
        return TenantContext(
            tenant_id=call.tenant_id,
            user_id=call.user_id,
            username=call.user_id,
            roles=set(call.roles),
            allowed_glpi_entity_ids=set(call.entity_ids),
        )

    async def invoke(self, name: str, arguments: dict[str, Any], call: ToolCall) -> Any:
        if name == "glpi.submit_action_intent":
            intent = ActionIntent.model_validate(arguments)
            intent.verify_integrity()
            if intent.tenant_id != call.tenant_id or intent.run_id != call.run_id:
                raise PermissionError("ActionIntent escaped tool call tenant or run")
            if intent.requested_by != call.user_id:
                raise PermissionError("ActionIntent requester differs from authenticated user")
            stored = await ServiceMindRepository(call.tenant_id).save_action_intent(
                run_id=intent.run_id,
                action_type=intent.action_type,
                target_id=intent.target_id,
                arguments=intent.arguments,
                risk_level=intent.risk_level,
                action_hash=intent.action_hash,
                intent_version=intent.intent_version,
                policy_version=intent.policy_version,
                review_digest=intent.review_digest,
                evidence_digest=intent.evidence_digest,
                evidence_refs=intent.evidence_refs,
                idempotency_context=intent.idempotency_context,
                requested_by=intent.requested_by,
                expires_at=intent.expires_at,
                dry_run_preview=intent.dry_run_preview,
            )
            return {
                "intent_id": str(stored.id),
                "action_hash": stored.action_hash,
                "status": stored.status,
            }

        if name == "glpi.read_resource" and arguments["resource_type"] == "cmdb":
            return {"resource": await self._cmdb(arguments, call)}
        config = await resolve_glpi_config(self._context(call))
        async with GlpiClient(config) as client:
            if name == "glpi.read.ticket":
                ticket = await client.get_ticket(int(arguments["ticket_id"]))
                if ticket.entity and ticket.entity.id not in call.entity_ids:
                    raise PermissionError("ticket entity is outside caller scope")
                return ticket.to_agent_payload()
            if name == "glpi.read.groups":
                return [item.to_agent_payload() for item in await client.list_groups()]
            if name == "glpi.read.ticket_followups":
                return [
                    item.to_agent_payload()
                    for item in await client.list_ticket_followups(int(arguments["ticket_id"]))
                ]
            if name == "glpi.read_resource":
                kind, resource_id = arguments["resource_type"], int(arguments["resource_id"])
                if kind == "ticket":
                    resource = (await client.get_ticket(resource_id)).to_agent_payload()
                else:
                    resource = await client.get_resource(kind, resource_id)
                entity = resource.get("entity")
                if isinstance(entity, dict) and entity.get("id") not in call.entity_ids:
                    raise PermissionError("resource entity is outside caller scope")
                return {"resource": resource}
            if name == "glpi.search_tickets":
                return {
                    "tickets": [
                        item.to_agent_payload()
                        for item in await client.search_tickets(
                            str(arguments["query"]), int(arguments["limit"])
                        )
                    ]
                }
            if name == "glpi.get_ticket_context":
                ticket_id = int(arguments["ticket_id"])
                ticket = await client.get_ticket(ticket_id)
                if ticket.entity and ticket.entity.id not in call.entity_ids:
                    raise PermissionError("ticket entity is outside caller scope")
                return {
                    "ticket": ticket.to_agent_payload(),
                    "followups": [
                        item.to_agent_payload()
                        for item in await client.list_ticket_followups(ticket_id)
                    ],
                    "groups": [item.to_agent_payload() for item in await client.list_groups()],
                }
            if name == "glpi.search_knowledge":
                values = [
                    item.to_agent_payload()
                    for item in await client.search_knowledge_items(
                        str(arguments["query"]), int(arguments["limit"])
                    )
                ]
                allowed = []
                for item in values:
                    raw_entities = item.get("entity_ids")
                    entity_ids = set(raw_entities) if isinstance(raw_entities, list) else set()
                    if not entity_ids or entity_ids & call.entity_ids:
                        allowed.append(item)
                return {"items": allowed[: int(arguments["limit"])]}
        if name == "glpi.query_cmdb_dependencies":
            return await self._cmdb(arguments, call)
        raise LookupError(f"native GLPI operation is not implemented: {name}")

    async def _cmdb(self, arguments: dict[str, Any], call: ToolCall) -> dict[str, Any]:
        store = build_graph_store()
        if store is None:
            raise RuntimeError("tenant CMDB graph is unavailable")
        try:
            matches = await store.match_nodes(
                call.tenant_id,
                identifiers=[str(arguments.get("identifier", arguments.get("resource_id")))],
            )
            graph = await store.subgraph(
                call.tenant_id,
                [item.key for item in matches[:5]],
                max_hops=int(arguments.get("max_hops", 1)),
                max_nodes=200,
            )
            return {
                "nodes": [item.model_dump(mode="json") for item in graph.nodes],
                "edges": [item.model_dump(mode="json") for item in graph.edges],
            }
        finally:
            await store.close()

    async def verify(
        self, name: str, arguments: dict[str, Any], output: Any, call: ToolCall
    ) -> bool:
        if name == "glpi.submit_action_intent":
            stored = await ServiceMindRepository(call.tenant_id).get_action_intent(call.run_id)
            return bool(
                stored
                and str(stored.id) == output.get("intent_id")
                and stored.action_hash == output.get("action_hash")
            )
        if name == "glpi.get_ticket_context":
            return output.get("ticket", {}).get("id") == arguments.get("ticket_id")
        if name == "glpi.read.ticket":
            return output.get("id") == arguments.get("ticket_id")
        if name == "glpi.read_resource":
            return output.get("resource", {}).get("id") in {arguments.get("resource_id"), None}
        return isinstance(output, dict)
