from __future__ import annotations

from core import settings


def build_graph_store():
    """Construct the configured GraphStore, or ``None`` when Graph-RAG is disabled.

    A store is only created when the operator opted in (``SERVICEMIND_GRAPH_RAG_ENABLED``)
    and supplied a password; the Neo4j driver connects lazily, so the first query, not
    this call, is where an unreachable server surfaces. The KnowledgeAgent degrades
    graph findings gracefully on any store failure.
    """
    if not settings.SERVICEMIND_GRAPH_RAG_ENABLED or not settings.NEO4J_PASSWORD:
        return None
    from neo4j import AsyncGraphDatabase

    from servicemind.graphrag.neo4j import Neo4jGraphStore

    driver = AsyncGraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD.get_secret_value()),
    )
    return Neo4jGraphStore(driver)
