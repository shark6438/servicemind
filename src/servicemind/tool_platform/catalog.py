from __future__ import annotations

from servicemind.domain.models import ACTION_REQUIRED_ROLES, ActionIntent
from servicemind.tool_platform.contracts import (
    DataClassification,
    RetryPolicy,
    ToolAccess,
    ToolDefinition,
    ToolRisk,
)
from servicemind.tool_platform.registry import ToolRegistry

#: The one side-effecting tool the platform exposes, named here because three layers
#: have to agree on the exact string: the catalog that declares it, the harness that
#: calls it, and the provider that implements it. A restated literal is how the harness
#: ends up calling a tool the registry does not declare -- which is precisely the defect
#: this closes, where the write bypassed the registry altogether.
APPEND_FOLLOWUP_TOOL = "glpi.append_ticket_followup"
APPEND_FOLLOWUP_TOOL_VERSION = "1.0.0"


def _object(properties: dict, required: list[str]) -> dict:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def glpi_tool_definitions(provider: str) -> tuple[ToolDefinition, ...]:
    read_retry = RetryPolicy(max_attempts=3, base_delay_ms=100, max_delay_ms=1_000)
    return (
        ToolDefinition(
            name="glpi.read.ticket",
            version="1.0.0",
            provider=provider,
            input_schema=_object({"ticket_id": {"type": "integer", "minimum": 1}}, ["ticket_id"]),
            output_schema={"type": "object"},
            read_write_type=ToolAccess.READ,
            risk_level=ToolRisk.LOW,
            allowed_roles=frozenset({"viewer", "analyst", "approver"}),
            retry_policy=read_retry,
            idempotency_strategy="request_id",
            verification_strategy="ticket_identity_and_entity_scope",
            data_classification=DataClassification.CONFIDENTIAL,
            discoverable=False,
        ),
        ToolDefinition(
            name="glpi.read.groups",
            version="1.0.0",
            provider=provider,
            input_schema=_object({"ticket_id": {"type": "integer", "minimum": 1}}, ["ticket_id"]),
            output_schema={"type": "array", "items": {"type": "object"}, "maxItems": 100},
            read_write_type=ToolAccess.READ,
            risk_level=ToolRisk.LOW,
            allowed_roles=frozenset({"viewer", "analyst", "approver"}),
            retry_policy=read_retry,
            idempotency_strategy="request_id",
            verification_strategy="schema_and_entity_scope",
            data_classification=DataClassification.INTERNAL,
            discoverable=False,
        ),
        ToolDefinition(
            name="glpi.read.ticket_followups",
            version="1.0.0",
            provider=provider,
            input_schema=_object({"ticket_id": {"type": "integer", "minimum": 1}}, ["ticket_id"]),
            output_schema={"type": "array", "items": {"type": "object"}, "maxItems": 500},
            read_write_type=ToolAccess.READ,
            risk_level=ToolRisk.MEDIUM,
            allowed_roles=frozenset({"viewer", "analyst", "approver"}),
            retry_policy=read_retry,
            idempotency_strategy="request_id",
            verification_strategy="ticket_scope",
            data_classification=DataClassification.CONFIDENTIAL,
            discoverable=False,
        ),
        ToolDefinition(
            name="glpi.read_resource",
            version="1.0.0",
            provider=provider,
            input_schema=_object(
                {
                    "resource_type": {"enum": ["ticket", "problem", "change", "cmdb", "knowledge"]},
                    "resource_id": {"type": "integer", "minimum": 1},
                },
                ["resource_type", "resource_id"],
            ),
            output_schema=_object({"resource": {"type": "object"}}, ["resource"]),
            read_write_type=ToolAccess.READ,
            risk_level=ToolRisk.MEDIUM,
            allowed_roles=frozenset({"viewer", "analyst", "approver"}),
            timeout_seconds=20,
            retry_policy=read_retry,
            idempotency_strategy="request_id",
            verification_strategy="resource_identity_and_tenant_scope",
            data_classification=DataClassification.CONFIDENTIAL,
            discoverable=False,
        ),
        ToolDefinition(
            name="glpi.search_tickets",
            version="1.0.0",
            provider=provider,
            input_schema=_object(
                {
                    "query": {"type": "string", "minLength": 1, "maxLength": 500},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                ["query", "limit"],
            ),
            output_schema=_object(
                {"tickets": {"type": "array", "maxItems": 20, "items": {"type": "object"}}},
                ["tickets"],
            ),
            read_write_type=ToolAccess.READ,
            risk_level=ToolRisk.LOW,
            allowed_roles=frozenset({"viewer", "analyst", "approver"}),
            timeout_seconds=15,
            retry_policy=read_retry,
            idempotency_strategy="request_id",
            verification_strategy="schema_and_tenant_scope",
            data_classification=DataClassification.CONFIDENTIAL,
        ),
        ToolDefinition(
            name="glpi.get_ticket_context",
            version="1.0.0",
            provider=provider,
            input_schema=_object({"ticket_id": {"type": "integer", "minimum": 1}}, ["ticket_id"]),
            output_schema=_object(
                {
                    "ticket": {"type": "object"},
                    "followups": {"type": "array", "items": {"type": "object"}},
                    "groups": {"type": "array", "items": {"type": "object"}},
                },
                ["ticket", "followups", "groups"],
            ),
            read_write_type=ToolAccess.READ,
            risk_level=ToolRisk.LOW,
            allowed_roles=frozenset({"viewer", "analyst", "approver"}),
            timeout_seconds=20,
            retry_policy=read_retry,
            idempotency_strategy="request_id",
            verification_strategy="ticket_identity_and_entity_scope",
            data_classification=DataClassification.CONFIDENTIAL,
        ),
        ToolDefinition(
            name="glpi.search_knowledge",
            version="1.0.0",
            provider=provider,
            input_schema=_object(
                {
                    "query": {"type": "string", "minLength": 1, "maxLength": 500},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                ["query", "limit"],
            ),
            output_schema=_object(
                {"items": {"type": "array", "maxItems": 20, "items": {"type": "object"}}},
                ["items"],
            ),
            read_write_type=ToolAccess.READ,
            risk_level=ToolRisk.LOW,
            allowed_roles=frozenset({"viewer", "analyst", "approver"}),
            timeout_seconds=20,
            retry_policy=read_retry,
            idempotency_strategy="request_id",
            verification_strategy="knowledge_acl_scope",
            data_classification=DataClassification.INTERNAL,
        ),
        ToolDefinition(
            name="glpi.query_cmdb_dependencies",
            version="1.0.0",
            provider=provider,
            input_schema=_object(
                {
                    "identifier": {"type": "string", "minLength": 1, "maxLength": 255},
                    "max_hops": {"type": "integer", "minimum": 1, "maximum": 3},
                },
                ["identifier", "max_hops"],
            ),
            output_schema=_object(
                {
                    "nodes": {"type": "array", "maxItems": 200, "items": {"type": "object"}},
                    "edges": {"type": "array", "maxItems": 500, "items": {"type": "object"}},
                },
                ["nodes", "edges"],
            ),
            read_write_type=ToolAccess.READ,
            risk_level=ToolRisk.MEDIUM,
            allowed_roles=frozenset({"analyst", "approver"}),
            timeout_seconds=20,
            retry_policy=read_retry,
            idempotency_strategy="request_id",
            verification_strategy="tenant_scoped_graph_projection",
            data_classification=DataClassification.CONFIDENTIAL,
        ),
        ToolDefinition(
            name="glpi.submit_action_intent",
            version="1.0.0",
            provider=provider,
            input_schema=ActionIntent.model_json_schema(),
            output_schema=_object(
                {
                    "intent_id": {"type": "string", "format": "uuid"},
                    "action_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "status": {"const": "proposed"},
                },
                ["intent_id", "action_hash", "status"],
            ),
            read_write_type=ToolAccess.SUBMIT_INTENT,
            risk_level=ToolRisk.MEDIUM,
            allowed_roles=frozenset({"analyst"}),
            timeout_seconds=10,
            retry_policy=RetryPolicy(max_attempts=1),
            idempotency_strategy="action_hash",
            verification_strategy="postgres_action_intent_readback",
            data_classification=DataClassification.RESTRICTED,
        ),
        ToolDefinition(
            name=APPEND_FOLLOWUP_TOOL,
            version=APPEND_FOLLOWUP_TOOL_VERSION,
            provider=provider,
            input_schema=_object(
                {
                    "ticket_id": {"type": "integer", "minimum": 1},
                    # The body exactly as approved. The platform's own marker is a
                    # separate argument rather than a suffix on this one, so that what
                    # the policy hashes and the provider compares is the approved text
                    # and not the approved text plus platform bookkeeping.
                    "content": {"type": "string", "minLength": 1, "maxLength": 60_000},
                    "idempotency_marker": {"type": "string", "minLength": 1, "maxLength": 255},
                    "is_private": {"type": "boolean"},
                },
                ["ticket_id", "content", "idempotency_marker", "is_private"],
            ),
            output_schema=_object(
                {
                    "followup_id": {"type": "integer"},
                    "ticket_id": {"type": "integer"},
                    "duplicate_suppressed": {"type": "boolean"},
                    "approved_content": {"type": "string"},
                },
                ["followup_id", "ticket_id", "duplicate_suppressed", "approved_content"],
            ),
            read_write_type=ToolAccess.WRITE,
            risk_level=ToolRisk.HIGH,
            # The registry states a role set per tool; this one is the same set the graph
            # requires before it will spend an approval (``ACTION_REQUIRED_ROLES``), taken
            # from that constant rather than restated, so the two gates cannot disagree
            # about who may write to a tenant's ticket.
            allowed_roles=ACTION_REQUIRED_ROLES,
            requires_approval=True,
            timeout_seconds=30,
            # One attempt: a retried side effect outside the durable harness is a second
            # write, and the harness owns the reconcile-and-suppress decision.
            retry_policy=RetryPolicy(max_attempts=1),
            idempotency_strategy="glpi_read_reconcile_by_action_marker",
            verification_strategy="glpi_read_after_write_content_equality",
            data_classification=DataClassification.RESTRICTED,
            # Not model-reachable: the harness constructs this call, and a model that
            # could discover it could ask for it.
            discoverable=False,
        ),
    )


def build_glpi_registry(provider: str) -> ToolRegistry:
    registry = ToolRegistry()
    for definition in glpi_tool_definitions(provider):
        registry.register(definition)
    return registry
