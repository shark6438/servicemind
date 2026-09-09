from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from servicemind.domain.evidence import Evidence, EvidenceSourceType
from servicemind.domain.knowledge import KnowledgeQuery, RetrievalPrincipal
from servicemind.graphrag.domain import (
    EdgeKind,
    GraphEdge,
    GraphNode,
    GraphSubgraph,
    NodeKind,
)
from servicemind.graphrag.store import GraphStore


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


def to_graph_evidence(
    tenant_id: UUID,
    findings: list[GraphFinding],
    query: KnowledgeQuery,
) -> list[Evidence]:
    return [
        Evidence.create(
            tenant_id=tenant_id,
            source_type=EvidenceSourceType.GRAPH,
            source_ref=finding.source_refs[0],
            resource_type=f"graph_{finding.relation}",
            resource_id=finding.anchor.key,
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
                "query": query.model_dump(mode="json"),
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


def _label(node: GraphNode) -> str:
    return f"{node.ref} '{node.title}'"


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
    ) -> None:
        self.max_hops = max_hops
        self.max_seed_anchors = max_seed_anchors
        self.max_findings = max_findings
        self.max_nodes = max_nodes

    async def retrieve(
        self,
        query: KnowledgeQuery,
        principal: RetrievalPrincipal,
        store: GraphStore,
    ) -> list[GraphFinding]:
        identifiers = list(query.identifiers)
        entities = list(query.entities)
        if not identifiers and not entities:
            return []
        matched = await store.match_nodes(
            principal.tenant_id, identifiers=identifiers, entities=entities
        )
        if not matched:
            return []
        event_like = (NodeKind.TICKET, NodeKind.PROBLEM, NodeKind.CHANGE)
        anchors = [node for node in matched if node.kind in event_like] or matched
        anchors = anchors[: self.max_seed_anchors]
        sub = await store.subgraph(
            principal.tenant_id,
            [node.key for node in anchors],
            max_hops=self.max_hops,
            max_nodes=self.max_nodes,
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
                edges = [edge for edge, _ in history]
                names = ", ".join(_label(node) for node in incident_nodes[:6])
                findings.append(
                    self._build(
                        anchor,
                        "ci_incident_history",
                        f"{len(incident_nodes)} incident(s) target {anchor.title} "
                        f"({anchor.ref}): {names}. Sibling incidents on the same CI are a "
                        "shared-root-cause signal for this outage.",
                        incident_nodes[:6],
                        edges,
                        provider,
                    )
                )
            services = [node for _, node in _forward(sub, anchor.key, EdgeKind.DEPENDS_ON)]
            if services:
                dependencies = _forward(sub, anchor.key, EdgeKind.DEPENDS_ON)
                findings.append(
                    self._build(
                        anchor,
                        "service_configuration_scope",
                        f"CI {anchor.title} ({anchor.ref}) carries service(s) "
                        f"{', '.join(node.title for node in services)}. Impact assessment "
                        "must include these services before declaring a service outage.",
                        services,
                        [edge for edge, _ in dependencies],
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
                incident_names = ", ".join(_label(node) for _, node in unique_tickets[:6])
                ci_names = ", ".join(node.title for node in cus[:4])
                finding_nodes = [node for _, node in unique_tickets[:8]]
                finding_nodes.extend(cus[:4])
                findings.append(
                    self._build(
                        anchor,
                        "service_incident_history",
                        f"{len(unique_tickets)} incident(s) touch service "
                        f"{anchor.title} ({anchor.ref}) through affected CI(s) "
                        f"{ci_names}: {incident_names}. Recurring incidents under the same "
                        "service point to a service-level fault, not a single CI.",
                        finding_nodes,
                        [edge for edge, _ in unique_tickets],
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
            related: list[GraphNode] = [ci, *siblings]
            edges = [
                edge,
                *[
                    e
                    for e, _ in _backward(sub, ci.key, EdgeKind.AFFECTS)
                    if e.source_key != anchor.key
                ],
            ]
            if siblings:
                findings.append(
                    self._build(
                        anchor,
                        "same_ci_incidents",
                        f"{_label(anchor)} affects CI {ci.title} ({ci.ref}); "
                        f"{len(siblings)} sibling incident(s) hit the same CI: "
                        f"{', '.join(_label(node) for node in siblings[:6])}. Same-CI "
                        "correlation points at a shared root cause rather than an "
                        "isolated fault.",
                        related[:7],
                        edges,
                        provider,
                    )
                )
            services = [node for _, node in _forward(sub, ci.key, EdgeKind.DEPENDS_ON)]
            if services:
                findings.append(
                    self._build(
                        anchor,
                        "affected_service_impact",
                        f"{_label(anchor)} affects CI {ci.title}, which service(s) "
                        f"{', '.join(node.title for node in services)} depend on. "
                        "Declare user-facing impact on those services and follow their "
                        "runbooks.",
                        [ci, *services],
                        [edge, *[e for e, _ in _forward(sub, ci.key, EdgeKind.DEPENDS_ON)]],
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
            narrative = (
                f"{_label(anchor)} affects CI {ci.title} ({ci.ref}), whose operating "
                f"runbook is {', '.join(_label(node) for _, node in runbooks)}. Follow "
                "that runbook's procedure for triage and recovery."
            )
            findings.append(
                self._build(
                    anchor,
                    "ci_runbook",
                    narrative,
                    [ci, *[node for _, node in runbooks]],
                    [edge for edge, _ in runbooks],
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
        runbooks = [node for _, node in _runbooks_forward(sub, [ci.key for ci in cis])]
        parts = [f"Change {_label(anchor)}"]
        if problems:
            parts.append(
                "resolves known problem(s) " + ", ".join(_label(node) for node in problems[:4])
            )
        if tickets:
            parts.append("linked to incident(s) " + ", ".join(_label(node) for node in tickets[:4]))
        if cis:
            parts.append("modifying CI(s) " + ", ".join(node.title for node in cis[:4]))
        if runbooks:
            parts.append(
                "with operating runbook(s) " + ", ".join(_label(node) for node in runbooks[:4])
            )
        parts.append("Correlate new occurrences with this approved change and its runbook.")
        return [
            self._build(
                anchor,
                "known_change_scope",
                " ".join(parts),
                [*problems, *cis, *tickets, *runbooks],
                [
                    *[edge for edge, _ in _backward(sub, anchor.key, EdgeKind.RESOLVED_BY)],
                    *[edge for edge, _ in _forward(sub, anchor.key, EdgeKind.MODIFIES)],
                    *[
                        edge
                        for problem in problems
                        for edge, _ in _backward(sub, problem.key, EdgeKind.LINKED_TO)
                    ],
                    *[edge for edge, _ in _runbooks_forward(sub, [ci.key for ci in cis])],
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
        narrative = (
            f"Runbook {_label(anchor)} is the operating procedure for "
            f"{', '.join(node.title for node in cis[:4])}. "
        )
        if tickets:
            narrative += (
                f"{len(tickets)} incident(s) affected these CI(s): "
                f"{', '.join(_label(node) for node in tickets[:4])}. "
            )
        narrative += "Follow this runbook when handling the current incident."
        return [
            self._build(
                anchor,
                "runbook_applicability",
                narrative,
                [*owners, *tickets],
                [*owner_edges, *ticket_edges],
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
            narrative = (
                f"Known-problem path for {_label(anchor)}: problem {problem.ref} "
                f"'{problem.title}' is resolved by change(s) "
                f"{', '.join(_label(node) for node in changes[:4])}"
            )
            if modified:
                narrative += f" modifying {', '.join(node.title for node in modified[:4])}"
            if runbooks:
                narrative += (
                    f" with operating runbook(s) {', '.join(_label(node) for node in runbooks[:4])}"
                )
            narrative += ". Correlate new occurrences with the approved change before acting."
            findings.append(
                self._build(
                    anchor,
                    "known_problem_change_path",
                    narrative,
                    [problem, *changes, *modified, *runbooks],
                    [
                        *change_edges,
                        *[
                            edge
                            for change in changes
                            for edge, _ in _forward(sub, change.key, EdgeKind.MODIFIES)
                        ],
                        *[edge for edge, _ in runbook_pairs],
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
