from __future__ import annotations

from servicemind.domain.models import ActionIntent
from servicemind.tool_platform.contracts import (
    DataClassification,
    RetryPolicy,
    ToolAccess,
    ToolDefinition,
    ToolRisk,
)
from servicemind.tool_platform.registry import ToolRegistry


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
    )


def build_glpi_registry(provider: str) -> ToolRegistry:
    registry = ToolRegistry()
    for definition in glpi_tool_definitions(provider):
        registry.register(definition)
    return registry
