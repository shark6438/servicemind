"""Slice 2: Graph-RAG projection store, retrieval side channel and wiring.

The memory store is the reference semantics; the same domain types drive the Neo4j
store (whose live test is docker-gated). Offline tests therefore assert behaviour
that must hold identically on Neo4j: per-tenant isolation, idempotent upserts,
identifier/entity seeding and typed-edge findings.
"""

import logging
import os
from uuid import UUID

import pytest

from servicemind.domain.evidence import (
    EVIDENCE_RESOURCE_ID_MAX,
    EVIDENCE_SOURCE_REF_MAX,
    EvidenceSourceType,
)
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
from servicemind.graphrag.retrieval import GraphFinding, GraphRetriever, to_graph_evidence
from servicemind.graphrag.store import GraphAccessError, node_is_visible, rank_matches
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


def _principal(
    tenant_id: UUID = TENANT,
    *,
    entities: frozenset[int] = frozenset({1}),
    groups: frozenset[int] = frozenset(),
    profiles: frozenset[int] = frozenset(),
) -> RetrievalPrincipal:
    """A caller's authority in the coordinates a graph read is filtered by.

    Every coordinate is spelled out here rather than defaulted inside
    ``RetrievalPrincipal``: the difference between "holds nothing on this axis" and
    "declares nothing on this axis" is the whole boundary, and a helper that let one
    stand in for the other would make the denial cases unaskable.
    """
    return RetrievalPrincipal(
        tenant_id=tenant_id,
        user_id="graph-test",
        entity_ids=entities,
        group_ids=groups,
        profile_ids=profiles,
    )


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
    tenant_hits = await store.match_nodes(_principal(), identifiers=["INC-1000"])
    assert [node.key for node in tenant_hits] == ["ticket:INC-1000"]
    # Querying the same id from the other tenant resolves only its own copy, and
    # neither store exposes the tenant's edges/nodes to the other principal.
    other_hits = await store.match_nodes(_principal(OTHER), identifiers=["INC-1000"])
    assert [node.key for node in other_hits] == ["ticket:INC-1000"]
    tenant_edges = (await store.subgraph(_principal(), ["ticket:INC-1000"])).edges
    other_edges = (await store.subgraph(_principal(OTHER), ["ticket:INC-1000"])).edges
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
    near = await store.subgraph(_principal(), ["ticket:INC-1000"], max_hops=1)
    near_keys = {node.key for node in near.nodes}
    # Hop 1: the ticket's own CI and the problem it links to.
    assert {"ci:ci-vpn-gw", "problem:PRB-7"} <= near_keys
    # Hop 2 (via the shared CI) would bring the sibling incident / service / change.
    assert "ticket:INC-1001" not in near_keys
    assert "service:svc-auth" not in near_keys
    full = await store.subgraph(_principal(), ["ticket:INC-1000"], max_hops=2)
    full_keys = {node.key for node in full.nodes}
    assert {"ticket:INC-1001", "service:svc-auth", "change:CHG-40"} <= full_keys
    assert "ticket:INC-1002" not in full_keys  # different CI unreachable
    assert all(node.tenant_id == TENANT for node in full.nodes)


@pytest.mark.asyncio
async def test_memory_store_subgraph_enforces_max_nodes_without_dangling_edges() -> None:
    store = MemoryGraphStore()
    await _seed_topology(store)

    bounded = await store.subgraph(_principal(), ["ticket:INC-1000"], max_hops=3, max_nodes=3)

    assert len(bounded.nodes) <= 3
    node_keys = {node.key for node in bounded.nodes}
    assert all(
        edge.source_key in node_keys and edge.target_key in node_keys for edge in bounded.edges
    )


# ---------------------------------------------------------------- node ACLs


def _restricted_topology() -> list[IncidentRecord]:
    """Three incidents that differ only in who may see them.

    Structure is deliberately identical across the three: the CI, the titles and the
    references all have the same shape, so a difference in what comes back is
    attributable to the ACL and to nothing else.
    """
    return [
        IncidentRecord(
            ticket_ref="INC-2000",
            ticket_title="VPN auth failure for the network team",
            ci_ref="ci-vpn-gw-3",
            ci_title="VPN Gateway (network team)",
            group_ids=frozenset({3}),
        ),
        IncidentRecord(
            ticket_ref="INC-2001",
            ticket_title="VPN auth failure for the service desk",
            ci_ref="ci-vpn-gw-4",
            ci_title="VPN Gateway (service desk)",
            group_ids=frozenset({4}),
        ),
        IncidentRecord(
            ticket_ref="INC-2002",
            ticket_title="VPN auth failure with no group restriction",
            ci_ref="ci-vpn-gw-public",
            ci_title="VPN Gateway (unrestricted)",
        ),
    ]


