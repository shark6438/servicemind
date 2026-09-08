from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any, TypedDict
from uuid import UUID

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field, model_validator

from core import get_model, settings
from servicemind.context.builder import redact_for_model
from servicemind.context.contracts import ContextEnvelope
from servicemind.domain.evidence import (
    EVIDENCE_CONTENT_MAX,
    Evidence,
    EvidenceSourceType,
)
from servicemind.integrations.glpi.client import GlpiAPIError, GlpiClient
from servicemind.integrations.glpi.resolver import resolve_glpi_config
from servicemind.runtime.contracts import (
    AgentInvocationContext,
    AgentResultEnvelope,
    AgentRunMetrics,
    AgentRunStatus,
    ToolInvocationRecord,
    stable_digest,
)
from servicemind.runtime.structured import structured_output
from servicemind.runtime.tool_gateway import (
    DataToolName,
    TenantGlpiReadGateway,
    tenant_glpi_read_gateway,
)
from servicemind.security.auth import TenantContext

#: Evidence rows a single Data Agent task may emit per GLPI resource. These are
#: deliberate ceilings, not heuristics: ``JoinedEvidence.items`` caps at 100 and the
#: join is shared with knowledge evidence, so one task must never be able to exhaust
#: that budget on its own. The GLPI client already caps groups at 50, so the open
#: variable (upstream) is the unbounded followup timeline -- bound it to the most
#: recent rows, which is also what a chatty-ticket analysis actually needs.
_MAX_SUPPORT_GROUP_ROWS = 50
_MAX_FOLLOWUP_ROWS = 15

_EVIDENCE_TRUNCATION_SUFFIX = "\n…[evidence content truncated at content ceiling]"


def _bounded_json_content(payload: Any) -> tuple[str, bool]:
    """Serialize ``payload`` to an ``Evidence.content``-safe string.

    Mirrors the RAG parent bound (``rag.service._bounded_evidence_content``):
    ``Evidence.content`` caps at ``EVIDENCE_CONTENT_MAX`` and the provider is
    responsible for bounding at the evidence boundary, never crashing. A GLPI ticket
    description or raw-HTML followup can legitimately exceed that; we truncate with an
    explicit marker and keep the full facts in ``metadata`` for the deterministic
    fallback path. Returns ``(content_text, truncated)``.
    """
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if len(text) <= EVIDENCE_CONTENT_MAX:
        return text, False
    head = text[: EVIDENCE_CONTENT_MAX - len(_EVIDENCE_TRUNCATION_SUFFIX)]
    return head + _EVIDENCE_TRUNCATION_SUFFIX, True


def _evidence_metadata(*, truncated: bool, **extra: Any) -> dict[str, Any]:
    """Per-row evidence metadata; records whether the content bound fired."""
    metadata: dict[str, Any] = {"untrusted_external_content": True, **extra}
    if truncated:
        metadata["content_truncated"] = True
    return metadata


class DataToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: DataToolName
    ticket_id: int = Field(ge=1)
    purpose: str = Field(min_length=1, max_length=300)


class DataAcquisitionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    calls: list[DataToolCall] = Field(default_factory=list, max_length=6)
    rationale_summary: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def reject_duplicates(self) -> DataAcquisitionPlan:
        names = [call.tool_name for call in self.calls]
        if len(names) != len(set(names)):
            raise ValueError("Data acquisition plan contains duplicate tool calls")
        return self


class DataAgentState(TypedDict, total=False):
    invocation: AgentInvocationContext
    context_envelope: ContextEnvelope | None
    tenant_context: TenantContext
    objective: str
    ticket_id: int
    plan: DataAcquisitionPlan
    raw_results: dict[str, Any]
    evidence: list[Evidence]
    model_calls: int
    tool_calls: int
    attempts: int
    tool_records: list[ToolInvocationRecord]
    degraded: bool
    failure_code: str | None


