"""Slice 2: Graph-RAG projection store, retrieval side channel and wiring.

The memory store is the reference semantics; the same domain types drive the Neo4j
store (whose live test is docker-gated). Offline tests therefore assert behaviour
that must hold identically on Neo4j: per-tenant isolation, idempotent upserts,
identifier/entity seeding and typed-edge findings.
"""

import os
from uuid import UUID

import pytest

from servicemind.domain.evidence import EvidenceSourceType
from servicemind.domain.knowledge import (
    KnowledgeQuery,
    KnowledgeRAGResult,
    RetrievalPrincipal,
)
from servicemind.graphrag import (
    GraphBatch,
    IncidentRecord,
    MemoryGraphStore,
    NodeKind,
    make_edge,
    make_node,
    project_incidents,
)
from servicemind.graphrag.domain import EdgeKind
from servicemind.graphrag.retrieval import GraphRetriever, to_graph_evidence
from servicemind.graphrag.store import rank_matches
from servicemind.rag.service import EnterpriseRAG

TENANT = UUID("11111111-1111-4111-8111-111111111111")
OTHER = UUID("22222222-2222-4222-8222-222222222222")


def _vpn_topology() -> list[IncidentRecord]:
    return [
        IncidentRecord(
            ticket_ref="INC-1000",
            ticket_title="VPN auth failure at site A",
            ci_ref="ci-vpn-gw",
            ci_title="VPN Gateway",
            service_ref="svc-auth",
            service_title="Authentication Service",
            problem_ref="PRB-7",
            problem_title="Repeated VPN authentication failures",
            change_ref="CHG-40",
            change_title="Rotate VPN gateway service token",
            runbook_ref="runbook://rb-vpn-mfa",
            runbook_title="VPN Gateway MFA enrollment runbook",
        ),
        IncidentRecord(
            ticket_ref="INC-1001",
            ticket_title="MFA token replay blocked",
            ci_ref="ci-vpn-gw",
            ci_title="VPN Gateway",
        ),
        IncidentRecord(
            ticket_ref="INC-1002",
            ticket_title="Disk full on web host",
            ci_ref="ci-web-01",
            ci_title="Web Host",
            service_ref="svc-web",
            service_title="Web Portal",
        ),
    ]


async def _seed_topology(store: MemoryGraphStore) -> None:
    await store.apply_batch(project_incidents(TENANT, _vpn_topology()))


def _query(*, identifiers=(), entities=()) -> KnowledgeQuery:
    text = " ".join([*identifiers, *entities]) or "probe"
    return KnowledgeQuery(
        raw_query=text,
        normalized_query=text,
        identifiers=list(identifiers),
        entities=list(entities),
    )


def _principal(tenant_id: UUID = TENANT) -> RetrievalPrincipal:
    return RetrievalPrincipal(tenant_id=tenant_id, user_id="graph-test", entity_ids=frozenset({1}))


# ---------------------------------------------------------------- projection


def test_graph_batch_rejects_mixed_tenants() -> None:
    node_a = make_node(TENANT, kind=NodeKind.CI, ref="ci-a", title="CI A")
    node_b = make_node(OTHER, kind=NodeKind.CI, ref="ci-b", title="CI B")
    with pytest.raises(ValueError):
        GraphBatch(nodes=[node_a, node_b], edges=[])


def test_graph_batch_rejects_dangling_edge_refs() -> None:
    node = make_node(TENANT, kind=NodeKind.CI, ref="ci-a", title="CI A")
    edge = make_edge(TENANT, kind=EdgeKind.DEPENDS_ON, source="ci:missing", target=node.key)
    with pytest.raises(ValueError):
        GraphBatch(nodes=[node], edges=[edge])


def test_project_incidents_uses_spec_directions() -> None:
    batch = project_incidents(TENANT, _vpn_topology()[:2], similar=[("INC-1000", "INC-1001", 0.9)])
    edges = {(e.source_key, e.kind, e.target_key) for e in batch.edges}
    assert ("ticket:INC-1000", EdgeKind.AFFECTS, "ci:ci-vpn-gw") in edges
    assert ("ci:ci-vpn-gw", EdgeKind.DEPENDS_ON, "service:svc-auth") in edges
    assert ("ticket:INC-1000", EdgeKind.LINKED_TO, "problem:PRB-7") in edges
    assert ("problem:PRB-7", EdgeKind.RESOLVED_BY, "change:CHG-40") in edges
    assert ("change:CHG-40", EdgeKind.MODIFIES, "ci:ci-vpn-gw") in edges
    assert ("ticket:INC-1000", EdgeKind.SIMILAR_TO, "ticket:INC-1001") in edges


