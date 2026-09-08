"""Graph-RAG side channel (Phase 4 baseline 4.2).

Structural retrieval over a typed ITSM projection (Ticket/Ci/Service/Problem/Change)
held in Neo4j, with an in-memory reference store of identical semantics for offline
tests and small demos. Text hybrid retrieval remains the primary knowledge channel;
findings here are contextual Evidence (``EvidenceSourceType.GRAPH``).
"""

from servicemind.graphrag.domain import (
    EdgeKind,
    GraphBatch,
    GraphEdge,
    GraphNode,
    GraphSubgraph,
    NodeKind,
)
from servicemind.graphrag.projection import (
    IncidentRecord,
    make_edge,
    make_node,
    project_incidents,
)
from servicemind.graphrag.retrieval import (
    GraphFinding,
    GraphRetriever,
    to_graph_evidence,
)
from servicemind.graphrag.store import GraphStore, MemoryGraphStore, rank_matches

__all__ = [
    "EdgeKind",
    "GraphBatch",
    "GraphEdge",
    "GraphFinding",
    "GraphNode",
    "GraphRetriever",
    "GraphStore",
    "GraphSubgraph",
    "IncidentRecord",
    "MemoryGraphStore",
    "NodeKind",
    "make_edge",
    "make_node",
    "project_incidents",
    "rank_matches",
    "to_graph_evidence",
]
