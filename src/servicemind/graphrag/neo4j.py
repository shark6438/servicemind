from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, LiteralString, cast
from uuid import UUID

from neo4j import Query

from servicemind.graphrag.domain import (
    EdgeKind,
    GraphBatch,
    GraphEdge,
    GraphNode,
    GraphSubgraph,
    NodeKind,
)
from servicemind.graphrag.store import rank_matches

if TYPE_CHECKING:
    from neo4j import AsyncDriver, AsyncManagedTransaction

logger = logging.getLogger("servicemind.graphrag.neo4j")


class Neo4jGraphStore:
    """Neo4j-backed GraphStore.

    The projection lives under a single node label (``RAGNode``) with the tenant as
    a plain equality field, and one relationship label per ``EdgeKind``. Every Cypher
    statement pins ``tenant_id`` so cross-tenant rows are structurally unreachable.
    Writes are parametrized idempotent ``MERGE``; relationship labels are never taken
    from request input -- they are derived from the fixed ``EdgeKind`` enum.
    """

    label = "neo4j"
    NODE_LABEL = "RAGNode"
    _KIND_TO_REL = {kind: kind.name for kind in EdgeKind}

    def __init__(self, driver: AsyncDriver) -> None:
        self._driver = driver
        self._initialized = False
        self._initialize_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            statements = (
                "CREATE INDEX rag_node_tenant_key IF NOT EXISTS "
                "FOR (n:RAGNode) ON (n.tenant_id, n.key)",
                "CREATE INDEX rag_node_tenant_ref IF NOT EXISTS "
                "FOR (n:RAGNode) ON (n.tenant_id, n.ref)",
                "CREATE INDEX rag_node_tenant_kind IF NOT EXISTS "
                "FOR (n:RAGNode) ON (n.tenant_id, n.kind)",
            )
            async with self._driver.session() as session:
                for statement in statements:
                    await (await session.run(Query(statement))).consume()
            self._initialized = True

    async def close(self) -> None:
        await self._driver.close()

    async def apply_batch(self, batch: GraphBatch) -> int:
        await self.initialize()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for edge in batch.edges:
            rel = self._KIND_TO_REL.get(edge.kind)
            if rel is None:
                raise ValueError(f"Unsupported edge kind: {edge.kind}")
            grouped.setdefault(rel, []).append(
                {
                    "source_key": edge.source_key,
                    "target_key": edge.target_key,
                    "weight": edge.weight,
                }
            )

        async with self._driver.session() as session:
            await session.execute_write(
                _merge_nodes, str(batch.nodes[0].tenant_id) if batch.nodes else None, batch
            )
            written = 0
            for rel, pairs in grouped.items():
                summary = await session.run(
                    Query(
                        cast(
                            LiteralString,
                            "UNWIND $pairs AS p "
                            "MATCH (a:RAGNode {tenant_id: $tenant, key: p.source_key}) "
                            "MATCH (b:RAGNode {tenant_id: $tenant, key: p.target_key}) "
                            f"MERGE (a)-[r:{rel}]->(b) "
                            "ON CREATE SET r.weight = p.weight "
                            "ON MATCH SET r.weight = p.weight "
                            "RETURN count(r) AS written",
                        )
                    ),
                    tenant=str(batch.edges[0].tenant_id),
                    pairs=pairs,
                )
                record = await summary.single()
                written += int(record["written"]) if record else 0
        return written

    async def match_nodes(
        self,
        tenant_id: UUID,
        *,
        identifiers: Sequence[str] = (),
        entities: Sequence[str] = (),
    ) -> list[GraphNode]:
        await self.initialize()
        lowered_identifiers = [value.casefold() for value in identifiers if value]
        lowered_entities = [value.casefold() for value in entities if value]
        if not lowered_identifiers and not lowered_entities:
            return []
        async with self._driver.session() as session:
            result = await session.run(
                Query(
                    "MATCH (n:RAGNode {tenant_id: $tenant}) "
                    "WITH n, toLower(n.ref) AS ref, toLower(n.title) AS title "
                    "WITH n, "
                    "any(i IN $identifiers WHERE ref = i OR ref ENDS WITH '/' + i "
                    "OR ref ENDS WITH ':' + i) AS strong, "
                    "any(i IN $identifiers WHERE ref CONTAINS i) AS weak, "
                    "any(e IN $entities WHERE title CONTAINS e) AS title_hit "
                    "WHERE strong OR weak OR title_hit "
                    "RETURN n.kind AS kind, n.ref AS ref, n.title AS title, "
                    "n.key AS key, n.extra AS extra "
                    "ORDER BY strong DESC, weak DESC, n.kind, n.key LIMIT $limit"
                ),
                tenant=str(tenant_id),
                identifiers=lowered_identifiers,
                entities=lowered_entities,
                limit=500,
            )
            nodes: list[GraphNode] = []
            async for record in result:
                kind = NodeKind(record["kind"])
                raw_extra = record["extra"]
                extra = json.loads(raw_extra) if isinstance(raw_extra, str) else {}
                nodes.append(
                    GraphNode(
                        key=record["key"],
                        ref=record["ref"],
                        kind=kind,
                        title=record["title"],
                        tenant_id=tenant_id,
                        extra=dict(extra),
                    )
                )
        return rank_matches(nodes, identifiers=list(identifiers), entities=list(entities))

    async def subgraph(
        self,
        tenant_id: UUID,
        seed_keys: list[str],
        *,
        max_hops: int = 2,
        max_nodes: int = 200,
    ) -> GraphSubgraph:
        await self.initialize()
        tenant = str(tenant_id)
        nodes: dict[str, GraphNode] = {}
        seed_nodes: list[GraphNode] = []
        limit = max(max_nodes, len(seed_keys))
        seen: set[str] = set(seed_keys)
        edges: dict[tuple[str, str, EdgeKind], GraphEdge] = {}
        async with self._driver.session() as session:
            frontier = sorted(seed_keys)
            for _ in range(max(max_hops, 0)):
                if not frontier:
                    break
                next_frontier: set[str] = set()
                for chunk in _chunks(frontier, 200):
                    result = await session.run(
                        (
                            "MATCH (a:RAGNode {tenant_id: $tenant})-[r]->"
                            "(b:RAGNode {tenant_id: $tenant}) "
                            "WHERE a.key IN $keys OR b.key IN $keys "
                            "RETURN a.key AS akey, b.key AS bkey, type(r) AS rel, "
                            "r.weight AS weight, "
                            "a.kind AS akind, a.ref AS aref, a.title AS atitle, "
                            "b.kind AS bkind, b.ref AS bref, b.title AS btitle"
                        ),
                        tenant=tenant,
                        keys=chunk,
                    )
                    async for record in result:
                        a_key, b_key = record["akey"], record["bkey"]
                        new_keys = {key for key in (a_key, b_key) if key not in nodes}
                        if len(nodes) + len(new_keys) > limit:
                            continue
                        kind = EdgeKind(record["rel"].lower())
                        edges[(a_key, b_key, kind)] = GraphEdge(
                            source_key=a_key,
                            target_key=b_key,
                            kind=kind,
                            tenant_id=tenant_id,
                            weight=float(record["weight"] or 1.0),
                        )
                        for key, kind_, ref, title in (
                            (a_key, record["akind"], record["aref"], record["atitle"]),
                            (b_key, record["bkind"], record["bref"], record["btitle"]),
                        ):
                            if key not in nodes:
                                nodes[key] = GraphNode(
                                    key=key,
                                    ref=ref,
                                    kind=NodeKind(kind_),
                                    title=title,
                                    tenant_id=tenant_id,
                                )
                            if key not in seen:
                                next_frontier.add(key)
                # Commit this hop: frontier keys become "seen" only now, so a key
                # discovered at hop N is expanded at hop N+1 (not in the same pass).
                frontier = sorted(next_frontier - seen)
                seen.update(frontier)
                if len(seen) >= limit:
                    break
        for key in seed_keys:
            if key in nodes:
                seed_nodes.append(nodes[key])
        return GraphSubgraph(
            tenant_id=tenant_id,
            seeds=seed_nodes,
            nodes=[nodes[key] for key in sorted(nodes)],
            edges=sorted(edges.values(), key=lambda e: (e.source_key, e.target_key, e.kind)),
        )

    async def delete_tenant(self, tenant_id: UUID) -> int:
        await self.initialize()
        async with self._driver.session() as session:
            result = await session.run(
                Query(
                    "MATCH (n:RAGNode {tenant_id: $tenant}) "
                    "DETACH DELETE n RETURN count(n) AS removed"
                ),
                tenant=str(tenant_id),
            )
            record = await result.single()
            return int(record["removed"]) if record else 0


async def _merge_nodes(tx: AsyncManagedTransaction, tenant: str | None, batch: GraphBatch) -> None:
    if not batch.nodes:
        return
    await tx.run(
        (
            "UNWIND $nodes AS n "
            "MERGE (node:RAGNode {tenant_id: n.tenant_id, key: n.key}) "
            "ON CREATE SET node.kind = n.kind, node.ref = n.ref, node.title = n.title, "
            "node.extra = n.extra "
            "ON MATCH SET node.kind = n.kind, node.ref = n.ref, node.title = n.title, "
            "node.extra = n.extra"
        ),
        nodes=[
            {
                "tenant_id": str(node.tenant_id),
                "key": node.key,
                "kind": node.kind.value,
                "ref": node.ref,
                "title": node.title,
                # Neo4j stores no map properties: keyword/visibility surface is kept
                # as a canonical JSON string and parsed back on read.
                "extra": json.dumps(node.extra, sort_keys=True, default=str),
            }
            for node in batch.nodes
        ],
    )


def _chunks(items: list[str], size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]