def test_project_incidents_projects_runbook_node_and_edge() -> None:
    batch = project_incidents(TENANT, _vpn_topology()[:1])
    runbook_keys = [node.key for node in batch.nodes if node.kind is NodeKind.RUNBOOK]
    assert runbook_keys == ["runbook:runbook://rb-vpn-mfa"]
    edges = {(e.source_key, e.kind, e.target_key) for e in batch.edges}
    assert (
        "ci:ci-vpn-gw",
        EdgeKind.HAS_RUNBOOK,
        "runbook:runbook://rb-vpn-mfa",
    ) in edges


# ---------------------------------------------------------------- memory store


@pytest.mark.asyncio
async def test_memory_store_upsert_is_idempotent() -> None:
    store = MemoryGraphStore()
    batch = project_incidents(TENANT, _vpn_topology()[:1])
    assert await store.apply_batch(batch) == len(batch.edges)
    # Identical re-projection must not add edges (MERGE semantics for Neo4j parity).
    assert await store.apply_batch(batch) == 0


@pytest.mark.asyncio
async def test_memory_store_match_isolates_tenants() -> None:
    store = MemoryGraphStore()
    await _seed_topology(store)
    # Another tenant shares the identifier namespace but holds a different record.
    await store.apply_batch(project_incidents(OTHER, [_vpn_topology()[0]]))
    tenant_hits = await store.match_nodes(TENANT, identifiers=["INC-1000"])
    assert [node.key for node in tenant_hits] == ["ticket:INC-1000"]
    # Querying the same id from the other tenant resolves only its own copy, and
    # neither store exposes the tenant's edges/nodes to the other principal.
    other_hits = await store.match_nodes(OTHER, identifiers=["INC-1000"])
    assert [node.key for node in other_hits] == ["ticket:INC-1000"]
    tenant_edges = (await store.subgraph(TENANT, ["ticket:INC-1000"])).edges
    other_edges = (await store.subgraph(OTHER, ["ticket:INC-1000"])).edges
    assert all(edge.tenant_id == TENANT for edge in tenant_edges)
    assert all(edge.tenant_id == OTHER for edge in other_edges)


def test_rank_matches_prefers_event_records_over_config() -> None:
    ci = make_node(TENANT, kind=NodeKind.CI, ref="ci:gw", title="vpn gateway")
    svc = make_node(TENANT, kind=NodeKind.SERVICE, ref="svc:x", title="vpn gateway svc")
    ticket = make_node(TENANT, kind=NodeKind.TICKET, ref="INC-9", title="vpn broken")
    entity_ranked = rank_matches([ci, svc, ticket], identifiers=[], entities=["vpn gateway"])
    assert entity_ranked[:1] == [ci]
    ref_ranked = rank_matches([ci, svc, ticket], identifiers=["INC-9"], entities=[])
    assert ref_ranked[:1] == [ticket]


@pytest.mark.asyncio
async def test_memory_store_subgraph_respects_hops_and_tenant() -> None:
    store = MemoryGraphStore()
    await _seed_topology(store)
    near = await store.subgraph(TENANT, ["ticket:INC-1000"], max_hops=1)
    near_keys = {node.key for node in near.nodes}
    # Hop 1: the ticket's own CI and the problem it links to.
    assert {"ci:ci-vpn-gw", "problem:PRB-7"} <= near_keys
    # Hop 2 (via the shared CI) would bring the sibling incident / service / change.
    assert "ticket:INC-1001" not in near_keys
    assert "service:svc-auth" not in near_keys
    full = await store.subgraph(TENANT, ["ticket:INC-1000"], max_hops=2)
    full_keys = {node.key for node in full.nodes}
    assert {"ticket:INC-1001", "service:svc-auth", "change:CHG-40"} <= full_keys
    assert "ticket:INC-1002" not in full_keys  # different CI unreachable
    assert all(node.tenant_id == TENANT for node in full.nodes)


