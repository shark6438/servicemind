from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from servicemind.domain.evidence import (
    EVIDENCE_RESOURCE_ID_MAX,
    EVIDENCE_SOURCE_REF_MAX,
    Evidence,
    EvidenceSourceType,
)
from servicemind.domain.knowledge import KnowledgeQuery, RetrievalPrincipal
from servicemind.graphrag.domain import (
    EdgeKind,
    GraphEdge,
    GraphNode,
    GraphSubgraph,
    NodeKind,
)
from servicemind.graphrag.store import GraphAccessError, GraphStore


class GraphFinding(BaseModel):
    """A structural finding: an anchor node plus the typed path that explains it.

    The narrative is derived text over stored topology (not a verbatim document), so
    Evidence built from a finding cites the constituent nodes/edges rather than a
    parent chunk. The Reviewer treats GRAPH evidence as contextual support and never
    as a quotable knowledge citation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: UUID
    anchor: GraphNode
    relation: str = Field(min_length=1, max_length=100)
    narrative: str = Field(min_length=1, max_length=8000)
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    provider: str = Field(min_length=1, max_length=100)

    @property
    def source_refs(self) -> list[str]:
        ordered: list[str] = []
        for node in [self.anchor, *self.nodes]:
            if node.ref not in ordered:
                ordered.append(node.ref)
        return ordered


#: How many neighbours one finding names, and therefore how many it cites. Every producer
#: below renders a list into a sentence and then handed the *unabridged* list to the
#: finding as citation metadata; the prose was sliced and the data was not. The two lists
#: were never the same list, and the second one is the one that ships: ``to_graph_evidence``
#: serializes every node and edge into ``Evidence.metadata``, which is unbounded, and
#: ``GraphFinding.narrative`` is not -- a node label is a ref plus a title at their
#: ceilings, so five of them already reach the 8000-character narrative cap. A CI with
#: forty dependent services therefore raised inside ``_build``, and ``graph_evidence``
#: answers any exception by discarding the whole side channel: the richest topologies got
#: no graph evidence at all, silently. One number, applied to the sentence and to its
#: citations, keeps the two from drifting apart again.
_CITED_MAX = 6


def _bounded(value: str, limit: int) -> str:
    """Clip a graph identifier to the Evidence contract, marking the cut honestly.

    ``GraphNode.ref`` and ``GraphNode.key`` are bounded by Graph-RAG's own, looser limits,
    and they are built from external references this platform does not control -- a GLPI
    URI, a deep corpus path. Those values have to be bounded where they become evidence;
    the alternative was a ``ValidationError`` from ``Evidence.create``, which the
    knowledge branch read as "the graph side channel failed" and answered by discarding
    the task's text evidence along with it.
    """
    if len(value) <= limit:
        return value
    marker = "…[clipped]"
    return value[: limit - len(marker)] + marker


def to_graph_evidence(
    tenant_id: UUID,
    findings: list[GraphFinding],
) -> list[Evidence]:
    """Turn graph findings into evidence that says nothing about the query.

    The signature deliberately has no ``query``: a finding's evidence must be the
    finding. See the note on the metadata below.
    """
    return [
        Evidence.create(
            tenant_id=tenant_id,
            source_type=EvidenceSourceType.GRAPH,
            source_ref=_bounded(finding.source_refs[0], EVIDENCE_SOURCE_REF_MAX),
            resource_type=f"graph_{finding.relation}",
            resource_id=_bounded(finding.anchor.key, EVIDENCE_RESOURCE_ID_MAX),
            content=finding.narrative,
            provider=finding.provider,
            retrieval_method=f"graph_{finding.relation}",
            metadata={
                "relation": finding.relation,
                "provider": finding.provider,
                "anchor": {
                    "key": finding.anchor.key,
                    "kind": finding.anchor.kind.value,
                    "title": finding.anchor.title,
                    "ref": finding.anchor.ref,
                },
                "nodes": [
                    {"key": n.key, "kind": n.kind.value, "title": n.title, "ref": n.ref}
                    for n in finding.nodes
                ],
                "edges": [
                    {
                        "source_key": e.source_key,
                        "target_key": e.target_key,
                        "kind": e.kind.value,
                        "weight": e.weight,
                    }
                    for e in finding.edges
                ],
                "source_refs": finding.source_refs,
                # No ``query`` here. It used to carry the processed retrieval query,
                # which the governance layer serializes into the model-visible evidence
                # content; on a re-retrieval round that query is built from the
                # Reviewer's own feedback, so the model read its previous critique back
                # as retrieved evidence. Bookkeeping about how a fact was found is not
                # part of the fact.
            },
        )
        for finding in findings
    ]


def _forward(sub: GraphSubgraph, key: str, kind: EdgeKind) -> list[tuple[GraphEdge, GraphNode]]:
    out = []
    for edge in sub.edges:
        if edge.source_key == key and edge.kind is kind:
            target = sub.find(edge.target_key)
            if target is not None:
                out.append((edge, target))
    return out


def _backward(sub: GraphSubgraph, key: str, kind: EdgeKind) -> list[tuple[GraphEdge, GraphNode]]:
    out = []
    for edge in sub.edges:
        if edge.target_key == key and edge.kind is kind:
            source = sub.find(edge.source_key)
            if source is not None:
                out.append((edge, source))
    return out


def _runbooks_forward(sub: GraphSubgraph, keys: list[str]) -> list[tuple[GraphEdge, GraphNode]]:
    """Distinct runbooks reached by ``ci:HAS_RUNBOOK->runbook`` from any of ``keys``."""
    seen: set[str] = set()
    out: list[tuple[GraphEdge, GraphNode]] = []
    for key in keys:
        for edge, node in _forward(sub, key, EdgeKind.HAS_RUNBOOK):
            if node.key not in seen:
                seen.add(node.key)
                out.append((edge, node))
    return out


#: How much of one node's identity a sentence may quote. ``GraphNode.ref`` and
#: ``GraphNode.title`` each allow 1000 characters, so a single label could be 2000 -- and a
#: sentence naming six of them is 12,000 characters against ``GraphFinding.narrative``'s
#: 8,000 ceiling. The label exists to be read inside a finding; the node's full ref and
#: title travel in the finding's ``nodes``, which is where a consumer that needs them
#: exactly should look. Clipping here is what keeps the sentence and its citations the same
#: material at two different resolutions.
_LABEL_PART_MAX = 300


def _label(node: GraphNode) -> str:
    return f"{_bounded(node.ref, _LABEL_PART_MAX)} '{_named(node)}'"


def _named(node: GraphNode) -> str:
    """A node's display name as a sentence may quote it; ``_LABEL_PART_MAX`` applies here too.

    Half the narratives name a node by title alone -- "which service(s) depend on" -- and
    those joins were unbounded strings straight off the graph: six services at the title
    ceiling is 5,400 characters in one sentence, which is most of the narrative contract
    spent on names nobody reads.
    """
    return _bounded(node.title, _LABEL_PART_MAX)


class GraphRetriever:
    """Turns a processed query into structural findings over a ``GraphStore``.

    Textual hybrid retrieval stays the primary channel; this side channel answers
    "which incidents, services and known fixes hang off the same CI/entity" using the
    typed edges from the Phase 4 projection. Empty identifiers+entities means nothing
    to anchor on and yields no findings (degradation is silent by design).
    """

    def __init__(
        self,
        *,
        max_hops: int = 2,
        max_seed_anchors: int = 4,
        max_findings: int = 8,
        max_nodes: int = 200,
        max_edges: int = 2_000,
    ) -> None:
        self.max_hops = max_hops
        self.max_seed_anchors = max_seed_anchors
        self.max_findings = max_findings
        self.max_nodes = max_nodes
        self.max_edges = max_edges

    async def retrieve(
        self,
        query: KnowledgeQuery,
        principal: RetrievalPrincipal,
        store: GraphStore,
    ) -> list[GraphFinding]:
        # Refused rather than degraded. A store that cannot filter by the principal
        # would answer this query with nodes the caller has no claim to, and the caller
        # -- which treats every failure of this side channel as "no structural findings"
        # -- would read that as an ordinary empty result. ``graph_evidence`` catches this
        # one separately for exactly that reason.
        if not store.supports_node_acl:
            raise GraphAccessError(store.label)
        identifiers = list(query.identifiers)
        entities = list(query.entities)
        if not identifiers and not entities:
            return []
        matched = await store.match_nodes(principal, identifiers=identifiers, entities=entities)
        if not matched:
            return []
        event_like = (NodeKind.TICKET, NodeKind.PROBLEM, NodeKind.CHANGE)
        anchors = [node for node in matched if node.kind in event_like] or matched
        anchors = anchors[: self.max_seed_anchors]
        sub = await store.subgraph(
            principal,
            [node.key for node in anchors],
            max_hops=self.max_hops,
            max_nodes=self.max_nodes,
            max_edges=self.max_edges,
        )
        provider = f"{store.label}-graph"
        findings = [finding for anchor in anchors for finding in self._find(anchor, sub, provider)]
        findings.sort(key=lambda f: (-len(f.edges), f.relation, f.anchor.key))
        return findings[: self.max_findings]

    def _find(
        self,
        anchor: GraphNode,
        sub: GraphSubgraph,
        provider: str,
    ) -> list[GraphFinding]:
        findings: list[GraphFinding] = []

        # Anchor is an incident: who else hit the same CI, which services ride on it,
        # and which runbook governs its triage/recovery?
        if anchor.kind is NodeKind.TICKET:
            findings.extend(self._same_ci_and_services(anchor, sub, provider))
            findings.extend(self._known_problem_path(anchor, sub, provider))
            findings.extend(self._ci_runbook_findings(anchor, sub, provider))
        elif anchor.kind is NodeKind.PROBLEM:
            findings.extend(self._known_problem_path(anchor, sub, provider))
        elif anchor.kind is NodeKind.CHANGE:
            findings.extend(self._change_scope(anchor, sub, provider))
        elif anchor.kind is NodeKind.RUNBOOK:
            findings.extend(self._runbook_scope(anchor, sub, provider))
        elif anchor.kind is NodeKind.CI:
            history = _backward(sub, anchor.key, EdgeKind.AFFECTS)
            incident_nodes = [node for _, node in history if node.kind is NodeKind.TICKET]
            if incident_nodes:
                cited = incident_nodes[:_CITED_MAX]
                cited_edges = [edge for edge, _ in history][:_CITED_MAX]
                names = ", ".join(_label(node) for node in cited)
                findings.append(
                    self._build(
                        anchor,
                        "ci_incident_history",
                        f"{len(incident_nodes)} incident(s) target {_named(anchor)} "
                        f"({anchor.ref}): {names}. Sibling incidents on the same CI are a "
                        "shared-root-cause signal for this outage.",
                        cited,
                        cited_edges,
                        provider,
                    )
                )
            services = [node for _, node in _forward(sub, anchor.key, EdgeKind.DEPENDS_ON)]
            if services:
                dependencies = _forward(sub, anchor.key, EdgeKind.DEPENDS_ON)
                cited = services[:_CITED_MAX]
                cited_edges = [edge for edge, _ in dependencies][:_CITED_MAX]
                findings.append(
                    self._build(
                        anchor,
                        "service_configuration_scope",
                        f"CI {_named(anchor)} ({anchor.ref}) carries {len(services)} "
                        f"service(s): {', '.join(_named(node) for node in cited)}. Impact "
                        "assessment must include these services before declaring a "
                        "service outage.",
                        cited,
                        cited_edges,
                        provider,
                    )
                )
        elif anchor.kind is NodeKind.SERVICE:
            cus = [node for _, node in _backward(sub, anchor.key, EdgeKind.DEPENDS_ON)]
            tickets: list[tuple[GraphEdge, GraphNode]] = []
            for ci in cus:
                tickets.extend(
                    (edge, node)
                    for edge, node in _backward(sub, ci.key, EdgeKind.AFFECTS)
                    if node.kind is NodeKind.TICKET
                )
            seen_keys: set[str] = set()
            unique_tickets = []
            for edge, node in tickets:
                if node.key not in seen_keys:
                    seen_keys.add(node.key)
                    unique_tickets.append((edge, node))
            if unique_tickets:
                cited_tickets = unique_tickets[:_CITED_MAX]
                cited_cis = cus[:_CITED_MAX]
                incident_names = ", ".join(_label(node) for _, node in cited_tickets)
                ci_names = ", ".join(_named(node) for node in cited_cis)
                findings.append(
                    self._build(
                        anchor,
                        "service_incident_history",
                        f"{len(unique_tickets)} incident(s) touch service "
                        f"{_named(anchor)} ({anchor.ref}) through affected CI(s) "
                        f"{ci_names}: {incident_names}. Recurring incidents under the same "
                        "service point to a service-level fault, not a single CI.",
                        [node for _, node in cited_tickets] + list(cited_cis),
                        [edge for edge, _ in cited_tickets],
                        provider,
                    )
                )
        return findings

    def _same_ci_and_services(
        self, anchor: GraphNode, sub: GraphSubgraph, provider: str
    ) -> list[GraphFinding]:
        findings: list[GraphFinding] = []
        for edge, ci in _forward(sub, anchor.key, EdgeKind.AFFECTS):
            siblings = [
                node
                for sibling_edge, node in _backward(sub, ci.key, EdgeKind.AFFECTS)
                if node.kind is NodeKind.TICKET and node.key != anchor.key
            ]
            sibling_edges = [
                e for e, _ in _backward(sub, ci.key, EdgeKind.AFFECTS) if e.source_key != anchor.key
            ]
            cited_siblings = siblings[:_CITED_MAX]
            if siblings:
                findings.append(
                    self._build(
                        anchor,
                        "same_ci_incidents",
                        f"{_label(anchor)} affects CI {_named(ci)} ({ci.ref}); "
                        f"{len(siblings)} sibling incident(s) hit the same CI: "
                        f"{', '.join(_label(node) for node in cited_siblings)}. Same-CI "
                        "correlation points at a shared root cause rather than an "
                        "isolated fault.",
                        [ci, *cited_siblings],
                        [edge, *sibling_edges[:_CITED_MAX]],
                        provider,
                    )
                )
            services = [node for _, node in _forward(sub, ci.key, EdgeKind.DEPENDS_ON)]
            if services:
                cited = services[:_CITED_MAX]
                findings.append(
                    self._build(
                        anchor,
                        "affected_service_impact",
                        f"{_label(anchor)} affects CI {_named(ci)}, which {len(services)} "
                        f"service(s) depend on: {', '.join(_named(node) for node in cited)}. "
                        "Declare user-facing impact on those services and follow their "
                        "runbooks.",
                        [ci, *cited],
                        [
                            edge,
                            *[e for e, _ in _forward(sub, ci.key, EdgeKind.DEPENDS_ON)][
                                :_CITED_MAX
                            ],
                        ],
                        provider,
                    )
                )
        return findings

    def _ci_runbook_findings(
        self, anchor: GraphNode, sub: GraphSubgraph, provider: str
    ) -> list[GraphFinding]:
        """Surface the runbook that governs the CI an incident affects."""
        findings: list[GraphFinding] = []
        for _, ci in _forward(sub, anchor.key, EdgeKind.AFFECTS):
            runbooks = _runbooks_forward(sub, [ci.key])
            if not runbooks:
                continue
            cited = runbooks[:_CITED_MAX]
            narrative = (
                f"{_label(anchor)} affects CI {_named(ci)} ({ci.ref}), whose operating "
                f"runbook is {', '.join(_label(node) for _, node in cited)}. Follow "
                "that runbook's procedure for triage and recovery."
            )
            findings.append(
                self._build(
                    anchor,
                    "ci_runbook",
                    narrative,
                    [ci, *[node for _, node in cited]],
                    [edge for edge, _ in cited],
                    provider,
                )
            )
        return findings

    def _change_scope(
        self, anchor: GraphNode, sub: GraphSubgraph, provider: str
    ) -> list[GraphFinding]:
        """A Change anchor: which problem it resolved, linked incidents, CIs and runbooks."""
        problems = [node for _, node in _backward(sub, anchor.key, EdgeKind.RESOLVED_BY)]
        cis = [node for _, node in _forward(sub, anchor.key, EdgeKind.MODIFIES)]
        if not problems and not cis:
            return []
        tickets: list[GraphNode] = []
        for problem in problems:
            for _, node in _backward(sub, problem.key, EdgeKind.LINKED_TO):
                if node.kind is NodeKind.TICKET and not any(t.key == node.key for t in tickets):
                    tickets.append(node)
        runbook_pairs = _runbooks_forward(sub, [ci.key for ci in cis])
        runbooks = [node for _, node in runbook_pairs]
        cited_problems = problems[:_CITED_MAX]
        cited_tickets = tickets[:_CITED_MAX]
        cited_cis = cis[:_CITED_MAX]
        cited_runbooks = runbooks[:_CITED_MAX]
        parts = [f"Change {_label(anchor)}"]
        if problems:
            parts.append(
                "resolves known problem(s) " + ", ".join(_label(node) for node in cited_problems)
            )
        if tickets:
            parts.append(
                "linked to incident(s) " + ", ".join(_label(node) for node in cited_tickets)
            )
        if cis:
            parts.append("modifying CI(s) " + ", ".join(_named(node) for node in cited_cis))
        if runbooks:
            parts.append(
                "with operating runbook(s) " + ", ".join(_label(node) for node in cited_runbooks)
            )
        parts.append("Correlate new occurrences with this approved change and its runbook.")
        return [
            self._build(
                anchor,
                "known_change_scope",
                " ".join(parts),
                [*cited_problems, *cited_cis, *cited_tickets, *cited_runbooks],
                [
                    *[edge for edge, _ in _backward(sub, anchor.key, EdgeKind.RESOLVED_BY)][
                        :_CITED_MAX
                    ],
                    *[edge for edge, _ in _forward(sub, anchor.key, EdgeKind.MODIFIES)][
                        :_CITED_MAX
                    ],
                    *[
                        edge
                        for problem in cited_problems
                        for edge, _ in _backward(sub, problem.key, EdgeKind.LINKED_TO)
                    ][:_CITED_MAX],
                    *[edge for edge, _ in runbook_pairs][:_CITED_MAX],
                ],
                provider,
            )
        ]

    def _runbook_scope(
        self, anchor: GraphNode, sub: GraphSubgraph, provider: str
    ) -> list[GraphFinding]:
        """A Runbook anchor: which CIs/Changes reference it and what incidents hit them."""
        owners: list[GraphNode] = []
        owner_edges: list[GraphEdge] = []
        seen: set[str] = set()
        for edge in sub.edges:
            if edge.kind is EdgeKind.HAS_RUNBOOK and edge.target_key == anchor.key:
                owner = sub.find(edge.source_key)
                if owner is not None and owner.key not in seen:
                    seen.add(owner.key)
                    owners.append(owner)
                    owner_edges.append(edge)
        if not owners:
            return []
        cis = [node for node in owners if node.kind is NodeKind.CI]
        tickets: list[GraphNode] = []
        ticket_edges: list[GraphEdge] = []
        for ci in cis:
            for edge, node in _backward(sub, ci.key, EdgeKind.AFFECTS):
                if node.kind is NodeKind.TICKET and not any(t.key == node.key for t in tickets):
                    tickets.append(node)
                    ticket_edges.append(edge)
        cited_cis = cis[:_CITED_MAX]
        cited_tickets = tickets[:_CITED_MAX]
        # ``owners`` and ``owner_edges`` are built together, one pair per HAS_RUNBOOK edge,
        # so slicing them to the same length keeps every cited edge attached to a cited node.
        cited_owners = owners[:_CITED_MAX]
        narrative = (
            f"Runbook {_label(anchor)} is the operating procedure for "
            f"{len(cis)} CI(s): {', '.join(_named(node) for node in cited_cis)}. "
        )
        if tickets:
            narrative += (
                f"{len(tickets)} incident(s) affected these CI(s): "
                f"{', '.join(_label(node) for node in cited_tickets)}. "
            )
        narrative += "Follow this runbook when handling the current incident."
        return [
            self._build(
                anchor,
                "runbook_applicability",
                narrative,
                [*cited_owners, *cited_tickets],
                [*owner_edges[:_CITED_MAX], *ticket_edges[:_CITED_MAX]],
                provider,
            )
        ]

    def _known_problem_path(
        self, anchor: GraphNode, sub: GraphSubgraph, provider: str
    ) -> list[GraphFinding]:
        problems: list[GraphNode] = []
        if anchor.kind is NodeKind.TICKET:
            problems = [node for _, node in _forward(sub, anchor.key, EdgeKind.LINKED_TO)]
        elif anchor.kind is NodeKind.PROBLEM:
            problems = [anchor]
        if not problems:
            return []
        findings: list[GraphFinding] = []
        for problem in problems:
            changes = [node for _, node in _forward(sub, problem.key, EdgeKind.RESOLVED_BY)]
            if not changes:
                continue
            change_edges = [edge for edge, _ in _forward(sub, problem.key, EdgeKind.RESOLVED_BY)]
            modified = [
                node
                for change in changes
                for _, node in _forward(sub, change.key, EdgeKind.MODIFIES)
            ]
            runbook_pairs = _runbooks_forward(sub, [node.key for node in modified])
            runbooks = [node for _, node in runbook_pairs]
            cited_changes = changes[:_CITED_MAX]
            cited_modified = modified[:_CITED_MAX]
            cited_runbooks = runbooks[:_CITED_MAX]
            narrative = (
                f"Known-problem path for {_label(anchor)}: problem {problem.ref} "
                f"'{_named(problem)}' is resolved by change(s) "
                f"{', '.join(_label(node) for node in cited_changes)}"
            )
            if modified:
                narrative += f" modifying {', '.join(_named(node) for node in cited_modified)}"
            if runbooks:
                narrative += (
                    " with operating runbook(s) "
                    f"{', '.join(_label(node) for node in cited_runbooks)}"
                )
            narrative += ". Correlate new occurrences with the approved change before acting."
            findings.append(
                self._build(
                    anchor,
                    "known_problem_change_path",
                    narrative,
                    [problem, *cited_changes, *cited_modified, *cited_runbooks],
                    [
                        *change_edges[:_CITED_MAX],
                        *[
                            edge
                            for change in cited_changes
                            for edge, _ in _forward(sub, change.key, EdgeKind.MODIFIES)
                        ][:_CITED_MAX],
                        *[edge for edge, _ in runbook_pairs][:_CITED_MAX],
                    ],
                    provider,
                )
            )
        return findings

    def _build(
        self,
        anchor: GraphNode,
        relation: str,
        narrative: str,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
        provider: str,
    ) -> GraphFinding:
        return GraphFinding(
            tenant_id=anchor.tenant_id,
            anchor=anchor,
            relation=relation,
            narrative=narrative,
            nodes=nodes,
            edges=edges,
            provider=provider,
        )


def default_graph_retriever() -> GraphRetriever:
    return GraphRetriever()
