"""The graph-side fixture ACC-13 reads, and the probe that reads it.

One module, two callers, because the fixture and its check are the same knowledge: the
seeding script lays these nodes down and the live driver asks the same question of them.
A check written separately from the probe would be able to pass over a graph the case
cannot actually read.

The topology is the smallest one that can tell *filtered* apart from *absent*:

    ticket A ──AFFECTS──▶ CI ──HAS_RUNBOOK──▶ runbook G3 (group 3)
                            └──HAS_RUNBOOK──▶ runbook G4 (group 4)

Both runbooks hang off the same CI, reached by the same traversal from the same anchor,
and they differ in exactly one field. So a principal holding group 3 and a principal
holding group 4 run one query over one graph and get different answers -- and that
difference is the ACL, not the topology. A runbook node that only one of them could
reach, or that sat on a different path, would make "the restricted node was not
returned" true for reasons that have nothing to do with the ACL.
"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from servicemind.domain.knowledge import KnowledgeQuery, RetrievalPrincipal
from servicemind.graphrag.domain import EdgeKind, GraphBatch, GraphEdge, GraphNode, NodeKind
from servicemind.graphrag.retrieval import GraphRetriever
from servicemind.graphrag.store import GraphStore

#: The runbook nodes the fixture projects, and the group each one is restricted to.
#:
#: Named here rather than in the case list so the seeding script, the check and the probe
#: all read one declaration; the acceptance case refers to these keys by the same
#: ``source_record_id`` the knowledge manifest uses, because the CI's operating runbook
#: and the runbook document are the same knowledge object.
GRAPH_RUNBOOK_GROUPS: dict[str, int] = {
    "KB-GLOBEX-VPN-MFA-G3": 3,
    "KB-GLOBEX-VPN-MFA-G4": 4,
}

#: The shared CI. One node, so the two runbooks are one hop apart from the anchor.
#:
#: ``entity_ids`` and not groups: the CI belongs to the Globex entity, which every
#: subject in this acceptance holds. Restricting it on the group axis would delete the
#: path for one of the two principals and the comparison would be between a missing
#: path and a present one rather than between two readings of the same path.
_FIXTURE_CI_KEY = "ci:acceptance//vpn-gateway"
_FIXTURE_CI_REF = "acceptance://globex/ci/vpn-gateway"


def runbook_ref(source_record_id: str) -> str:
    """The graph node ref for a runbook document, in the knowledge corpus' own URI shape."""
    return f"acceptance://globex/{source_record_id}"


def _runbook_key(source_record_id: str) -> str:
    return f"runbook::{runbook_ref(source_record_id)}"


def _ticket_key(ticket_id: int) -> str:
    return f"ticket:glpi//{ticket_id}"


def _ticket_ref(ticket_id: int) -> str:
    return f"glpi://ticket/{ticket_id}"


def graph_fixture_batch(
    tenant_id: UUID,
    *,
    entity_id: int,
    ticket_ids: Iterable[int],
) -> GraphBatch:
    """The whole fixture as one batch, so one apply leaves the graph consistent."""
    entities = frozenset({entity_id})
    nodes = [
        GraphNode(
            key=_FIXTURE_CI_KEY,
            ref=_FIXTURE_CI_REF,
            kind=NodeKind.CI,
            title="Globex VPN gateway (acceptance fixture)",
            tenant_id=tenant_id,
            entity_ids=entities,
        )
    ]
    edges: list[GraphEdge] = []
    for ticket_id in ticket_ids:
        nodes.append(
            GraphNode(
                key=_ticket_key(ticket_id),
                ref=_ticket_ref(ticket_id),
                kind=NodeKind.TICKET,
                title=f"[P7.6-ACCEPTANCE] VPN MFA failure on ticket {ticket_id}",
                tenant_id=tenant_id,
                entity_ids=entities,
            )
        )
        edges.append(
            GraphEdge(
                source_key=_ticket_key(ticket_id),
                target_key=_FIXTURE_CI_KEY,
                kind=EdgeKind.AFFECTS,
                tenant_id=tenant_id,
            )
        )
    for source_record_id, group_id in GRAPH_RUNBOOK_GROUPS.items():
        nodes.append(
            GraphNode(
                key=_runbook_key(source_record_id),
                ref=runbook_ref(source_record_id),
                kind=NodeKind.RUNBOOK,
                title=f"Runbook {source_record_id}",
                tenant_id=tenant_id,
                entity_ids=entities,
                # Group-restricted on purpose. Without this the fixture would prove
                # nothing: it is the field whose effect the case measures.
                group_ids=frozenset({group_id}),
            )
        )
        edges.append(
            GraphEdge(
                source_key=_FIXTURE_CI_KEY,
                target_key=_runbook_key(source_record_id),
                kind=EdgeKind.HAS_RUNBOOK,
                tenant_id=tenant_id,
            )
        )
    return GraphBatch(nodes=nodes, edges=edges)


