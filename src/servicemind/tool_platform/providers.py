from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from servicemind.domain.evidence import self_authored_marker
from servicemind.domain.knowledge import RetrievalPrincipal
from servicemind.domain.models import ActionIntent
from servicemind.graphrag.build import build_graph_store
from servicemind.integrations.glpi.client import GlpiClient
from servicemind.integrations.glpi.models import normalized_text
from servicemind.integrations.glpi.resolver import resolve_glpi_config
from servicemind.persistence.models import ActionStatus
from servicemind.persistence.repository import ServiceMindRepository
from servicemind.security.auth import TenantContext
from servicemind.tool_platform.catalog import APPEND_FOLLOWUP_TOOL
from servicemind.tool_platform.contracts import ProviderResult, ToolCall, ToolDefinition
from servicemind.tool_platform.gateway import ToolVerificationFailed

#: The intent statuses a write may be performed under. Both mean a human approval
#: stands behind these exact bytes: ``APPROVED`` is an unclaimed approval, and
#: ``EXECUTING`` is one the executor has claimed and is performing right now -- the
#: claim and the write are a single act, so the executor marks the row executing before
#: it calls the gateway. Reading only ``APPROVED`` therefore refused every write the
#: platform can make, and the tests did not see it because their repository fake
#: answered each read with the status it was constructed with, hiding the transition the
#: executor had just made. Terminal statuses stay out: a failed attempt is not a
#: standing approval, and a duplicate of a completed write is the executor's idempotency
#: record to reconcile, not something a second call may re-perform.
_APPROVAL_STANDS = frozenset({ActionStatus.APPROVED.value, ActionStatus.EXECUTING.value})