@pytest.mark.asyncio
async def test_memory_store_subgraph_enforces_max_nodes_without_dangling_edges() -> None:
    store = MemoryGraphStore()
    await _seed_topology(store)

    bounded = await store.subgraph(TENANT, ["ticket:INC-1000"], max_hops=3, max_nodes=3)

    assert len(bounded.nodes) <= 3
    node_keys = {node.key for node in bounded.nodes}
    assert all(
        edge.source_key in node_keys and edge.target_key in node_keys for edge in bounded.edges
    )


def test_graph_subgraph_neighbors_only_reverse_symmetric_edges() -> None:
    batch = project_incidents(TENANT, _vpn_topology()[:2], similar=[("INC-1000", "INC-1001", 0.9)])
    subgraph = MemoryGraphStore()

    async def build():
        await subgraph.apply_batch(batch)
        return await subgraph.subgraph(TENANT, ["ticket:INC-1000"], max_hops=3)

    import asyncio

    value = asyncio.run(build())
    problem = value.find("problem:PRB-7")
    assert problem is not None
    assert value.neighbors(problem.key, EdgeKind.LINKED_TO) == []
    reverse_similar = value.neighbors("ticket:INC-1001", EdgeKind.SIMILAR_TO)
    assert [node.key for node in reverse_similar] == ["ticket:INC-1000"]


# ---------------------------------------------------------------- retrieval


@pytest.mark.asyncio
async def test_retriever_finds_same_ci_incidents_and_problem_path() -> None:
    store = MemoryGraphStore()
    await _seed_topology(store)
    findings = await GraphRetriever().retrieve(
        _query(identifiers=["INC-1000"]), _principal(), store
    )
    relations = {finding.relation for finding in findings}
    assert relations >= {"same_ci_incidents", "affected_service_impact"}
    assert "known_problem_change_path" in relations
    same_ci = next(f for f in findings if f.relation == "same_ci_incidents")
    refs = {node.ref for node in same_ci.nodes}
    assert "INC-1001" in refs and "INC-1002" not in refs
    problem = next(f for f in findings if f.relation == "known_problem_change_path")
    assert "CHG-40" in problem.narrative
    assert "rb-vpn-mfa" in problem.narrative  # runbook carried along the problem path


@pytest.mark.asyncio
async def test_retriever_ticket_anchor_surfaces_ci_runbook() -> None:
    store = MemoryGraphStore()
    await _seed_topology(store)
    findings = await GraphRetriever().retrieve(
        _query(identifiers=["INC-1000"]), _principal(), store
    )
    runbook = next(f for f in findings if f.relation == "ci_runbook")
    assert "rb-vpn-mfa" in runbook.narrative
    assert any(node.kind is NodeKind.RUNBOOK for node in runbook.nodes)


@pytest.mark.asyncio
async def test_retriever_change_anchor_finds_problem_path_and_runbook() -> None:
    store = MemoryGraphStore()
    await _seed_topology(store)
    findings = await GraphRetriever().retrieve(_query(identifiers=["CHG-40"]), _principal(), store)
    relations = {finding.relation for finding in findings}
    assert "known_change_scope" in relations
    change = next(f for f in findings if f.relation == "known_change_scope")
    assert "PRB-7" in change.narrative
    assert "rb-vpn-mfa" in change.narrative
    assert any(node.kind is NodeKind.RUNBOOK for node in change.nodes)


@pytest.mark.asyncio
async def test_retriever_runbook_anchor_reports_applicability() -> None:
    store = MemoryGraphStore()
    await _seed_topology(store)
    findings = await GraphRetriever().retrieve(_query(entities=["runbook"]), _principal(), store)
    assert findings
    applicability = next(f for f in findings if f.relation == "runbook_applicability")
    assert any(node.kind is NodeKind.RUNBOOK for node in [applicability.anchor])
    assert "INC-1000" in applicability.narrative


@pytest.mark.asyncio
async def test_retriever_entity_anchor_maps_service_to_incidents() -> None:
    store = MemoryGraphStore()
    await _seed_topology(store)
    findings = await GraphRetriever().retrieve(
        _query(entities=["Authentication Service"]), _principal(), store
    )
    assert findings and findings[0].relation == "service_incident_history"
    refs = {node.ref for node in findings[0].nodes}
    assert {"INC-1000", "INC-1001"} <= refs


@pytest.mark.asyncio
async def test_retriever_requires_anchor_and_respects_tenant() -> None:
    store = MemoryGraphStore()
    await _seed_topology(store)
    assert await GraphRetriever().retrieve(_query(), _principal(), store) == []
    assert (
        await GraphRetriever().retrieve(_query(identifiers=["INC-1000"]), _principal(OTHER), store)
        == []
    )


