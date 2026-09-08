from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from servicemind.graphrag.domain import (
    EdgeKind,
    GraphBatch,
    GraphEdge,
    GraphNode,
    GraphSubgraph,
    NodeKind,
)


class GraphStore(Protocol):
    """Tenant-scoped structural store behind the Graph-RAG side channel.

    Every read and write is filtered by ``tenant_id``; projection nodes and edges
    carry the tenant as a first-class key component so an operator can never reach
    into another tenant's topology through the graph API.
    """

    label: str

    async def initialize(self) -> None: ...

    async def close(self) -> None: ...

    async def delete_tenant(self, tenant_id: UUID) -> int: ...

    async def apply_batch(self, batch: GraphBatch) -> int:
        """Idempotently upsert nodes + edges; returns the number of edges written."""
        ...

    async def match_nodes(
        self,
        tenant_id: UUID,
        *,
        identifiers: Sequence[str] = (),
        entities: Sequence[str] = (),
    ) -> list[GraphNode]:
        """Ranked candidate nodes a retrieval query can anchor on."""
        ...

    async def subgraph(
        self,
        tenant_id: UUID,
        seed_keys: list[str],
        *,
        max_hops: int = 2,
        max_nodes: int = 200,
    ) -> GraphSubgraph:
        """Everything reachable from ``seed_keys`` within ``max_hops``."""
        ...


def _strong_ref(node: GraphNode, identifier: str) -> bool:
    ref = node.ref.casefold()
    needle = identifier.casefold()
    return ref == needle or ref.endswith(f"/{needle}") or ref.endswith(f":{needle}")


def _weak_ref(node: GraphNode, identifier: str) -> bool:
    ref = node.ref.casefold()
    return identifier.casefold() in ref


def rank_matches(
    nodes: list[GraphNode],
    *,
    identifiers: Sequence[str],
    entities: Sequence[str],
) -> list[GraphNode]:
    """Deterministic, tenant-agnostic candidate ranking shared by every store.

    A node scores highest when a ticket identifier names it directly, then when an
    identifier appears anywhere in its reference, then when an entity term appears in
    its title. Event records (ticket/problem/change) sort before configuration items
    so incident history anchors a query before generic topology.
    """
    kind_order = {
        NodeKind.TICKET: 0,
        NodeKind.PROBLEM: 1,
        NodeKind.CHANGE: 2,
        NodeKind.CI: 3,
        NodeKind.SERVICE: 4,
        NodeKind.RUNBOOK: 5,
    }
    scored: list[tuple[int, int, str, GraphNode]] = []
    for node in nodes:
        strong = any(_strong_ref(node, identifier) for identifier in identifiers)
        weak = any(_weak_ref(node, identifier) for identifier in identifiers)
        title_hit = any(term.casefold() in node.title.casefold() for term in entities if term)
        if not (strong or weak or title_hit):
            continue
        tier = 0 if strong else 1 if weak else 2
        scored.append((tier, kind_order[node.kind], node.key, node))
    scored.sort()
    return [node for _, _, _, node in scored]


class MemoryGraphStore:
    """Reference GraphStore implementation for offline tests and small demos.

    Semantics mirror the Neo4j store: per-tenant adjacency, idempotent MERGE-style
    upserts, and the same shared ``rank_matches`` candidate ranking.
    """

    label = "memory"

    def __init__(self) -> None:
        self._nodes: dict[UUID, dict[str, GraphNode]] = defaultdict(dict)
        self._edges: dict[UUID, dict[tuple[str, str, EdgeKind], GraphEdge]] = defaultdict(dict)

    async def initialize(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def apply_batch(self, batch: GraphBatch) -> int:
        for node in batch.nodes:
            self._nodes[node.tenant_id][node.key] = node
        written = 0
        for edge in batch.edges:
            slot = self._edges[edge.tenant_id]
            marker = (edge.source_key, edge.target_key, edge.kind)
            previous = slot.get(marker)
            slot[marker] = edge
            if previous is None:
                written += 1
        return written

    async def match_nodes(
        self,
        tenant_id: UUID,
        *,
        identifiers: Sequence[str] = (),
        entities: Sequence[str] = (),
    ) -> list[GraphNode]:
        return rank_matches(
            list(self._nodes[tenant_id].values()),
            identifiers=list(identifiers),
            entities=list(entities),
        )

    async def subgraph(
        self,
        tenant_id: UUID,
        seed_keys: list[str],
        *,
        max_hops: int = 2,
        max_nodes: int = 200,
    ) -> GraphSubgraph:
        nodes = self._nodes[tenant_id]
        edges = list(self._edges[tenant_id].values())
        limit = max(max_nodes, len(seed_keys))
        seen: set[str] = {key for key in seed_keys if key in nodes}
        collected: list[GraphEdge] = []
        collected_markers: set[tuple[str, str, EdgeKind]] = set()
        frontier = set(seen)
        for _ in range(max(max_hops, 0)):
            if not frontier:
                break
            next_frontier: set[str] = set()
            for key in frontier:
                for edge in edges:
                    if edge.source_key == key:
                        neighbor = edge.target_key
                    elif edge.target_key == key:
                        neighbor = edge.source_key
                    else:
                        continue
                    if neighbor not in nodes:
                        continue
                    if neighbor not in seen and len(seen) >= limit:
                        continue
                    marker = (edge.source_key, edge.target_key, edge.kind)
                    if marker not in collected_markers:
                        collected_markers.add(marker)
                        collected.append(edge)
                    if neighbor not in seen:
                        seen.add(neighbor)
                        next_frontier.add(neighbor)
            if len(seen) >= limit:
                break
            frontier = next_frontier
        seed_nodes = [nodes[key] for key in seed_keys if key in nodes]
        ordered = sorted(seen, key=lambda key: nodes[key].ref)
        return GraphSubgraph(
            tenant_id=tenant_id,
            seeds=seed_nodes,
            nodes=[nodes[key] for key in ordered],
            edges=sorted(collected, key=lambda e: (e.source_key, e.target_key, e.kind)),
        )

    async def delete_tenant(self, tenant_id: UUID) -> int:
        count = len(self._nodes[tenant_id]) + len(self._edges[tenant_id])
        self._nodes.pop(tenant_id, None)
        self._edges.pop(tenant_id, None)
        return count