def test_a_projection_derives_every_node_under_the_records_acl() -> None:
    """The record is one source document; nothing derived from it outlives its scope."""
    batch = project_incidents(TENANT, [_restricted_topology()[0]])
    assert batch.nodes, "the fixture must project something for this to say anything"
    assert {node.group_ids for node in batch.nodes} == {frozenset({3})}
    assert {node.entity_ids for node in batch.nodes} == {frozenset()}


def test_the_profile_axis_is_enforced_like_the_others() -> None:
    node = make_node(
        TENANT, kind=NodeKind.RUNBOOK, ref="rb-1", title="x", profile_ids=frozenset({7})
    )
    assert node_is_visible(node, _principal(profiles=frozenset({7})))
    assert not node_is_visible(node, _principal(profiles=frozenset({8})))
    # Holding nothing is denial on that axis, not a wildcard.
    assert not node_is_visible(node, _principal())


@pytest.mark.asyncio
async def test_a_restricted_node_is_found_by_its_group_and_by_no_one_else() -> None:
    """The positive half is load-bearing: "returns nothing" alone proves nothing.

    A store that answered nothing for every principal, or a query that simply failed
    to match, is indistinguishable from an enforced ACL if only the negative case is
    checked. So the same query is asked of a principal who holds the group and must
    return the node, and of two who do not and must return nothing -- with the
    unrestricted third record visible to the second of them, which is what rules out
    "the query is broken" as the explanation for the empty answers.
    """
    store = MemoryGraphStore()
    await store.apply_batch(project_incidents(TENANT, _restricted_topology()))

    in_group_3 = await store.match_nodes(
        _principal(groups=frozenset({3})), identifiers=["INC-2000"]
    )
    assert [node.key for node in in_group_3] == ["ticket:INC-2000"]

    in_group_4_only = await store.match_nodes(
        _principal(groups=frozenset({4})), identifiers=["INC-2000"]
    )
    assert in_group_4_only == []
    holding_nothing = await store.match_nodes(_principal(), identifiers=["INC-2000"])
    assert holding_nothing == []

    unrestricted = await store.match_nodes(_principal(), identifiers=["INC-2002"])
    assert [node.key for node in unrestricted] == ["ticket:INC-2002"]


@pytest.mark.asyncio
async def test_a_restricted_node_is_not_reachable_from_a_visible_one() -> None:
    """Filtering the seeds but not the traversal is the way around the filter.

    One hop from a node you are allowed to see is exactly where the node you are not
    allowed to see sits, so the edge into it has to be gone as well -- a returned edge
    is a returned reference to it.
    """
    batch = project_incidents(
        TENANT, _restricted_topology()[:2], similar=[("INC-2000", "INC-2001", 0.9)]
    )
    store = MemoryGraphStore()
    await store.apply_batch(batch)

    subgraph = await store.subgraph(
        _principal(groups=frozenset({3})), ["ticket:INC-2000"], max_hops=3
    )

    assert {node.key for node in subgraph.nodes} == {"ticket:INC-2000", "ci:ci-vpn-gw-3"}
    assert all("INC-2001" not in f"{edge.source_key}{edge.target_key}" for edge in subgraph.edges)


@pytest.mark.asyncio
async def test_a_restricted_node_cannot_be_used_as_a_seed() -> None:
    store = MemoryGraphStore()
    await store.apply_batch(project_incidents(TENANT, _restricted_topology()[:2]))

    subgraph = await store.subgraph(_principal(groups=frozenset({3})), ["ticket:INC-2001"])

    assert subgraph.seeds == []
    assert subgraph.nodes == []
    assert subgraph.edges == []


@pytest.mark.asyncio
async def test_a_matching_group_id_does_not_reach_across_the_tenant() -> None:
    """Group ids are per-realm and collide. Tenant is checked before the coordinates."""
    store = MemoryGraphStore()
    await store.apply_batch(project_incidents(OTHER, _restricted_topology()[:1]))

    hits = await store.match_nodes(
        _principal(TENANT, groups=frozenset({3})), identifiers=["INC-2000"]
    )

    assert hits == []


