"""Count visible evidence the asker had no right to see (§4.1).

§4.1 carries a gate whose threshold is a count: *wrong tenant / ACL / expired-version
items in visible evidence: 0*. Until now nothing could compute it. ``Citation`` records
the document, the parent chunk, the source version and the content hash -- and not the
tenant or the groups the item was admitted under, so a post-hoc audit of a citation list
cannot tell a legitimate hit from a disclosure. The count was written as a threshold with
no input behind it.

The input does not have to come from the citation. The harness *loaded* the corpus, so it
knows every document's ACL as ground truth; what it needs to check is whether the pipeline
admits anything outside the asker's. That is the check below, and it is deliberately
independent of the pipeline: the coordinates come from the loader, the retrieved keys come
from the run, and nothing in the retrieval path can influence the comparison.

This is a *visibility* check, not an authorization check. It runs after the fact against a
principal the harness chose, so it can only find leaks the pipeline made -- it cannot
prove the pipeline would refuse a request it never saw, and it does not replace the
per-request pre-filter. Its value is that it is the one check whose expected answer is
known in advance: zero, always.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from servicemind.domain.knowledge import AuthorityLevel


class LeakKind(StrEnum):
    """The three ways an item can be visible to someone who should not see it.

    Kept apart because they fail apart. A wrong-tenant hit is a filter that did not run;
    a group hit is a filter that ran with the wrong groups -- the Phase 7 propagation
    defect, where the approver's or the recovery principal's groups became the run's; an
    expired-version hit is a filter that ran *before* the clock was consulted. Summing
    them into one "unauthorized evidence" count would report the same number for a
    cross-tenant leak and for a stale document.
    """

    WRONG_TENANT = "wrong_tenant"
    UNAUTHORIZED_GROUP = "unauthorized_group"
    EXPIRED_VERSION = "expired_version"


@dataclass(frozen=True)
class EvidenceCoordinates:
    """The ACL a retrieved item was indexed under, taken from the loader's corpus.

    Deliberately not the ACL the *pipeline* reports: reading that back would ask the
    pipeline to grade its own filtering. These values come from the documents the harness
    handed to ``ingest``, before any retrieval ran.
    """

    tenant_id: UUID | None
    group_ids: frozenset[int] = frozenset()
    is_active: bool = True
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    authority_level: AuthorityLevel | None = None

    def admits(
        self,
        *,
        asker_tenant_id: UUID,
        asker_group_ids: frozenset[int],
        query_time: datetime,
    ) -> bool:
        """Whether this item was visible to this asker at this time, per ``KnowledgeACL``.

        The group rule is ``KnowledgeACL.allows``'s, restated here rather than imported
        because the two answer different questions: that one authorises a request, this
        one audits a result. A shared implementation would make a bug in the rule
        invisible to the check that exists to catch it -- both would be wrong together.
        In particular an empty group set is *no group access*, never "unrestricted".
        """
        if self.tenant_id != asker_tenant_id:
            return False
        if not self.is_active:
            return False
        if self.group_ids and not (self.group_ids & asker_group_ids):
            return False
        if self.effective_from is not None and query_time < self.effective_from:
            return False
        if self.effective_to is not None and query_time >= self.effective_to:
            return False
        return True


@dataclass(frozen=True)
class Violation:
    query_id: str
    source_record_id: str
    kind: LeakKind
    detail: str


def classify(
    coordinates: EvidenceCoordinates,
    *,
    asker_tenant_id: UUID,
    asker_group_ids: frozenset[int],
    query_time: datetime,
) -> LeakKind | None:
    """Which kind of leak this is, or ``None`` if it is properly visible.

    Ordered so that the most specific cause wins: a document belonging to another tenant
    is a wrong-tenant leak whatever else is true of it, and reporting it as a group
    problem would send the reader to the wrong filter.
    """
    if coordinates.tenant_id != asker_tenant_id:
        return LeakKind.WRONG_TENANT
    if not coordinates.is_active:
        return LeakKind.EXPIRED_VERSION
    if coordinates.group_ids and not (coordinates.group_ids & asker_group_ids):
        return LeakKind.UNAUTHORIZED_GROUP
    if coordinates.effective_from is not None and query_time < coordinates.effective_from:
        return LeakKind.EXPIRED_VERSION
    if coordinates.effective_to is not None and query_time >= coordinates.effective_to:
        return LeakKind.EXPIRED_VERSION
    return None


def visible_evidence_violations(
    *,
    query_id: str,
    retrieved: Sequence[str],
    coordinates: Mapping[str, EvidenceCoordinates],
    asker_tenant_id: UUID,
    asker_group_ids: frozenset[int],
    query_time: datetime,
) -> list[Violation]:
    """Every retrieved item this asker should not have seen, in ranked order.

    An unknown key is reported as a wrong-tenant violation rather than skipped. It means
    the pipeline returned a document the harness never loaded, which is either a
    cross-tenant read or a key-mapping bug; both are things §4.1 wants to know about, and
    silently ignoring them would turn a missing corpus entry into a clean result.
    """
    violations: list[Violation] = []
    for key in retrieved:
        item = coordinates.get(key)
        if item is None:
            violations.append(
                Violation(
                    query_id=query_id,
                    source_record_id=key,
                    kind=LeakKind.WRONG_TENANT,
                    detail=(
                        "retrieved but not present in the corpus this run loaded, so it "
                        "cannot be shown to belong to the asker's tenant"
                    ),
                )
            )
            continue
        kind = classify(
            item,
            asker_tenant_id=asker_tenant_id,
            asker_group_ids=asker_group_ids,
            query_time=query_time,
        )
        if kind is not None:
            violations.append(
                Violation(
                    query_id=query_id,
                    source_record_id=key,
                    kind=kind,
                    detail=_detail(kind, item, asker_group_ids),
                )
            )
    return violations


def _detail(kind: LeakKind, item: EvidenceCoordinates, asker_group_ids: frozenset[int]) -> str:
    if kind is LeakKind.WRONG_TENANT:
        return f"indexed under tenant {item.tenant_id}, retrieved for another tenant"
    if kind is LeakKind.EXPIRED_VERSION:
        window = f"{item.effective_from} .. {item.effective_to}"
        state = "marked inactive" if not item.is_active else f"effective window {window}"
        return f"not in force at query time ({state})"
    return (
        f"restricted to groups {sorted(item.group_ids)}, asker holds "
        f"{sorted(asker_group_ids) or 'none'}"
    )


def summarize(violations: Iterable[Violation]) -> dict[str, int]:
    """Violation counts by kind, with a total. The §4.1 gate reads ``total``."""
    counts = {kind.value: 0 for kind in LeakKind}
    total = 0
    for violation in violations:
        counts[violation.kind.value] += 1
        total += 1
    return {**counts, "total": total}