def principal_for(
    tenant_id: UUID,
    *,
    user_id: str,
    entity_ids: set[int],
    group_ids: set[int],
) -> RetrievalPrincipal:
    """A principal shaped like the ones the platform builds from a token."""
    return RetrievalPrincipal(
        tenant_id=tenant_id,
        user_id=user_id,
        entity_ids=frozenset(entity_ids),
        group_ids=frozenset(group_ids),
    )


def fixture_ids_in_refs(refs: Iterable[str]) -> list[str]:
    """Which fixture runbooks a set of graph refs names, by exact URI and not by substring.

    Substring matching would let ``KB-GLOBEX-VPN-MFA-G3-DRAFT`` count as the G3 runbook,
    which is the same class of mistake as a citation that resolves to the wrong document.
    """
    by_ref = {
        runbook_ref(source_record_id): source_record_id for source_record_id in GRAPH_RUNBOOK_GROUPS
    }
    found: list[str] = []
    for ref in refs:
        source_record_id = by_ref.get(ref)
        if source_record_id is not None and source_record_id not in found:
            found.append(source_record_id)
    return sorted(found)


async def graph_fixture_reading(
    store: GraphStore,
    principal: RetrievalPrincipal,
    *,
    ticket_id: int,
) -> list[str]:
    """Read the fixture through the production retriever, as this principal.

    Through ``GraphRetriever`` and not around it: the case asserts that the platform's
    own traversal respects the node ACL, and a probe that read the store directly would
    be testing the store while the claim is about the retriever that calls it.

    The query is built here rather than passed through ``query_processor``, and that is
    a deliberate limit on what this probe claims. The processor derives identifiers from
    the question's text with a regex for ``INC-123``-shaped tokens, and a GLPI ticket id
    is a bare number that no such pattern matches -- so on the real path the anchor comes
    from the model's rewrite, which is not reproducible enough to hang an isolation
    assertion on. Pinning the identifier makes this a controlled experiment over the
    production traversal: same graph, same retriever, same store, two principals, one
    variable. It does not claim to reproduce any particular run's anchoring.
    """
    query = KnowledgeQuery(
        raw_query=f"VPN MFA ticket {ticket_id} related incidents",
        normalized_query=f"VPN MFA ticket {ticket_id} related incidents",
        identifiers=[str(ticket_id)],
    )
    findings = await GraphRetriever().retrieve(query, principal, store)
    refs = [ref for finding in findings for ref in finding.source_refs]
    return fixture_ids_in_refs(refs)


async def graph_fixture_problems(
    store: GraphStore,
    tenant_id: UUID,
    *,
    entity_id: int,
    ticket_id: int,
) -> list[str]:
    """Whether the fixture gives ACC-13 both readings it needs.

    Run at seed time, not only at case time: a fixture that has drifted into a state
    where the case cannot distinguish filtering from absence would otherwise be reported
    as a platform failure by the next run.
    """
    expected = {
        source_record_id: group_id for source_record_id, group_id in GRAPH_RUNBOOK_GROUPS.items()
    }
    problems: list[str] = []
    for source_record_id, group_id in expected.items():
        reading = await graph_fixture_reading(
            store,
            principal_for(
                tenant_id,
                user_id=f"acceptance-fixture-check-g{group_id}",
                entity_ids={entity_id},
                group_ids={group_id},
            ),
            ticket_id=ticket_id,
        )
        if source_record_id not in reading:
            problems.append(
                f"the principal holding group {group_id} cannot reach {source_record_id}; "
                f"the graph fixture is incomplete (saw {reading})"
            )
        others = sorted(set(reading) - {source_record_id})
        for other in others:
            if expected[other] != group_id:
                problems.append(
                    f"a principal holding group {group_id} reached {other}, which is "
                    f"restricted to group {expected[other]}: the node ACL is not filtering"
                )
    return problems
