"""The §4.1 zero-count, which had a threshold before it had an input.

The gate reads *wrong tenant / ACL / expired-version items in visible evidence: 0*. A
citation records the document, the parent chunk, the source version and the hash -- and
nothing about who was allowed to see it -- so a list of citations cannot tell a legitimate
hit from a disclosure and the number was uncomputable. These tests pin the check that
supplies it: the coordinates come from the corpus the loader indexed, the keys come from
the run, and the two are compared against the asker that asked.

The count is checked in both directions. A checker that never fires is indistinguishable
from a pipeline that never leaks, and this one exists precisely because the second reading
is the one everybody assumes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from servicemind.domain.knowledge import (
    AuthorityLevel,
    Citation,
    ContextItem,
    CorpusScope,
    KnowledgeACL,
    KnowledgeQuery,
    KnowledgeRAGResult,
    RetrievalHit,
    RetrievalMode,
    RetrievalPrincipal,
)
from servicemind.evaluation.gold import GoldQuery, GoldSet
from servicemind.evaluation.harness import Baseline, evaluate, principal_for
from servicemind.evaluation.leakage import (
    EvidenceCoordinates,
    LeakKind,
    classify,
    summarize,
    visible_evidence_violations,
)

ACME = UUID("11111111-1111-4111-8111-111111111111")
GLOBEX = UUID("22222222-2222-4222-8222-222222222222")
NOW = datetime(2026, 6, 1, tzinfo=UTC)
IN_FORCE = datetime(2026, 1, 1, tzinfo=UTC)


def _at(**overrides: object) -> EvidenceCoordinates:
    """Coordinates for an in-force ACME document, with the axes under test overridden."""
    fields: dict[str, object] = {"tenant_id": ACME, "effective_from": IN_FORCE}
    fields.update(overrides)
    return EvidenceCoordinates(**fields)  # type: ignore[arg-type]


def _check(
    keys: list[str],
    coordinates: dict[str, EvidenceCoordinates],
    *,
    tenant_id: UUID = ACME,
    group_ids: frozenset[int] = frozenset(),
    query_time: datetime = NOW,
):
    return visible_evidence_violations(
        query_id="q1",
        retrieved=keys,
        coordinates=coordinates,
        asker_tenant_id=tenant_id,
        asker_group_ids=group_ids,
        query_time=query_time,
    )


def test_a_document_from_another_tenant_is_a_wrong_tenant_violation() -> None:
    violations = _check(["ours", "theirs"], {"ours": _at(), "theirs": _at(tenant_id=GLOBEX)})
    assert [v.source_record_id for v in violations] == ["theirs"]
    assert violations[0].kind is LeakKind.WRONG_TENANT
    assert str(GLOBEX) in violations[0].detail


def test_a_group_restricted_document_needs_a_group_in_common() -> None:
    """An asker holding no groups must not see a group-restricted document.

    This is the reading ``RetrievalPrincipal.allows_scope`` uses -- an empty coordinate
    means *the resource declares no restriction*, and a principal holding nothing still
    sees unrestricted resources and no restricted ones. An audit that read "the asker
    holds no groups" as "the asker holds every group" would report zero leaks on exactly
    the run where every restricted document was exposed to a caller with no groups.
    """
    coordinates = {"g3": _at(group_ids=frozenset({3}))}
    assert _check(["g3"], coordinates, group_ids=frozenset({3})) == []
    assert _check(["g3"], coordinates, group_ids=frozenset({4})) != []
    assert _check(["g3"], coordinates, group_ids=frozenset()) != []


def test_an_asker_holding_one_of_two_groups_sees_the_document() -> None:
    coordinates = {"g3g4": _at(group_ids=frozenset({3, 4}))}
    assert _check(["g3g4"], coordinates, group_ids=frozenset({4})) == []
    assert _check(["g3g4"], coordinates, group_ids=frozenset({9})) != []


def test_a_document_with_no_group_restriction_is_visible_to_an_asker_with_none() -> None:
    """The other half of the rule: no declared restriction is public within the tenant."""
    assert _check(["open"], {"open": _at()}, group_ids=frozenset()) == []
    assert _check(["open"], {"open": _at()}, group_ids=frozenset({3})) == []


def test_an_inactive_document_is_an_expired_version_violation() -> None:
    """Withdrawn knowledge is not a document that is merely old.

    ``set_document_active(is_active=False)`` is the platform's retirement switch, and a
    run that surfaces a retired procedure is exactly what ACC-02 exists to catch. It is
    sorted apart from a stale effective window because the fixes differ: one is a filter
    that ignored the flag, the other a filter that ignored the clock.
    """
    violations = _check(["retired"], {"retired": _at(is_active=False)})
    assert [v.kind for v in violations] == [LeakKind.EXPIRED_VERSION]
    assert "marked inactive" in violations[0].detail


def test_the_effective_window_is_bounded_on_both_sides() -> None:
    early = _at(effective_from=NOW + timedelta(days=1))
    late = _at(effective_from=IN_FORCE, effective_to=NOW)
    assert [v.kind for v in _check(["a"], {"a": early})] == [LeakKind.EXPIRED_VERSION]
    assert [v.kind for v in _check(["b"], {"b": late})] == [LeakKind.EXPIRED_VERSION]
    # The window is half-open at both ends, matching ``RetrievalPrincipal.allows``:
    # effective_from is the first instant the item is in force, effective_to the first
    # instant it is gone. One second either side of each boundary flips the answer, so an
    # off-by-one here is a leak the gate would otherwise never see.
    assert _check(["start"], {"start": _at(effective_from=NOW)}, query_time=NOW) == []
    assert (
        _check(["start"], {"start": _at(effective_from=NOW + timedelta(seconds=1))}, query_time=NOW)
        != []
    )
    assert (
        _check(["end"], {"end": _at(effective_to=NOW + timedelta(seconds=1))}, query_time=NOW) == []
    )
    assert _check(["end"], {"end": _at(effective_to=NOW)}, query_time=NOW) != []


def test_a_retrieved_key_the_corpus_never_loaded_is_reported_not_skipped() -> None:
    """Skipping it would turn a cross-tenant read into a clean result.

    The harness indexed the corpus itself, so a key outside it is either a read of data
    this tenant never had or a key-mapping bug. Both are things §4.1 asks about, and
    neither is "no violation found".
    """
    violations = _check(["ours", "ghost"], {"ours": _at()})
    assert [v.source_record_id for v in violations] == ["ghost"]
    assert "not present in the corpus" in violations[0].detail


def test_classify_puts_the_tenant_first() -> None:
    """A foreign document is reported as a tenant leak whatever else is true of it.

    Reported the other way round, a cross-tenant exposure would surface as a group
    problem and send the reader to the wrong filter. The order is asserted because it is
    the one property of this function that a reader cannot infer from the branches.
    """
    foreign_and_restricted = _at(tenant_id=GLOBEX, group_ids=frozenset({7}), is_active=False)
    assert (
        classify(
            foreign_and_restricted,
            asker_tenant_id=ACME,
            asker_group_ids=frozenset(),
            query_time=NOW,
        )
        is LeakKind.WRONG_TENANT
    )


def test_classify_and_admits_agree_on_every_axis() -> None:
    """``admits`` and ``classify`` are one rule written twice for one reason.

    ``EvidenceCoordinates.admits`` restates ``RetrievalPrincipal.allows_scope`` rather
    than importing it, because an audit that shares its rule with the thing it audits
    agrees with it even when both are wrong. That independence is worth having only if
    the copy is correct, so the two functions are pinned to each other here.
    """
    askers = [
        {"asker_tenant_id": ACME, "asker_group_ids": frozenset()},
        {"asker_tenant_id": ACME, "asker_group_ids": frozenset({3})},
        {"asker_tenant_id": GLOBEX, "asker_group_ids": frozenset({3})},
    ]
    items = [
        _at(),
        _at(group_ids=frozenset({3})),
        _at(group_ids=frozenset({4})),
        _at(tenant_id=GLOBEX),
        _at(is_active=False),
        _at(effective_from=NOW + timedelta(days=1)),
        _at(effective_to=NOW),
    ]
    for item in items:
        for asker in askers:
            verdict = classify(item, query_time=NOW, **asker)  # type: ignore[arg-type]
            assert (verdict is None) is item.admits(query_time=NOW, **asker)  # type: ignore[arg-type]


def test_summary_separates_the_kinds_and_totals_them() -> None:
    violations = _check(
        ["theirs", "retired", "g3"],
        {
            "theirs": _at(tenant_id=GLOBEX),
            "retired": _at(is_active=False),
            "g3": _at(group_ids=frozenset({3})),
        },
    )
    assert summarize(violations) == {
        "wrong_tenant": 1,
        "unauthorized_group": 1,
        "expired_version": 1,
        "total": 3,
    }


def _result(keys: list[str], query: str) -> KnowledgeRAGResult:
    items: list[ContextItem] = []
    for key in keys:
        chunk_id = uuid4()
        hit = RetrievalHit(
            child_chunk_id=chunk_id,
            parent_chunk_id=chunk_id,
            document_id=uuid4(),
            child_content=f"content {key}",
            score=1.0,
            title=key,
            source="leakage-fixture",
            source_uri=f"sop://phase4/{key}",
            source_record_id=key,
            source_version="v1",
            license="project-owned",
            authority_level=AuthorityLevel.INTERNAL_KNOWLEDGE,
            synthetic=False,
            content_hash="0" * 64,
            acl=KnowledgeACL(corpus_scope=CorpusScope.TENANT, tenant_id=ACME),
        )
        items.append(
            ContextItem(
                parent_content=f"parent {key}",
                hit=hit,
                citation=Citation.from_hit(hit),
                token_count=1,
            )
        )
    return KnowledgeRAGResult(
        query=KnowledgeQuery(raw_query=query, normalized_query=query),
        items=items,
        retrieval_mode="test",
        candidate_count=len(keys),
        latency_ms=1.0,
    )


class _RankingProvider:
    """A provider whose ranking is exactly the keys it was constructed with.

    Deliberately not ACL-aware: it stands in for a pipeline that returned too much, which
    is the only way to check that the harness notices.
    """

    def __init__(self, keys: list[str]) -> None:
        self.keys = keys

    async def retrieve(self, *, principal, query, mode, run_rerank, final_k):
        del principal, mode, run_rerank, final_k
        return _result(self.keys, query)


def _one_query_gold() -> GoldSet:
    return GoldSet(name="one", queries=[GoldQuery(id="q", query="how do I rebind")])


@pytest.mark.asyncio
async def test_a_run_without_coordinates_records_no_violations() -> None:
    """Unknown and clean must not render the same way.

    With no coordinates nothing is known about what a returned key was allowed to be, so
    the list stays empty -- which is *not* the same statement as "none were found", and
    the docstring on ``evaluate`` says so. The §4.1 count is only meaningful when the
    caller indexed a corpus and can say what it loaded; that is why the scripts that
    index one pass coordinates and a run that does not gets an empty list rather than a
    zero it did not earn.
    """
    principal = RetrievalPrincipal(tenant_id=ACME, user_id="u", query_time=NOW)
    report = await evaluate(_RankingProvider(["g7"]), _one_query_gold(), principal)
    assert report.metrics("dense").visible_evidence_violations() == []


@pytest.mark.asyncio
async def test_the_harness_counts_a_leak_the_pipeline_should_not_have_returned() -> None:
    """The direction that makes the check worth having.

    The harness is handed a ranking it does not control and coordinates it does; the
    violation has to land on the outcome, per baseline, because a report-level total
    would hide which of the four filters admitted it.
    """
    principal = RetrievalPrincipal(tenant_id=ACME, user_id="u", query_time=NOW)
    report = await evaluate(
        _RankingProvider(["g7"]),
        _one_query_gold(),
        principal,
        baselines=(Baseline(name="dense", mode=RetrievalMode.DENSE, run_rerank=False),),
        coordinates={"g7": _at(group_ids=frozenset({7}))},
    )
    violations = report.metrics("dense").visible_evidence_violations()
    assert [v.kind for v in violations] == [LeakKind.UNAUTHORIZED_GROUP]
    assert violations[0].query_id == "q"
    assert violations[0].source_record_id == "g7"


@pytest.mark.asyncio
async def test_the_same_ranking_is_clean_for_the_asker_who_holds_the_group() -> None:
    """The check must be about the asker, not about the document.

    A checker that flagged every restricted document would report the same count on a run
    that served the right people and a run that served the wrong ones, which is the
    failure mode this whole module exists to avoid.
    """
    principal = RetrievalPrincipal(
        tenant_id=ACME, user_id="u", group_ids=frozenset({7}), query_time=NOW
    )
    report = await evaluate(
        _RankingProvider(["g7"]),
        _one_query_gold(),
        principal,
        baselines=(Baseline(name="dense", mode=RetrievalMode.DENSE, run_rerank=False),),
        coordinates={"g7": _at(group_ids=frozenset({7}))},
    )
    assert report.metrics("dense").visible_evidence_violations() == []


@pytest.mark.asyncio
async def test_every_baseline_is_audited_separately() -> None:
    """Four filters, one corpus, one answer each. A total would average them into nothing.

    The baselines differ only in how they rank -- the ACL pre-filter is applied
    identically in every mode (``RetrievalMode``'s own docstring). A run where one mode
    leaks and three do not is a fact about that mode, and it has to survive to the
    report.
    """
    principal = RetrievalPrincipal(tenant_id=ACME, user_id="u", query_time=NOW)
    report = await evaluate(
        _RankingProvider(["g7"]),
        _one_query_gold(),
        principal,
        baselines=(
            Baseline(name="dense", mode=RetrievalMode.DENSE, run_rerank=False),
            Baseline(name="bm25", mode=RetrievalMode.BM25, run_rerank=False),
            Baseline(name="hybrid", mode=RetrievalMode.HYBRID, run_rerank=False),
            Baseline(name="hybrid_rerank", mode=RetrievalMode.HYBRID, run_rerank=True),
        ),
        coordinates={"g7": _at(group_ids=frozenset({7}))},
    )
    for baseline in ("dense", "bm25", "hybrid", "hybrid_rerank"):
        assert len(report.metrics(baseline).visible_evidence_violations()) == 1, baseline


def test_a_query_naming_its_asker_overrides_the_run_principal() -> None:
    """One query text, two callers: one answers, the other is a disclosure.

    §3.2 asks for a wrong-tenant stratum and §4.1 for a zero-count on wrong-ACL evidence,
    and neither is expressible with a single principal for the whole run. A query that
    names no asker must still inherit the run's principal unchanged -- including by
    identity, so that the common path costs nothing.
    """
    default = RetrievalPrincipal(tenant_id=ACME, user_id="u", group_ids=frozenset({1}))
    assert principal_for(GoldQuery(id="a", query="x"), default) is default

    override = principal_for(
        GoldQuery(id="b", query="x", asker_tenant_id=GLOBEX, asker_group_ids=frozenset({3})),
        default,
    )
    assert override.tenant_id == GLOBEX
    assert override.group_ids == frozenset({3})
    assert override.user_id == default.user_id

    tenant_only = principal_for(GoldQuery(id="c", query="x", asker_tenant_id=GLOBEX), default)
    assert tenant_only.entity_ids == default.entity_ids

    groups_only = principal_for(
        GoldQuery(id="d", query="x", asker_group_ids=frozenset({5})), default
    )
    assert groups_only.tenant_id == ACME
    assert groups_only.group_ids == frozenset({5})
