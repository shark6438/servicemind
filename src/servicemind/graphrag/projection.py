from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from servicemind.graphrag.domain import (
    EdgeKind,
    GraphBatch,
    GraphEdge,
    GraphNode,
    NodeKind,
)


def make_node(
    tenant_id: UUID,
    *,
    kind: NodeKind,
    ref: str,
    title: str,
    extra: dict[str, Any] | None = None,
    entity_ids: frozenset[int] = frozenset(),
    group_ids: frozenset[int] = frozenset(),
    profile_ids: frozenset[int] = frozenset(),
) -> GraphNode:
    """Stable key = ``{kind}:{ref}`` so re-projecting the same source is idempotent.

    The ACL coordinates have no default *value* to fall back on: they are copied from
    whatever the source record declares, and a source that declares nothing produces an
    unrestricted node. Making this a default argument rather than a required one would
    have been the trap -- a projection site that forgot to pass the coordinates would
    have produced a node indistinguishable from a deliberately public one.
    """
    return GraphNode(
        key=f"{kind.value}:{ref}",
        ref=ref,
        kind=kind,
        title=title,
        tenant_id=tenant_id,
        extra=extra or {},
        entity_ids=entity_ids,
        group_ids=group_ids,
        profile_ids=profile_ids,
    )


def make_edge(
    tenant_id: UUID,
    *,
    kind: EdgeKind,
    source: str | GraphNode,
    target: str | GraphNode,
    weight: float = 1.0,
) -> GraphEdge:
    source_key = source.key if isinstance(source, GraphNode) else source
    target_key = target.key if isinstance(target, GraphNode) else target
    return GraphEdge(
        source_key=source_key,
        target_key=target_key,
        kind=kind,
        tenant_id=tenant_id,
        weight=weight,
    )


@dataclass(frozen=True)
class IncidentRecord:
    """One GLPI/ITSM incident rendered into the structural projection."""

    ticket_ref: str
    ticket_title: str
    ci_ref: str
    ci_title: str
    service_ref: str | None = None
    service_title: str | None = None
    problem_ref: str | None = None
    problem_title: str | None = None
    change_ref: str | None = None
    change_title: str | None = None
    #: The runbook/SOP that governs triage and recovery for this incident's CI.
    #: Projected as a RUNBOOK node with ``ci:HAS_RUNBOOK->runbook``.
    runbook_ref: str | None = None
    runbook_title: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    #: Who may see the incident this record describes. Every node the record projects --
    #: the ticket, and the CI/service/problem/change/runbook hanging off it -- inherits
    #: these, because the record is one source document and a node derived from it is not
    #: more visible than the document.
    entity_ids: frozenset[int] = frozenset()
    group_ids: frozenset[int] = frozenset()
    profile_ids: frozenset[int] = frozenset()


def project_incidents(
    tenant_id: UUID,
    records: list[IncidentRecord],
    *,
    similar: Sequence[tuple[str, str, float]] = (),
) -> GraphBatch:
    """Project incident records into a typed GraphBatch (Phase 4 baseline 4.2).

    Edges follow the frozen spec directions exactly: Ticket-AFFECTS->CI,
    CI-DEPENDS_ON->Service, Ticket-LINKED_TO->Problem, Problem-RESOLVED_BY->Change,
    Change-MODIFIES->CI, CI-HAS_RUNBOOK->Runbook (when the record carries one).
    ``similar`` lists ``(ticket_ref_a, ticket_ref_b, weight)`` pairs that become
    Ticket-SIMILAR_TO->Ticket edges.
    """
    nodes: dict[str, GraphNode] = {}
    edges: dict[tuple[str, str, EdgeKind], GraphEdge] = {}

    def add_node(
        kind: NodeKind,
        ref: str,
        title: str,
        extra: dict[str, Any],
        acl: tuple[frozenset[int], frozenset[int], frozenset[int]] = (
            frozenset(),
            frozenset(),
            frozenset(),
        ),
    ) -> None:
        entity_ids, group_ids, profile_ids = acl
        node = make_node(
            tenant_id,
            kind=kind,
            ref=ref,
            title=title,
            extra=extra,
            entity_ids=entity_ids,
            group_ids=group_ids,
            profile_ids=profile_ids,
        )
        nodes[node.key] = node

    def add_edge(kind: EdgeKind, source: str, target: str, weight: float = 1.0) -> None:
        edges[(source, target, kind)] = make_edge(
            tenant_id, kind=kind, source=source, target=target, weight=weight
        )

    by_ticket: dict[str, IncidentRecord] = {}
    for record in records:
        by_ticket[record.ticket_ref] = record
        acl = (record.entity_ids, record.group_ids, record.profile_ids)
        add_node(NodeKind.TICKET, record.ticket_ref, record.ticket_title, record.extra, acl)
        add_node(NodeKind.CI, record.ci_ref, record.ci_title, {}, acl)
        add_edge(EdgeKind.AFFECTS, f"ticket:{record.ticket_ref}", f"ci:{record.ci_ref}")
        if record.service_ref and record.service_title:
            add_node(NodeKind.SERVICE, record.service_ref, record.service_title, {}, acl)
            add_edge(EdgeKind.DEPENDS_ON, f"ci:{record.ci_ref}", f"service:{record.service_ref}")
        if record.problem_ref and record.problem_title:
            add_node(NodeKind.PROBLEM, record.problem_ref, record.problem_title, {}, acl)
            add_edge(
                EdgeKind.LINKED_TO, f"ticket:{record.ticket_ref}", f"problem:{record.problem_ref}"
            )
        if record.change_ref and record.change_title:
            add_node(NodeKind.CHANGE, record.change_ref, record.change_title, {}, acl)
            if record.problem_ref:
                add_edge(
                    EdgeKind.RESOLVED_BY,
                    f"problem:{record.problem_ref}",
                    f"change:{record.change_ref}",
                )
            add_edge(EdgeKind.MODIFIES, f"change:{record.change_ref}", f"ci:{record.ci_ref}")
        if record.runbook_ref and record.runbook_title:
            add_node(NodeKind.RUNBOOK, record.runbook_ref, record.runbook_title, {}, acl)
            add_edge(
                EdgeKind.HAS_RUNBOOK,
                f"ci:{record.ci_ref}",
                f"runbook:{record.runbook_ref}",
            )

    for left, right, weight in similar:
        add_edge(
            EdgeKind.SIMILAR_TO,
            f"ticket:{left}",
            f"ticket:{right}",
            weight=weight,
        )
    return GraphBatch(nodes=list(nodes.values()), edges=list(edges.values()))
