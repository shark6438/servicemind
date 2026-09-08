from __future__ import annotations

from enum import StrEnum
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class NodeKind(StrEnum):
    """Entity types in the ITSM projection graph."""

    TICKET = "ticket"
    CI = "ci"
    SERVICE = "service"
    PROBLEM = "problem"
    CHANGE = "change"
    RUNBOOK = "runbook"


class EdgeKind(StrEnum):
    """Typed relationships between projection nodes (Phase 4 baseline 4.2).

    Directions mirror the spec: a Ticket AFFECTS a CI, a CI DEPENDS_ON a Service,
    Tickets are SIMILAR_TO one another, a Ticket is LINKED_TO a Problem, a Problem is
    RESOLVED_BY a Change, a Change MODIFIES a CI, and a CI HAS_RUNBOOK the runbook/SOP
    that governs its triage and recovery.
    """

    AFFECTS = "affects"
    DEPENDS_ON = "depends_on"
    SIMILAR_TO = "similar_to"
    LINKED_TO = "linked_to"
    RESOLVED_BY = "resolved_by"
    MODIFIES = "modifies"
    HAS_RUNBOOK = "has_runbook"


class GraphNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Stable, tenant-local identity used as the graph key (e.g. ``ticket:glpi//1234``).
    key: str = Field(min_length=1, max_length=500)
    #: External reference/URI that can be turned into a citation (``glpi://ticket/1234``).
    ref: str = Field(min_length=1, max_length=1000)
    kind: NodeKind
    title: str = Field(min_length=1, max_length=1000)
    tenant_id: UUID
    #: Searchable keyword surface (summary, status, product...) used to match queries.
    extra: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_key: str
    target_key: str
    kind: EdgeKind
    tenant_id: UUID
    #: Contextual weight/strength when known (e.g. textual similarity for SIMILAR_TO).
    weight: float = Field(default=1.0, ge=0.0, le=1.0)
    extra: dict[str, Any] = Field(default_factory=dict)


class GraphBatch(BaseModel):
    """A tenant-scoped write batch. Edges must reference nodes inside the batch."""

    model_config = ConfigDict(extra="forbid")

    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)

    @model_validator(mode="after")
    def edges_reference_batch_nodes(self) -> Self:
        tenants = {node.tenant_id for node in self.nodes} | {edge.tenant_id for edge in self.edges}
        if len(tenants) > 1:
            raise ValueError("a projection batch cannot mix tenants")
        present = {node.key for node in self.nodes}
        for edge in self.edges:
            if edge.source_key not in present or edge.target_key not in present:
                raise ValueError(
                    f"graph edge {edge.source_key}->{edge.target_key} references a node "
                    "outside the batch"
                )
        return self


class GraphSubgraph(BaseModel):
    """Everything reachable from a set of seed nodes within ``max_hops``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: UUID
    seeds: list[GraphNode] = Field(default_factory=list)
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)

    def neighbors(self, key: str, kind: EdgeKind | None = None) -> list[GraphNode]:
        out = []
        for edge in self.edges:
            target = None
            if edge.source_key == key:
                target = edge.target_key
            elif edge.kind is EdgeKind.SIMILAR_TO and edge.target_key == key:
                target = edge.source_key
            if target is not None and (kind is None or edge.kind is kind):
                for node in self.nodes:
                    if node.key == target:
                        out.append(node)
                        break
        return out

    def find(self, key: str) -> GraphNode | None:
        for node in self.nodes:
            if node.key == key:
                return node
        return None

    def key_by_kind(self, kind: NodeKind) -> list[GraphNode]:
        return [node for node in self.nodes if node.kind is kind]