@pytest.mark.asyncio
async def test_findings_become_tenant_scoped_graph_evidence() -> None:
    store = MemoryGraphStore()
    await _seed_topology(store)
    findings = await GraphRetriever().retrieve(
        _query(identifiers=["INC-1000"]), _principal(), store
    )
    query = _query(identifiers=["INC-1000"])
    evidence = to_graph_evidence(TENANT, findings, query)
    assert evidence
    assert all(item.source_type is EvidenceSourceType.GRAPH for item in evidence)
    assert all(item.tenant_id == TENANT for item in evidence)
    assert all(item.provenance.provider == "memory-graph" for item in evidence)
    assert all(item.metadata["source_refs"] for item in evidence)  # structural citations
    assert all("citation" not in item.metadata for item in evidence)  # not a text citation


# ---------------------------------------------------------------- rag wiring


def _rag_result() -> KnowledgeRAGResult:
    return KnowledgeRAGResult(
        query=_query(identifiers=["INC-1000"]),
        items=[],
        retrieval_mode="dense_bm25_rrf_cross_encoder_parent",
        candidate_count=0,
        latency_ms=1.0,
    )


def _bare_rag(graph_store=None) -> EnterpriseRAG:
    # graph_evidence only touches graph_store; index/embedding/reranker are unused.
    return EnterpriseRAG(  # type: ignore[arg-type]
        index=None,
        embedding=None,
        reranker=None,
        graph_store=graph_store,
    )


@pytest.mark.asyncio
async def test_rag_graph_side_channel_appends_when_wired() -> None:
    store = MemoryGraphStore()
    await _seed_topology(store)
    rag = _bare_rag(store)
    evidence = await rag.graph_evidence(_principal(), _rag_result())
    assert evidence
    assert all(item.source_type is EvidenceSourceType.GRAPH for item in evidence)
    same_ci = next(item for item in evidence if item.metadata["relation"] == "same_ci_incidents")
    assert "INC-1001" in "|".join(same_ci.metadata["source_refs"])


@pytest.mark.asyncio
async def test_rag_graph_side_channel_is_absent_by_default() -> None:
    rag = _bare_rag(None)
    assert await rag.graph_evidence(_principal(), _rag_result()) == []


class _FailingStore(MemoryGraphStore):
    async def match_nodes(self, tenant_id, *, identifiers=(), entities=()):
        raise RuntimeError("neo4j unavailable")


@pytest.mark.asyncio
async def test_rag_graph_side_channel_degrades_silently() -> None:
    rag = _bare_rag(_FailingStore())
    assert await rag.graph_evidence(_principal(), _rag_result()) == []


# ---------------------------------------------------------------- live Neo4j


@pytest.mark.docker
@pytest.mark.asyncio
async def test_neo4j_store_live_projection_lifecycle() -> None:
    """One live pass against the real Neo4j (needs --run-docker + creds in env)."""
    password = os.environ.get("NEO4J_PASSWORD")
    if not password:
        pytest.skip("NEO4J_PASSWORD not set; cannot reach the graph container")
    from neo4j import AsyncGraphDatabase

    from servicemind.graphrag.neo4j import Neo4jGraphStore

    driver = AsyncGraphDatabase.driver(
        os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7687"),
        auth=(os.environ.get("NEO4J_USER", "neo4j"), password),
    )
    store = Neo4jGraphStore(driver)
    batch = project_incidents(TENANT, _vpn_topology())
    try:
        first = await store.apply_batch(batch)
        assert first == len(batch.edges)
        # Re-applying the projection must MERGE, never duplicate.
        second = await store.apply_batch(batch)
        assert second == len(batch.edges)
        hits = await store.match_nodes(TENANT, identifiers=["INC-1000"])
        assert [node.key for node in hits] == ["ticket:INC-1000"]
        sub = await store.subgraph(TENANT, ["ticket:INC-1000"], max_hops=2)
        keys = {node.key for node in sub.nodes}
        assert {"ticket:INC-1001", "service:svc-auth"} <= keys
        findings = await GraphRetriever().retrieve(
            _query(identifiers=["INC-1000"]), _principal(), store
        )
        assert {finding.relation for finding in findings} >= {
            "same_ci_incidents",
            "known_problem_change_path",
        }
    finally:
        await store.delete_tenant(TENANT)
        await store.close()