class DataAgent:
    """Bounded GLPI sub-agent with model-proposed, policy-compiled read tools."""

    def __init__(
        self,
        *,
        gateway: TenantGlpiReadGateway = tenant_glpi_read_gateway,
        model_factory: Callable[[], BaseChatModel] | None = None,
    ) -> None:
        self.gateway = gateway
        self.model_factory = model_factory or (lambda: get_model(settings.DEFAULT_MODEL))
        self.graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(DataAgentState)
        graph.add_node("plan", self._plan_node)
        graph.add_node("execute", self._execute_node)
        graph.add_node("normalize", self._normalize_node)
        graph.add_edge(START, "plan")
        graph.add_edge("plan", "execute")
        graph.add_edge("execute", "normalize")
        graph.add_edge("normalize", END)
        return graph.compile()

    def _minimum_plan(self, ticket_id: int, objective: str) -> DataAcquisitionPlan:
        calls = [
            DataToolCall(
                tool_name=DataToolName.GET_TICKET,
                ticket_id=ticket_id,
                purpose="Retrieve the tenant-scoped ticket facts.",
            ),
            DataToolCall(
                tool_name=DataToolName.LIST_GROUPS,
                ticket_id=ticket_id,
                purpose="Validate that any recommended assignment group exists.",
            ),
        ]
        lowered = objective.casefold()
        if any(term in lowered for term in ("followup", "timeline", "history", "跟进", "历史")):
            calls.append(
                DataToolCall(
                    tool_name=DataToolName.LIST_FOLLOWUPS,
                    ticket_id=ticket_id,
                    purpose="Retrieve ticket followup history requested by the task.",
                )
            )
        return DataAcquisitionPlan(
            calls=calls,
            rationale_summary="Minimum evidence for ticket analysis and assignment validation.",
        )

    def _budgeted_minimum(
        self,
        invocation: AgentInvocationContext,
        ticket_id: int,
        objective: str,
    ) -> DataAcquisitionPlan:
        """Largest read-only prefix of the minimum plan that the budget admits.

        The degrade path must never raise or exceed authority: optional followups and
        then ``LIST_GROUPS`` are dropped as the budget tightens. A zero budget or an
        allowlist without ticket access yields an empty degraded result; it never
        invents permission for a convenience read.
        """
        ceiling = invocation.max_tool_calls
        allowed = invocation.allowed_capabilities
        kept: list[DataToolCall] = []
        for call in self._minimum_plan(ticket_id, objective).calls:
            if len(kept) >= ceiling:
                break
            if call.tool_name.value in allowed:
                kept.append(call)
        return DataAcquisitionPlan(
            calls=kept,
            rationale_summary="Budget-bounded minimum evidence for ticket analysis.",
        )

    def _compile_plan(
        self,
        proposal: DataAcquisitionPlan,
        *,
        invocation: AgentInvocationContext,
        ticket_id: int,
    ) -> DataAcquisitionPlan:
        if len(proposal.calls) > invocation.max_tool_calls:
            raise ValueError("Data Agent proposal exceeds the invocation tool budget")
        for call in proposal.calls:
            if call.ticket_id != ticket_id:
                raise PermissionError("Data Agent cannot change the Supervisor-selected ticket")
            invocation.require_capability(call.tool_name.value)
        proposed = {call.tool_name for call in proposal.calls}
        required = {DataToolName.GET_TICKET, DataToolName.LIST_GROUPS}
        if not required <= proposed:
            raise ValueError("Data Agent plan must retrieve ticket facts and support groups")
        return proposal

    async def _plan_node(self, state: DataAgentState) -> dict[str, Any]:
        invocation = state["invocation"]
        invocation.ensure_active()
        ticket_id = state["ticket_id"]
        objective = state["objective"]
        if invocation.max_model_calls == 0:
            # No model budget: run the deterministic minimum and say so. Reporting
            # this as SUCCEEDED would mask that no model planning occurred (the Phase 3
            # contract requires an explicit DEGRADED for budget exhaustion).
            return {
                "plan": self._budgeted_minimum(invocation, ticket_id, objective),
                "model_calls": 0,
                "degraded": True,
                "failure_code": "DATA_MODEL_BUDGET_EXHAUSTED",
            }
        fallback = self._budgeted_minimum(invocation, ticket_id, objective)
        try:
            runnable = structured_output(self.model_factory(), DataAcquisitionPlan)
            envelope = state.get("context_envelope")
            model_input: dict[str, Any] = {
                "ticket_id": state["ticket_id"],
                "allowed_tools": sorted(invocation.allowed_capabilities),
                "max_tool_calls": invocation.max_tool_calls,
            }
            if envelope is not None:
                model_input["governed_context"] = envelope.model_payload()
            else:
                model_input["objective"] = state["objective"]
            proposal = await runnable.ainvoke(
                [
                    SystemMessage(
                        content=(
                            "You are a read-only enterprise ITSM Data Agent. Propose the minimum "
                            "GLPI evidence calls needed for the task. Never write, change the "
                            "ticket ID, invent tools, or handle credentials. Always include "
                            "glpi.read.ticket and glpi.read.groups. Add ticket followups only when "
                            "history is required. Return JSON matching this schema: "
                            f"{json.dumps(DataAcquisitionPlan.model_json_schema())}"
                        )
                    ),
                    HumanMessage(
                        content=redact_for_model(
                            json.dumps(model_input, ensure_ascii=False)
                        ).text
                    ),
                ]
            )
            plan = self._compile_plan(
                DataAcquisitionPlan.model_validate(proposal),
                invocation=invocation,
                ticket_id=state["ticket_id"],
            )
            return {"plan": plan, "model_calls": 1, "degraded": False}
        except Exception:
            # Failure cannot broaden authority: the fallback is a fixed, budget-respecting
            # read-only plan. It is trusted (never re-compiled, so a tight tool budget or
            # a history-flagged objective cannot make the degrade path itself raise).
            return {
                "plan": fallback,
                "model_calls": 1,
                "degraded": True,
                "failure_code": "DATA_PLAN_DEGRADED",
            }

    async def _execute_call(
        self, state: DataAgentState, call: DataToolCall
    ) -> tuple[str, Any, int, ToolInvocationRecord]:
        attempts = 0
        for attempt in range(2):
            attempts += 1
            try:
                result = await self.gateway.execute(
                    invocation=state["invocation"],
                    tenant_context=state["tenant_context"],
                    tool_name=call.tool_name,
                    ticket_id=call.ticket_id,
                )
                return (
                    call.tool_name.value,
                    result,
                    attempts,
                    ToolInvocationRecord(
                        tool_name=call.tool_name.value,
                        input_hash=stable_digest({"ticket_id": call.ticket_id}),
                        status="succeeded",
                        attempts=attempts,
                        result_ref=f"sha256:{stable_digest(result)}",
                    ),
                )
            except GlpiAPIError as exc:
                if attempt or exc.status_code not in {429, 500, 502, 503, 504}:
                    raise
                await asyncio.sleep(0.25)
            except TimeoutError:
                if attempt:
                    raise
                await asyncio.sleep(0.25)
        raise AssertionError("unreachable")

    async def _execute_node(self, state: DataAgentState) -> dict[str, Any]:
        results = await asyncio.gather(
            *(self._execute_call(state, call) for call in state["plan"].calls)
        )
        return {
            "raw_results": {name: value for name, value, _, _ in results},
            "tool_calls": len(results),
            "attempts": max(sum(attempts for _, _, attempts, _ in results), 1),
            "tool_records": [record for _, _, _, record in results],
        }

    async def _normalize_node(self, state: DataAgentState) -> dict[str, Any]:
        tenant_id = state["invocation"].tenant_id
        ticket_id = state["ticket_id"]
        raw = state["raw_results"]
        ticket = raw.get(DataToolName.GET_TICKET.value)
        if ticket is None:
            return {"evidence": []}
        ticket_text, ticket_truncated = _bounded_json_content(ticket)
        evidence = [
            Evidence.create(
                tenant_id=tenant_id,
                source_type=EvidenceSourceType.GLPI,
                source_ref=f"glpi://tickets/{ticket_id}",
                resource_type="ticket",
                resource_id=str(ticket_id),
                content=ticket_text,
                provider="glpi-high-level-api-v2.3",
                retrieval_method=DataToolName.GET_TICKET.value,
                confidence=1,
                metadata=_evidence_metadata(truncated=ticket_truncated, ticket_facts=ticket),
            )
        ]
        # Support groups are already name-sorted by the client; keep its ceiling.
        # Optional tools may be absent from a budget-bounded plan, so never index raw
        # unconditionally (a KeyError here would crash the run, not degrade it).
        for group in raw.get(DataToolName.LIST_GROUPS.value, [])[:_MAX_SUPPORT_GROUP_ROWS]:
            evidence.append(
                Evidence.create(
                    tenant_id=tenant_id,
                    source_type=EvidenceSourceType.GLPI,
                    source_ref=f"glpi://groups/{group['id']}",
                    resource_type="support_group",
                    resource_id=str(group["id"]),
                    content=f"GLPI support group: {group['name']}",
                    provider="glpi-high-level-api-v2.3",
                    retrieval_method=DataToolName.LIST_GROUPS.value,
                    confidence=1,
                    metadata=_evidence_metadata(truncated=False, group=group),
                )
            )
        if DataToolName.LIST_FOLLOWUPS.value in raw:
            # The upstream followup timeline is unbounded; keep the most recent rows so
            # one chatty ticket cannot exhaust the joined-evidence budget downstream.
            followups = sorted(raw[DataToolName.LIST_FOLLOWUPS.value], key=lambda item: item["id"])[
                -_MAX_FOLLOWUP_ROWS:
            ]
            for item in followups:
                text, truncated = _bounded_json_content(item)
                evidence.append(
                    Evidence.create(
                        tenant_id=tenant_id,
                        source_type=EvidenceSourceType.GLPI,
                        source_ref=f"glpi://tickets/{ticket_id}/followups/{item['id']}",
                        resource_type="ticket_followup",
                        resource_id=str(item["id"]),
                        content=text,
                        provider="glpi-high-level-api-v2.3",
                        retrieval_method=DataToolName.LIST_FOLLOWUPS.value,
                        confidence=1,
                        metadata=_evidence_metadata(truncated=truncated),
                    )
                )
        return {"evidence": evidence}

    async def run(
        self,
        *,
        invocation: AgentInvocationContext,
        tenant_context: TenantContext,
        objective: str,
        ticket_id: int,
        context_envelope: ContextEnvelope | None = None,
    ) -> AgentResultEnvelope[list[Evidence]]:
        started = time.perf_counter()
        state = await self.graph.ainvoke(
            {
                "invocation": invocation,
                "context_envelope": context_envelope,
                "tenant_context": tenant_context,
                "objective": objective,
                "ticket_id": ticket_id,
                "model_calls": 0,
                "tool_calls": 0,
                "attempts": 1,
                "degraded": False,
                "tool_records": [],
            }
        )
        evidence = state["evidence"]
        return AgentResultEnvelope[list[Evidence]](
            agent_name="data",
            task_id=invocation.task_id,
            status=(AgentRunStatus.DEGRADED if state.get("degraded") else AgentRunStatus.SUCCEEDED),
            output=evidence,
            evidence_refs=[item.evidence_id for item in evidence],
            metrics=AgentRunMetrics(
                model_calls=state.get("model_calls", 0),
                tool_calls=state.get("tool_calls", 0),
                attempts=state.get("attempts", 1),
                latency_ms=(time.perf_counter() - started) * 1000,
            ),
            model_name=str(settings.DEFAULT_MODEL),
            prompt_version=invocation.prompt_version,
            policy_version=invocation.policy_version,
            failure_code=state.get("failure_code"),
            tool_invocations=state.get("tool_records", []),
        )

    async def get_ticket_facts(self, context: TenantContext, ticket_id: int) -> dict[str, object]:
        config = await resolve_glpi_config(context)
        async with GlpiClient(config) as client:
            return (await client.get_ticket(ticket_id)).to_agent_payload()

    async def get_ticket_evidence(self, context: TenantContext, ticket_id: int) -> list[Evidence]:
        """Deterministic compatibility and fast path; complex tasks use ``run``."""
        config = await resolve_glpi_config(context)
        async with GlpiClient(config) as client:
            ticket, groups = await asyncio.gather(
                client.get_ticket(ticket_id), client.list_groups()
            )
        facts = ticket.to_agent_payload()
        facts_text, facts_truncated = _bounded_json_content(facts)
        result = [
            Evidence.create(
                tenant_id=UUID(str(context.tenant_id)),
                source_type=EvidenceSourceType.GLPI,
                source_ref=f"glpi://tickets/{ticket_id}",
                resource_type="ticket",
                resource_id=str(ticket_id),
                content=facts_text,
                provider="glpi-high-level-api-v2.3",
                retrieval_method=DataToolName.GET_TICKET.value,
                confidence=1,
                metadata=_evidence_metadata(truncated=facts_truncated, ticket_facts=facts),
            )
        ]
        result.extend(
            Evidence.create(
                tenant_id=UUID(str(context.tenant_id)),
                source_type=EvidenceSourceType.GLPI,
                source_ref=f"glpi://groups/{group.id}",
                resource_type="support_group",
                resource_id=str(group.id),
                content=f"GLPI support group: {group.name}",
                provider="glpi-high-level-api-v2.3",
                retrieval_method=DataToolName.LIST_GROUPS.value,
                confidence=1,
                metadata=_evidence_metadata(truncated=False, group=group.to_agent_payload()),
            )
            for group in groups[:_MAX_SUPPORT_GROUP_ROWS]
        )
        return result


data_agent = DataAgent()
