from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from servicemind.domain.knowledge import RetrievalPrincipal
from servicemind.graphrag.domain import (
    EdgeKind,
    GraphBatch,
    GraphEdge,
    GraphNode,
    GraphSubgraph,
    NodeKind,
)


class GraphAccessError(RuntimeError):
    """A store was asked to read a graph it cannot filter to the caller's authority.

    The graph side channel used to degrade to "no findings" on any failure, which is the
    right posture for an advisory channel -- except for this one failure. A store that
    returns nodes without applying the principal's ACL does not return *fewer* findings,
    it returns *other people's* findings, and the caller has no way to tell that apart
    from a query that genuinely matched nothing. So this is raised rather than degraded:
    the alternative is a side channel that is silently wider than the text channel it
    stands beside.
    """

    def __init__(self, store: str) -> None:
        super().__init__(f"graph store {store!r} cannot apply node ACLs")
        self.store = store


def node_is_visible(node: GraphNode, principal: RetrievalPrincipal) -> bool:
    """Shared by every store, so no implementation gets to have its own reading.

    Tenant first: a store that filtered only by ACL would serve another tenant's nodes
    to a principal that happened to hold a matching group id.
    """
    return node.tenant_id == principal.tenant_id and principal.allows_scope(
        entity_ids=node.entity_ids,
        group_ids=node.group_ids,
        profile_ids=node.profile_ids,
    )


def visible_nodes(nodes: list[GraphNode], principal: RetrievalPrincipal) -> list[GraphNode]:
    return [node for node in nodes if node_is_visible(node, principal)]


class GraphStore(Protocol):
    """Tenant- and ACL-scoped structural store behind the Graph-RAG side channel.

    Every read is filtered by ``tenant_id`` *and* by the caller's ACL coordinates;
    projection nodes and edges carry the tenant as a first-class key component so an
    operator can never reach into another tenant's topology through the graph API.

    Reads take a ``RetrievalPrincipal`` rather than a bare ``tenant_id``. Passing the
    tenant alone was how the finer scope went missing: the caller had a principal, the
    port asked for one field of it, and no implementation could have filtered by the
    other three even if it had wanted to. A principal cannot disagree with itself the way
    a tenant id passed alongside a scope can.
    """

    label: str
    #: Declared on the port so the side channel can refuse a store that would read
    #: without filtering, instead of discovering it from the results.
    supports_node_acl: bool

    async def initialize(self) -> None: ...

    async def close(self) -> None: ...

    async def delete_tenant(self, tenant_id: UUID) -> int: ...

    async def apply_batch(self, batch: GraphBatch) -> int:
        """Idempotently upsert nodes + edges; returns the number of edges written."""
        ...

    async def match_nodes(
        self,
        principal: RetrievalPrincipal,
        *,
        identifiers: Sequence[str] = (),
        entities: Sequence[str] = (),
    ) -> list[GraphNode]:
        """Ranked candidate nodes a retrieval query can anchor on, within the principal."""
        ...

    async def subgraph(
        self,
        principal: RetrievalPrincipal,
        seed_keys: list[str],
        *,
        max_hops: int = 2,
        max_nodes: int = 200,
        max_edges: int = 2_000,
    ) -> GraphSubgraph:
        """What is reachable from ``seed_keys`` within ``max_hops``, bounded on both sides.

        ``max_nodes`` bounded the node set and nothing bounded the edges, in either
        implementation. An ITSM graph is not sparse at the middle: one CI that a thousand
        incidents have affected is a legal, ordinary topology, and every one of those edges
        is incident to a node that is in the set. The bound has to be declared here, on the
        port, because both implementations have to honour the same one -- a bound that
        only one side enforces is the bound that does not exist.

        Reachability is itself filtered: a node the principal may not read is not a
        neighbour, and an edge into one is not a path. Filtering only the seeds would
        have left the traversal as the way around the filter -- one hop from a node you
        are allowed to see is exactly where the interesting unauthorized content is.
        """
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
    supports_node_acl = True

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
        principal: RetrievalPrincipal,
        *,
        identifiers: Sequence[str] = (),
        entities: Sequence[str] = (),
    ) -> list[GraphNode]:
        return rank_matches(
            visible_nodes(list(self._nodes[principal.tenant_id].values()), principal),
            identifiers=list(identifiers),
            entities=list(entities),
        )

    async def subgraph(
        self,
        principal: RetrievalPrincipal,
        seed_keys: list[str],
        *,
        max_hops: int = 2,
        max_nodes: int = 200,
        max_edges: int = 2_000,
    ) -> GraphSubgraph:
        tenant_id = principal.tenant_id
        # Filtered once, before the traversal: every later step reads this dict, so a
        # node the principal may not see is not in ``nodes`` and therefore cannot be a
        # neighbour, cannot extend a frontier and cannot be a seed.
        nodes = {
            key: node
            for key, node in self._nodes[tenant_id].items()
            if node_is_visible(node, principal)
        }
        edges = list(self._edges[tenant_id].values())
        limit = max(max_nodes, len(seed_keys))
        edge_limit = max(max_edges, len(seed_keys))
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
                        if len(collected) >= edge_limit:
                            continue
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
