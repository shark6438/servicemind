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
from servicemind.domain.evidence import Evidence, EvidenceSourceType
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


class DataToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: DataToolName
    ticket_id: int = Field(ge=1)
    purpose: str = Field(min_length=1, max_length=300)


class DataAcquisitionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    calls: list[DataToolCall] = Field(min_length=1, max_length=6)
    rationale_summary: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def reject_duplicates(self) -> DataAcquisitionPlan:
        names = [call.tool_name for call in self.calls]
        if len(names) != len(set(names)):
            raise ValueError("Data acquisition plan contains duplicate tool calls")
        return self


class DataAgentState(TypedDict, total=False):
    invocation: AgentInvocationContext
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
        fallback = self._minimum_plan(state["ticket_id"], state["objective"])
        if invocation.max_model_calls == 0:
            return {"plan": fallback, "model_calls": 0, "degraded": False}
        try:
            runnable = structured_output(self.model_factory(), DataAcquisitionPlan)
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
                        content=json.dumps(
                            {
                                "objective": state["objective"],
                                "ticket_id": state["ticket_id"],
                                "allowed_tools": sorted(invocation.allowed_capabilities),
                                "max_tool_calls": invocation.max_tool_calls,
                            },
                            ensure_ascii=False,
                        )
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
            # Failure cannot broaden authority: the fallback is a fixed read-only plan.
            plan = self._compile_plan(
                fallback, invocation=invocation, ticket_id=state["ticket_id"]
            )
            return {
                "plan": plan,
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
            "attempts": sum(attempts for _, _, attempts, _ in results),
            "tool_records": [record for _, _, _, record in results],
        }

    async def _normalize_node(self, state: DataAgentState) -> dict[str, Any]:
        tenant_id = state["invocation"].tenant_id
        ticket_id = state["ticket_id"]
        raw = state["raw_results"]
        ticket = raw[DataToolName.GET_TICKET.value]
        evidence = [
            Evidence.create(
                tenant_id=tenant_id,
                source_type=EvidenceSourceType.GLPI,
                source_ref=f"glpi://tickets/{ticket_id}",
                resource_type="ticket",
                resource_id=str(ticket_id),
                content=json.dumps(ticket, ensure_ascii=False, sort_keys=True),
                provider="glpi-high-level-api-v2.3",
                retrieval_method=DataToolName.GET_TICKET.value,
                confidence=1,
                metadata={"ticket_facts": ticket, "untrusted_external_content": True},
            )
        ]
        evidence.extend(
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
                metadata={"group": group, "untrusted_external_content": True},
            )
            for group in raw[DataToolName.LIST_GROUPS.value]
        )
        if DataToolName.LIST_FOLLOWUPS.value in raw:
            evidence.extend(
                Evidence.create(
                    tenant_id=tenant_id,
                    source_type=EvidenceSourceType.GLPI,
                    source_ref=f"glpi://tickets/{ticket_id}/followups/{item['id']}",
                    resource_type="ticket_followup",
                    resource_id=str(item["id"]),
                    content=json.dumps(item, ensure_ascii=False, sort_keys=True),
                    provider="glpi-high-level-api-v2.3",
                    retrieval_method=DataToolName.LIST_FOLLOWUPS.value,
                    confidence=1,
                    metadata={"untrusted_external_content": True},
                )
                for item in raw[DataToolName.LIST_FOLLOWUPS.value]
            )
        return {"evidence": evidence}

    async def run(
        self,
        *,
        invocation: AgentInvocationContext,
        tenant_context: TenantContext,
        objective: str,
        ticket_id: int,
    ) -> AgentResultEnvelope[list[Evidence]]:
        started = time.perf_counter()
        state = await self.graph.ainvoke(
            {
                "invocation": invocation,
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
            status=(
                AgentRunStatus.DEGRADED if state.get("degraded") else AgentRunStatus.SUCCEEDED
            ),
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

    async def get_ticket_evidence(
        self, context: TenantContext, ticket_id: int
    ) -> list[Evidence]:
        """Deterministic compatibility and fast path; complex tasks use ``run``."""
        config = await resolve_glpi_config(context)
        async with GlpiClient(config) as client:
            ticket, groups = await asyncio.gather(client.get_ticket(ticket_id), client.list_groups())
        facts = ticket.to_agent_payload()
        result = [
            Evidence.create(
                tenant_id=UUID(str(context.tenant_id)),
                source_type=EvidenceSourceType.GLPI,
                source_ref=f"glpi://tickets/{ticket_id}",
                resource_type="ticket",
                resource_id=str(ticket_id),
                content=json.dumps(facts, ensure_ascii=False, sort_keys=True),
                provider="glpi-high-level-api-v2.3",
                retrieval_method=DataToolName.GET_TICKET.value,
                confidence=1,
                metadata={"ticket_facts": facts, "untrusted_external_content": True},
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
                metadata={"group": group.to_agent_payload(), "untrusted_external_content": True},
            )
            for group in groups
        )
        return result


data_agent = DataAgent()