def body_matches(stored: str, expected_text: str) -> bool:
    """Is the stored followup, as text, what was approved?

    The check used to be ``marker in html_to_text(followup.content)``, which answers a
    different question: *this row exists*. A body truncated by a field limit, replaced by
    an editor, or cut off at the marker all keep the marker and all passed -- so a run
    whose write did not land reported ``verified=True``. The read-back is the only
    evidence the platform has that the write reached GLPI as approved, and it has to
    compare the content, not the label.

    Compared through ``normalized_text`` rather than literally, because GLPI stores
    newlines as markup and returns them that way; the normalization is the platform's
    single definition of "the same text", shared with the acceptance grader so a write
    cannot be verified by one and rejected by the other.
    """
    return normalized_text(stored) == expected_text


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
            allowed_glpi_group_ids=set(call.group_ids),
        )

    async def _approved_intent(self, call: ToolCall):
        """Resolve the approval the call cites, or refuse to write.

        The policy engine can only check that ``approval_ref`` is present and that
        ``approval_binding`` equals the digest the caller itself computed; both are
        supplied by the caller, so on their own they prove a caller filled two fields
        consistently. The reference is only worth carrying if something durable answers
        for it, and the durable thing is the ActionIntent: a row this tenant's run
        persisted, holding the action hash the human approved. Without this check the
        gateway would be a pass-through with an approval-shaped argument.
        """
        prefix = "action-intent://"
        reference = call.approval_ref or ""
        if not reference.startswith(prefix):
            raise PermissionError("a side-effecting call must cite the ActionIntent it acts on")
        try:
            intent_id = UUID(reference[len(prefix) :])
        except ValueError as exc:
            raise PermissionError("the approval reference is not an ActionIntent id") from exc
        stored = await ServiceMindRepository(call.tenant_id).get_action_intent(call.run_id)
        if stored is None or stored.id != intent_id:
            raise PermissionError("no ActionIntent matches the approval this call cites")
        if stored.status not in _APPROVAL_STANDS:
            raise PermissionError("the cited ActionIntent is not approved")
        return stored

    async def _append_followup(self, arguments: dict[str, Any], call: ToolCall) -> dict[str, Any]:
        """Write one approved followup, or recognise the one a crash already wrote.

        The bytes are pinned to the approved intent rather than trusted from the caller:
        a tool call that carries an approval is a claim about *which* action is being
        performed, and everything after it -- the body, the target, the visibility, the
        idempotency marker -- has to agree with the row the approval names. Otherwise the
        approval would authorise "some write by this run", which is not what a human
        approved.
        """
        stored = await self._approved_intent(call)
        content = str(arguments["content"])
        marker = str(arguments["idempotency_marker"])
        ticket_id = int(arguments["ticket_id"])
        is_private = bool(arguments["is_private"])
        approved = stored.arguments if isinstance(stored.arguments, dict) else {}
        if content != str(approved.get("content", "")):
            raise PermissionError("the content being written is not the content that was approved")
        if ticket_id != stored.target_id:
            raise PermissionError("the ticket being written to is not the approved target")
        if is_private != bool(approved.get("is_private", True)):
            raise PermissionError("the visibility being written is not the approved visibility")
        if marker != self_authored_marker(call.run_id, stored.action_hash):
            raise PermissionError("the idempotency marker does not name the approved action")

        expected_text = normalized_text(f"{content}\n{marker}")
        config = await resolve_glpi_config(self._context(call))
        async with GlpiClient(config) as client:
            # Reconcile before writing: this recovers a crash between GLPI committing and
            # the harness's own idempotency row completing. The marker is the *identity*
            # of the write -- run plus action hash -- so it is what finds the row. What
            # it must not be is the only thing checked: a followup carrying our marker
            # with a body that is not the approved one is a persisted effect nobody
            # approved, and reporting it as a suppressed duplicate would report success
            # for it.
            existing = await client.list_ticket_followups(ticket_id)
            duplicate = next(
                (item for item in existing if marker in normalized_text(item.content)),
                None,
            )
            if duplicate is not None:
                if not body_matches(duplicate.content, expected_text):
                    raise RuntimeError(
                        "GLPI already holds this run's followup, and its body is not the "
                        "approved content: the persisted effect differs from the intent, so "
                        "it is neither this write nor a safe duplicate"
                    )
                return {
                    "followup_id": duplicate.id,
                    "ticket_id": ticket_id,
                    "duplicate_suppressed": True,
                    "approved_content": content,
                }
            followup = await client.append_ticket_followup(
                ticket_id, f"{content}\n{marker}", is_private=is_private
            )
        return {
            "followup_id": followup.id,
            "ticket_id": ticket_id,
            "duplicate_suppressed": False,
            "approved_content": content,
        }

    async def _read_back_followup(
        self, arguments: dict[str, Any], output: Any, call: ToolCall
    ) -> bool:
        """Ask GLPI what it stored, and compare that to what was approved.

        Deliberately a fresh read rather than the write's own response: a provider that
        truncates or rewrites the field can return the body it was given while storing
        something else, and only a read that does not go through the write path can tell
        the difference. A row that cannot be found at all is not a verified write either.
        """
        if not isinstance(output, dict):
            return False
        marker = str(arguments["idempotency_marker"])
        expected_text = normalized_text(f"{arguments['content']}\n{marker}")
        config = await resolve_glpi_config(self._context(call))
        async with GlpiClient(config) as client:
            rows = await client.list_ticket_followups(int(arguments["ticket_id"]))
        stored = next((item for item in rows if item.id == output.get("followup_id")), None)
        if stored is None:
            return False
        if not body_matches(stored.content, expected_text):
            raise ToolVerificationFailed(
                "GLPI read-after-write verification failed: the stored followup is not "
                "the approved content"
            )
        return True

    async def invoke(self, name: str, arguments: dict[str, Any], call: ToolCall) -> Any:
        if name == APPEND_FOLLOWUP_TOOL:
            return await self._append_followup(arguments, call)
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
            # The call's own scope, not its tenant: the graph is reachable from here as
            # well as from the knowledge agent, and a read path that hands the store a
            # tenant id is a read path that does not filter. ``ToolCall`` carries the
            # group coordinate for the same reason it carries the entity one -- both are
            # read by this filter, and both are absent from an empty call.
            principal = RetrievalPrincipal(
                tenant_id=call.tenant_id,
                user_id=call.user_id,
                entity_ids=call.entity_ids,
                group_ids=call.group_ids,
            )
            matches = await store.match_nodes(
                principal,
                identifiers=[str(arguments.get("identifier", arguments.get("resource_id")))],
            )
            graph = await store.subgraph(
                principal,
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
        if name == APPEND_FOLLOWUP_TOOL:
            return await self._read_back_followup(arguments, output, call)
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
        if name == "glpi.read.groups":
            # Declared strategy: schema_and_entity_scope. This read returns an array,
            # so the dict-shaped fallback below would reject every successful call and
            # surface as a spurious ToolVerificationFailed. A group that carries no
            # entity is accepted, matching the convention used by glpi.read.ticket and
            # glpi.read_resource; a group that names a foreign entity is not.
            return isinstance(output, list) and all(
                isinstance(item, dict)
                and (
                    not isinstance(item.get("entity"), dict)
                    or item["entity"].get("id") in call.entity_ids
                )
                for item in output
            )
        if name == "glpi.read.ticket_followups":
            # Declared strategy: ticket_scope. Also an array. A followup that cannot be
            # attributed to the requested ticket is not evidence of scope, so this one
            # fails closed instead.
            return isinstance(output, list) and all(
                isinstance(item, dict) and item.get("ticket_id") == arguments.get("ticket_id")
                for item in output
            )
        if name == "glpi.read_resource":
            return output.get("resource", {}).get("id") in {arguments.get("resource_id"), None}
        return isinstance(output, dict)
