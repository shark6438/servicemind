from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph

from core import settings
from servicemind.domain.evidence import Evidence, EvidenceSourceType
from servicemind.domain.knowledge import RetrievalPrincipal
from servicemind.rag.service import EnterpriseRAG, build_enterprise_rag


@dataclass(frozen=True)
class Runbook:
    identifier: str
    title: str
    tags: tuple[str, ...]
    content: str


RUNBOOKS = (
    Runbook(
        "rb-vpn-mfa",
        "VPN MFA incident triage",
        ("vpn", "mfa", "authentication", "network"),
        "For VPN or MFA failures, verify identity-provider health, token clock skew, gateway reachability and recent authentication changes. Network Team owns gateway/connectivity faults; Identity Team owns token enrollment faults. Record findings as an internal work note before changing production.",
    ),
    Runbook(
        "rb-priority-matrix",
        "Incident impact and urgency matrix",
        ("priority", "impact", "urgency", "incident"),
        "Priority must be derived from impact and urgency. High urgency with medium impact normally maps to high priority. A major priority requires evidence of broad service impact and must be escalated for human review.",
    ),
    Runbook(
        "rb-general-triage",
        "General incident triage",
        ("incident", "ticket", "service", "support", "triage"),
        "Confirm ticket status, affected service, impact, urgency and current team. Service Desk owns unclassified incidents. Preserve evidence, avoid destructive actions, and escalate when the available facts cannot support a recommendation.",
    ),
)


class KnowledgeState(TypedDict, total=False):
    tenant_id: UUID
    user_id: str
    query: str
    entity_ids: set[int]
    group_ids: set[int]
    profile_ids: set[int]
    evidence: list[Evidence]
    retrieval_round: int
    use_query_model: bool
    model_query: str | None


class KnowledgeAgent:
    """Enterprise RAG subgraph with no business mutation authority."""

    def __init__(self, rag: EnterpriseRAG | None = None) -> None:
        self._rag = rag
        graph = StateGraph(KnowledgeState)
        graph.add_node("retrieve", self._retrieve_node)
        graph.add_edge(START, "retrieve")
        graph.add_edge("retrieve", END)
        self.graph = graph.compile()

    @property
    def rag(self) -> EnterpriseRAG:
        if self._rag is None:
            self._rag = build_enterprise_rag()
        return self._rag

    async def _retrieve_node(self, state: KnowledgeState) -> dict[str, Any]:
        if settings.SERVICEMIND_RAG_ENABLED:
            principal = RetrievalPrincipal(
                tenant_id=state["tenant_id"],
                user_id=state["user_id"],
                entity_ids=frozenset(state.get("entity_ids", set())),
                group_ids=frozenset(state.get("group_ids", set())),
                profile_ids=frozenset(state.get("profile_ids", set())),
            )
            result = await self.rag.retrieve(
                principal=principal,
                query=state["query"],
                model_query=state.get("model_query"),
                use_query_model=state.get("use_query_model", True),
                final_k=10 if state.get("retrieval_round", 0) else 8,
                # Fan the processed query's LLM rewrites out to their own BM25 arms
                # on top of the single dense anchor (cluster-side RRF merges them;
                # OpenSearch caps hybrid at 5 arms). Safe even when the rewrite stage
                # is disabled -- _fan_out_texts then degrades to the two-arm request.
                use_rewrites=settings.SERVICEMIND_RAG_MULTI_QUERY,
            )
            evidence = self.rag.to_evidence(state["tenant_id"], result)
            if self.rag.graph_store is not None:
                # Graph findings are a structural side channel appended after text
                # evidence; text hybrid retrieval stays the primary knowledge source.
                evidence = [
                    *evidence,
                    *await self.rag.graph_evidence(principal, result),
                ]
            return {"evidence": evidence}
        if settings.SERVICEMIND_RAG_REQUIRED:
            raise RuntimeError("Enterprise RAG is required but disabled")
        return {
            "evidence": self._baseline(
                state["tenant_id"], state["query"], state.get("retrieval_round", 0)
            )
        }

    async def retrieve(
        self,
        *,
        tenant_id: UUID,
        query: str,
        retrieval_round: int = 0,
        user_id: str = "system",
        entity_ids: set[int] | None = None,
        group_ids: set[int] | None = None,
        profile_ids: set[int] | None = None,
        use_query_model: bool = True,
        model_query: str | None = None,
    ) -> list[Evidence]:
        result = await self.graph.ainvoke(
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "query": query,
                "retrieval_round": retrieval_round,
                "entity_ids": entity_ids or set(),
                "group_ids": group_ids or set(),
                "profile_ids": profile_ids or set(),
                "use_query_model": use_query_model,
                "model_query": model_query,
            }
        )
        return result["evidence"]

    def _baseline(self, tenant_id: UUID, query: str, retrieval_round: int) -> list[Evidence]:
        tokens = set(re.findall(r"[a-z0-9]+", query.casefold()))
        ranked = sorted(
            ((sum(tag in tokens for tag in item.tags), item) for item in RUNBOOKS),
            key=lambda pair: (-pair[0], pair[1].identifier),
        )
        selected = [item for score, item in ranked if score > 0][: 3 if retrieval_round else 2]
        if not selected and retrieval_round:
            selected = [item for item in RUNBOOKS if item.identifier == "rb-general-triage"]
        return [
            Evidence.create(
                tenant_id=tenant_id,
                source_type=EvidenceSourceType.KNOWLEDGE,
                source_ref=f"runbook://{item.identifier}",
                resource_type="runbook",
                resource_id=item.identifier,
                content=f"{item.title}: {item.content}",
                provider="phase3-baseline-runbooks",
                retrieval_method="deterministic_tag_match",
                confidence=1,
                metadata={"title": item.title, "tags": list(item.tags), "degraded_rag": True},
            )
            for item in selected
        ]


knowledge_agent = KnowledgeAgent()