@pytest.mark.asyncio
async def test_the_retriever_refuses_a_store_that_cannot_apply_node_acls() -> None:
    """A store that cannot filter does not return fewer findings, it returns others'.

    Nothing about its results distinguishes those two, so the refusal has to happen on
    the declared capability rather than on an inspection of what came back.
    """

    class _UnfilterableStore(MemoryGraphStore):
        supports_node_acl = False

    store = _UnfilterableStore()
    await _seed_topology(store)

    with pytest.raises(GraphAccessError) as failure:
        await GraphRetriever().retrieve(_query(identifiers=["INC-1000"]), _principal(), store)

    assert failure.value.store == "memory"


@pytest.mark.asyncio
async def test_the_side_channel_reports_an_unfilterable_store_rather_than_degrading(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refused graph read and a broken one are not the same event.

    The advisory side channel legitimately degrades to nothing when the store is down.
    It must not do so quietly when the store *cannot be scoped*: both end in an empty
    list, and only one of them means the caller's own evidence is missing.
    """

    class _UnfilterableStore(MemoryGraphStore):
        supports_node_acl = False

    rag = _bare_rag(_UnfilterableStore())

    with caplog.at_level(logging.ERROR, logger="servicemind.rag.service"):
        assert await rag.graph_evidence(_principal(), _rag_result()) == []

    refusals = [
        record for record in caplog.records if "cannot apply node ACLs" in record.getMessage()
    ]
    assert len(refusals) == 1, "the refusal must be logged as its own event, exactly once"
    assert refusals[0].levelno == logging.ERROR


def test_graph_subgraph_neighbors_only_reverse_symmetric_edges() -> None:
    batch = project_incidents(TENANT, _vpn_topology()[:2], similar=[("INC-1000", "INC-1001", 0.9)])
    subgraph = MemoryGraphStore()

    async def build():
        await subgraph.apply_batch(batch)
        return await subgraph.subgraph(_principal(), ["ticket:INC-1000"], max_hops=3)

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
    evidence = to_graph_evidence(TENANT, findings)
    assert evidence
    assert all(item.source_type is EvidenceSourceType.GRAPH for item in evidence)
    assert all(item.tenant_id == TENANT for item in evidence)
    assert all(item.provenance.provider == "memory-graph" for item in evidence)
    assert all(item.metadata["source_refs"] for item in evidence)  # structural citations
    assert all("citation" not in item.metadata for item in evidence)  # not a text citation


def _long_identifier_finding() -> GraphFinding:
    """A finding whose identifiers are legal for Graph-RAG and illegal for Evidence."""
    ref = "glpi://ticket/" + "9" * 300
    node = make_node(TENANT, kind=NodeKind.TICKET, ref=ref, title="Incident with a long reference")
    return GraphFinding(
        tenant_id=TENANT,
        anchor=node,
        relation="similar_to",
        narrative="Two incidents share an authentication dependency.",
        provider="memory-graph",
    )


def test_a_long_external_identifier_becomes_evidence_instead_of_raising() -> None:
    """Graph-RAG's own bounds are looser than the Evidence contracts they feed.

    ``GraphNode.ref`` allows 1000 characters where ``Evidence.source_ref`` allows 500, and
    ``GraphNode.key`` allows 500 where ``Evidence.resource_id`` allows 255 -- and both are
    built from external references this platform does not control. Converting a finding
    therefore used to raise inside ``Evidence.create``. That exception left
    ``graph_evidence`` before its guard and the knowledge node reads any raise there as a
    failed knowledge task, so one long identifier discarded the run's *text* evidence too.
    """
    finding = _long_identifier_finding()
    assert len(finding.anchor.key) > 255, "the fixture must exceed the Evidence contract"

    (item,) = to_graph_evidence(TENANT, [finding])

    assert len(item.source_ref) <= EVIDENCE_SOURCE_REF_MAX
    assert len(item.resource_id) <= EVIDENCE_RESOURCE_ID_MAX
    # The clip marks itself and keeps the identifier's head, which is what it identifies by.
    assert item.resource_id.startswith("ticket:glpi://ticket/")
    assert item.resource_id.endswith("…[clipped]")
    assert item.content == finding.narrative, "the narrative is separately bounded"


#: At the Graph-RAG ceilings. A node label is a ref plus a title, so a sentence naming a
#: handful of these is where ``GraphFinding.narrative`` is decided. ``key`` is
#: ``{kind}:{ref}`` and allows 500, which caps a ticket ref just below 500.
_TITLE_CEILING = 1000
_REF_CEILING = 490


def _at_ceiling(prefix: str, length: int) -> str:
    """A field value that fills its contract: ``length`` characters, and legal."""
    return prefix + "x" * (length - len(prefix))


def _wide_topology(siblings: int) -> list[IncidentRecord]:
    """One CI at the centre of ``siblings`` incidents, each an equal claimant on it.

    Legal at every field: the CI really is affected by all of them, and each ref and title
    is inside ``GraphNode``'s 1000-character contract. An ITSM graph is not sparse in the
    middle -- a gateway half the estate routes through looks exactly like this, and the
    long refs are the ordinary case for a URI or a corpus path.
    """
    records = [
        IncidentRecord(
            ticket_ref="INC-1000",
            ticket_title="VPN auth failure at site A",
            ci_ref="ci-vpn-gw",
            ci_title="VPN Gateway",
            service_ref="svc-auth",
            service_title="Authentication Service",
        )
    ]
    for index in range(siblings):
        records.append(
            IncidentRecord(
                ticket_ref=_at_ceiling(f"glpi://ticket/INC-2{index:03d}/", _REF_CEILING),
                ticket_title=_at_ceiling(f"Incident {index} ", _TITLE_CEILING),
                ci_ref="ci-vpn-gw",
                ci_title="VPN Gateway",
                service_ref=f"svc-{index}",
                service_title=_at_ceiling(f"Service {index} ", _TITLE_CEILING),
            )
        )
    return records


@pytest.mark.asyncio
async def test_a_wide_topology_yields_graph_evidence_instead_of_nothing() -> None:
    """The side channel's guard turns an unbounded sentence into silence, not into an error.

    Every producer rendered a list into a sentence and then handed the *whole* list to the
    finding. ``GraphFinding.narrative`` allows 8000 characters and a label is two 1000-
    character fields, so a CI with forty dependent services raised inside ``_build`` --
    and ``graph_evidence`` answers any exception by returning no graph evidence at all.
    The richest topologies were the ones that got nothing, and nothing said so.
    """
    store = MemoryGraphStore()
    await store.apply_batch(project_incidents(TENANT, _wide_topology(40)))

    evidence = await _bare_rag(store).graph_evidence(_principal(), _rag_result())

    assert evidence, "a wide but legal topology must still produce graph evidence"
    assert any(item.metadata["relation"] == "affected_service_impact" for item in evidence)


@pytest.mark.asyncio
async def test_a_finding_cites_the_neighbours_its_sentence_names_and_no_more() -> None:
    """The prose was sliced to a handful; the citation metadata shipped the entire subgraph.

    ``to_graph_evidence`` serializes every node and edge into ``Evidence.metadata``, which
    is unbounded -- so the padding the sentence refused to print was still going to the
    model, as raw JSON, at full size.
    """
    store = MemoryGraphStore()
    await store.apply_batch(project_incidents(TENANT, _wide_topology(40)))
    findings = await GraphRetriever().retrieve(
        _query(identifiers=["INC-1000"]), _principal(), store
    )

    assert findings
    for finding in findings:
        assert len(finding.narrative) <= 8000
        # Six named neighbours, plus the anchor's own edges, plus the anchor node itself.
        assert len(finding.nodes) <= 13, finding.relation
        assert len(finding.edges) <= 13, finding.relation


def test_the_subgraph_edge_bound_is_honoured_by_the_port() -> None:
    """``max_nodes`` bounded the nodes and nothing bounded the edges.

    Both stores collect every edge incident to a node in the set. One CI a thousand
    incidents have affected is ordinary, and all of those edges qualify.
    """
    import asyncio

    store = MemoryGraphStore()

    async def build():
        await store.apply_batch(project_incidents(TENANT, _wide_topology(50)))
        # Node keys are ``{kind}:{ref}`` (see ``make_node``); the CI's ref is ``ci-vpn-gw``.
        return await store.subgraph(_principal(), ["ci:ci-vpn-gw"], max_nodes=200, max_edges=10)

    subgraph = asyncio.run(build())

    assert len(subgraph.edges) <= 10
    assert len(subgraph.nodes) > 10, "the node side of the bound must be untouched"


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
    async def match_nodes(self, principal, *, identifiers=(), entities=()):
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
        hits = await store.match_nodes(_principal(), identifiers=["INC-1000"])
        assert [node.key for node in hits] == ["ticket:INC-1000"]
        sub = await store.subgraph(_principal(), ["ticket:INC-1000"], max_hops=2)
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


@pytest.mark.docker
@pytest.mark.asyncio
async def test_neo4j_store_live_applies_node_acls_on_both_read_paths() -> None:
    """The Cypher filter is a separate implementation and needs its own evidence.

    ``match_nodes`` and ``subgraph`` assemble their predicates independently, and the
    traversal filters *both* endpoints. The offline suite exercises the memory store's
    reading of the same rule and cannot see any of that; on a graph written before the
    coordinates existed it also has to read an absent property as the empty list rather
    than as a null that drops the row.
    """
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
    batch = project_incidents(
        TENANT, _restricted_topology()[:2], similar=[("INC-2000", "INC-2001", 0.9)]
    )
    try:
        await store.apply_batch(batch)

        held = await store.match_nodes(_principal(groups=frozenset({3})), identifiers=["INC-2000"])
        assert [node.key for node in held] == ["ticket:INC-2000"]
        withheld = await store.match_nodes(
            _principal(groups=frozenset({4})), identifiers=["INC-2000"]
        )
        assert withheld == []
        assert held[0].group_ids == frozenset({3}), "the coordinates must round-trip"

        reachable = await store.subgraph(
            _principal(groups=frozenset({3})), ["ticket:INC-2000"], max_hops=2
        )
        keys = {node.key for node in reachable.nodes}
        assert keys == {"ticket:INC-2000", "ci:ci-vpn-gw-3"}
        assert all(
            "INC-2001" not in f"{edge.source_key}{edge.target_key}" for edge in reachable.edges
        )

        no_seed = await store.subgraph(_principal(groups=frozenset({3})), ["ticket:INC-2001"])
        assert no_seed.nodes == [] and no_seed.seeds == []
    finally:
        await store.delete_tenant(TENANT)
        await store.close()


def test_the_cypher_predicate_covers_every_axis_and_reads_absent_as_unrestricted() -> None:
    """A structural guard, because this filter cannot be exercised without a cluster.

    ``_acl_where`` is assembled as text and evaluated by Neo4j, so the only offline
    evidence available is the shape of the text it produces. That is weaker than the
    docker-gated test above and is stated as what it is: it catches a predicate that has
    lost an axis or lost its ``coalesce``, and it does not prove that Cypher evaluates
    the predicate the way the memory store evaluates ``node_is_visible``.
    """
    from servicemind.graphrag.neo4j import _acl_where

    fragment = _acl_where("n")

    for axis in ("entity_ids", "group_ids", "profile_ids"):
        # ``coalesce`` is what keeps a node written before the coordinates existed
        # visible: ``size(null)`` is null, a null conjunct drops the row, and every
        # pre-existing node would silently disappear.
        assert f"coalesce(n.{axis}, [])" in fragment
        assert f"x IN ${axis}" in fragment, "the principal's set must arrive as a parameter"
    assert fragment.count(" OR ") == 3, "exactly one unrestricted escape per axis"


def test_both_cypher_read_paths_filter_the_whole_path() -> None:
    """The two Cypher reads assemble their predicates separately, so both are checked."""
    import inspect

    from servicemind.graphrag.neo4j import Neo4jGraphStore

    assert Neo4jGraphStore.supports_node_acl, "the side channel refuses a store that says no"
    assert "_acl_where('n')" in inspect.getsource(Neo4jGraphStore.match_nodes)
    traversal = inspect.getsource(Neo4jGraphStore.subgraph)
    assert "_acl_where('a')" in traversal, "the requested endpoint"
    assert "_acl_where('b')" in traversal, (
        "an edge from a visible node into a hidden one is a one-hop path past the filter"
    )


def test_the_neo4j_acl_parameters_are_the_principals_own_coordinates() -> None:
    from servicemind.graphrag.neo4j import _acl_params

    params = _acl_params(_principal(entities=frozenset({2, 1}), groups=frozenset({4, 3})))

    assert params == {"entity_ids": [1, 2], "group_ids": [3, 4], "profile_ids": []}


def test_two_stores_do_not_get_two_readings_of_the_same_rule() -> None:
    """``node_is_visible`` is the shared implementation both stores delegate to.

    A store that re-derived the rule could agree with this one today and disagree
    tomorrow; the port exposes the predicate so there is exactly one place to disagree
    from, and this is the assertion that keeps the delegation in place.
    """
    from servicemind.graphrag.neo4j import Neo4jGraphStore

    for store in (MemoryGraphStore, Neo4jGraphStore):
        assert store.supports_node_acl is True

    restricted = make_node(
        TENANT, kind=NodeKind.TICKET, ref="INC-9", title="t", group_ids=frozenset({3})
    )
    assert node_is_visible(restricted, _principal(groups=frozenset({3})))
    assert not node_is_visible(restricted, _principal(groups=frozenset({4})))
