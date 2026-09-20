from __future__ import annotations

import json
from urllib.parse import urlparse

import httpx

from servicemind.memory.contracts import SECRET_PATTERN
from servicemind.memory.policy import contains_injection_marker
from servicemind.tool_platform.contracts import (
    ToolCall,
    ToolDefinition,
    ToolPolicyDecision,
    ToolRisk,
)


def _argument_text(call: ToolCall) -> str:
    return json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)


class DeterministicToolPolicy:
    """Fail-closed local baseline used even when an external PDP is unavailable."""

    def __init__(self, *, policy_version: str = "servicemind-tool-policy-v1") -> None:
        self.policy_version = policy_version

    async def decide(self, definition: ToolDefinition, call: ToolCall) -> ToolPolicyDecision:
        reasons: list[str] = []
        body = _argument_text(call)
        if call.tool_name not in call.capabilities:
            reasons.append("CAPABILITY_DENIED")
        if not definition.allowed_roles.intersection(call.roles):
            reasons.append("ROLE_DENIED")
        if definition.allowed_entities and not definition.allowed_entities.intersection(
            call.entity_ids
        ):
            reasons.append("ENTITY_DENIED")
        if call.taint_labels:
            reasons.append("UNRESOLVED_TAINT")
        if SECRET_PATTERN.search(body):
            reasons.append("SECRET_IN_TOOL_ARGUMENT")
        if contains_injection_marker(body):
            reasons.append("PROMPT_INJECTION_IN_TOOL_ARGUMENT")
        if (
            definition.risk_level in {ToolRisk.HIGH, ToolRisk.CRITICAL}
            and not definition.requires_approval
        ):
            reasons.append("HIGH_RISK_REQUIRES_APPROVAL")
        if definition.requires_approval and (
            not call.approval_ref or call.approval_binding != call.approval_digest
        ):
            reasons.append("APPROVAL_MISSING_OR_STALE")
        return ToolPolicyDecision(
            allow=not reasons,
            requires_approval=definition.requires_approval,
            allowed_fields=frozenset(definition.input_schema.get("properties", {})),
            reason_codes=tuple(reasons or ["POLICY_ALLOWED"]),
            policy_version=self.policy_version,
        )


class OpaToolPolicy:
    """OPA REST decision point with local mandatory guards and fail-closed semantics."""

    def __init__(
        self,
        base_url: str,
        *,
        decision_path: str = "/v1/data/servicemind/tool/decision",
        policy_version: str,
        timeout_seconds: float = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("OPA URL must be an absolute HTTP(S) URL")
        if not decision_path.startswith("/v1/data/"):
            raise ValueError("OPA decision path must use the Data API")
        self.base_url = base_url.rstrip("/")
        self.decision_path = decision_path
        self.policy_version = policy_version
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.mandatory = DeterministicToolPolicy(policy_version=policy_version)

    async def decide(self, definition: ToolDefinition, call: ToolCall) -> ToolPolicyDecision:
        mandatory = await self.mandatory.decide(definition, call)
        if not mandatory.allow:
            return mandatory
        payload = {
            "input": {
                "identity": {"user_id": call.user_id, "roles": sorted(call.roles)},
                "tenant_id": str(call.tenant_id),
                "entity_ids": sorted(call.entity_ids),
                "tool": definition.model_dump(mode="json"),
                "arguments_hash": call.argument_hash,
                "risk": definition.risk_level.value,
                "approval_ref": call.approval_ref,
                "taint_labels": sorted(call.taint_labels),
            }
        }
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                transport=self.transport,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = await client.post(self.decision_path, json=payload)
                response.raise_for_status()
                document = response.json()
            result = document["result"]
            if not isinstance(result, dict) or not isinstance(result.get("allow"), bool):
                raise ValueError("OPA returned an invalid decision")
            return ToolPolicyDecision(
                allow=result["allow"],
                requires_approval=bool(
                    result.get("requires_approval", definition.requires_approval)
                ),
                allowed_fields=frozenset(result.get("allowed_fields", mandatory.allowed_fields)),
                redactions=tuple(result.get("redactions", ())),
                reason_codes=tuple(result.get("reason_codes") or ("OPA_ALLOWED",)),
                policy_version=str(result.get("policy_version") or self.policy_version),
                external_decision_id=response.headers.get("X-OPA-Decision-ID")
                or document.get("decision_id"),
            )
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return ToolPolicyDecision(
                allow=False,
                requires_approval=definition.requires_approval,
                reason_codes=("POLICY_ENGINE_UNAVAILABLE_OR_INVALID",),
                policy_version=self.policy_version,
            )
