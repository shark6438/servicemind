"""What the release-set schema refuses, and why each refusal is load-bearing.

``gold.py`` used to describe a query with a binary ``relevant`` list, and two things
followed from that. A judgment could not say *which* tenant, group or version it was made
under, so the §4.1 gate whose threshold is a count of "wrong tenant/ACL/expired-version
items in visible evidence" had no input it could be computed from. And nothing in a set
recorded whether a human had judged it, so "this gate does not apply here" had to be
asserted in the harness -- a boolean in code, which can only ever report what it was
written to report.

Both are now properties of the data, and these tests pin the two directions that matter:
a set that meets §3.2/§3.3 is accepted, and a set that merely *looks* annotated -- or that
carries two views of the same judgment that disagree -- is refused rather than scored.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from servicemind.evaluation.gold import (
    ANSWERING_GRADE,
    AnnotationProvenance,
    GoldQuery,
    GoldSet,
    JudgedItem,
    LabelTier,
    Nugget,
    NuggetSupport,
    QueryCategory,
    RefusalReason,
    Split,
)

TENANT = UUID("11111111-1111-4111-8111-111111111111")


def _judged(chunk: str, grade: int, **conditions: object) -> JudgedItem:
    """A judgment on one parent chunk, of the document that chunk belongs to.

    The two identifiers are deliberately different -- ``runbook#1`` is a chunk of
    ``runbook`` -- because ``relevant`` names documents and ``graded`` names chunks, and
    the validator exists to catch a set where those two views disagree.
    """
    conditions.setdefault("source_record_id", chunk.split("#", 1)[0])
    return JudgedItem(parent_chunk_id=chunk, grade=grade, **conditions)


def _conforming_set() -> GoldSet:
    """The smallest set that satisfies every stratum §3.2 asks for."""
    return GoldSet(
        name="conforming",
        provenance=AnnotationProvenance(
            label_tier=LabelTier.DOUBLE_ANNOTATOR,
            annotator_count=2,
            cohen_kappa=0.84,
            double_annotated_fraction=0.2,
            guideline_version="v1",
        ),
        queries=[
            GoldQuery(
                id="dev-single",
                query="how do I rebind an mfa device",
                split=Split.DEVELOPMENT,
                category=QueryCategory.SINGLE_EVIDENCE,
                relevant=["runbook"],
                graded=[_judged("runbook#0", 4)],
            ),
            GoldQuery(
                id="test-multi",
                query="which change approved the vpn rewrite",
                category=QueryCategory.MULTI_EVIDENCE,
                relevant=["change-record"],
                graded=[_judged("change-record#0", 3)],
            ),
            GoldQuery(
                id="test-hard-negative",
                query="voice is failing",
                category=QueryCategory.HARD_NEGATIVE,
                graded=[_judged("lookalike-ticket#0", 0)],
            ),
            GoldQuery(
                id="test-refuse-secrets",
                query="paste the production database password",
                unanswerable=True,
                category=QueryCategory.UNANSWERABLE,
                refusal_reason=RefusalReason.MUST_REFUSE_ACCESS,
            ),
            GoldQuery(
                id="test-insufficient",
                query="which printer model ships next year",
                unanswerable=True,
                category=QueryCategory.UNANSWERABLE,
                refusal_reason=RefusalReason.INSUFFICIENT_EVIDENCE,
            ),
            GoldQuery(
                id="test-version-conflict",
                query="is the four-eyes rule still in force",
                unanswerable=True,
                category=QueryCategory.UNANSWERABLE,
                refusal_reason=RefusalReason.VERSION_CONFLICT,
            ),
        ],
    )


def test_a_set_that_meets_the_protocol_may_be_judged_by_the_release_thresholds() -> None:
    assert _conforming_set().release_gate_blockers == []


def test_the_proxy_selection_is_blocked_by_what_it_does_not_record() -> None:
    """The manifest the committed TechQA numbers come from, judged by its own contents.

    It reads as a set of 400 queries with binary relevance and a tier the source supplied,
    so the blockers are the clauses it cannot state: no grades, no hard negatives, one
    kind of unanswerable, no development split, no human signoff. The point of stating
    them here is that the harness no longer has to -- and that the same function would
    stop blocking a set that supplied them.
    """
    from servicemind.evaluation.gold import ReleaseSetShape, release_gate_blockers

    proxy = ReleaseSetShape(
        provenance=AnnotationProvenance(
            label_tier=LabelTier.SILVER,
            notes="nvidia/TechQA-RAG-Eval ships relevance as source metadata",
        )
    )
    blockers = release_gate_blockers(proxy)
    joined = " ".join(blockers)
    assert "tier silver" in joined
    assert "0--4" in joined
    assert "hard-negative" in joined
    assert len([b for b in blockers if "must-refuse query of kind" in b]) == 3
    assert "development split" in joined
    assert len(blockers) == 7

    # And the direction that matters: the same manifest, once it records what §3 asks
    # for, stops being blocked -- with no code change anywhere.
    unblocked = ReleaseSetShape(
        provenance=AnnotationProvenance(
            label_tier=LabelTier.DOUBLE_ANNOTATOR, annotator_count=2, cohen_kappa=0.83
        ),
        graded_queries=400,
        hard_negative_queries=80,
        refusal_reasons=frozenset(RefusalReason),
        development_queries=120,
    )
    assert release_gate_blockers(unblocked) == []


def test_the_committed_phase4_gold_set_is_not_a_release_set() -> None:
    """The set the current numbers come from must not be able to unlock §4.1.

    This is the property the TechQA harness previously held by hardcoding
    ``applicable: false``. Holding it in the data means the same set still reports the
    same thing after the harness stops asserting it -- and, more usefully, that a set
    which *would* qualify says so without anyone editing code.
    """
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "evaluation" / "gold" / "gold_set.v1.json"
    committed = GoldSet.model_validate(json.loads(root.read_text(encoding="utf-8")))

    problems = committed.release_gate_blockers
    assert problems, "a silver, ungraded, hard-negative-free set was judged releasable"
    joined = " ".join(problems)
    assert "annotation provenance" in joined
    assert "0--4" in joined
    assert "hard-negative" in joined
    assert "version_conflict" in joined


def test_a_single_annotator_is_not_signoff() -> None:
    """One expert is §3.3's *first* pass, not its signoff.

    The tier exists so that a set labelled by one person is not silently as good as one
    that cleared kappa; ``label_tier`` alone is not enough, which is why the check reads
    the count and the agreement as well.
    """
    alone = AnnotationProvenance(
        label_tier=LabelTier.DOUBLE_ANNOTATOR, annotator_count=1, cohen_kappa=0.9
    )
    assert not alone.meets_tenant_protocol

    low_agreement = AnnotationProvenance(
        label_tier=LabelTier.DOUBLE_ANNOTATOR, annotator_count=2, cohen_kappa=0.61
    )
    assert not low_agreement.meets_tenant_protocol, (
        "§3.3 revises the guideline below kappa 0.80 rather than accepting the labels"
    )

    silver = AnnotationProvenance(label_tier=LabelTier.SILVER, annotator_count=2, cohen_kappa=0.95)
    assert not silver.meets_tenant_protocol, "a source-provided label is not a judgment"


def test_an_unanswerable_query_must_say_which_kind_it_is() -> None:
    with pytest.raises(ValueError, match="must say which of"):
        GoldQuery(
            id="mystery",
            query="something we declined",
            unanswerable=True,
            category=QueryCategory.UNANSWERABLE,
        )
    with pytest.raises(ValueError, match="cannot carry a refusal reason"):
        GoldQuery(id="fine", query="answerable", refusal_reason=RefusalReason.VERSION_CONFLICT)


def test_the_two_views_of_one_judgment_may_not_disagree() -> None:
    """``relevant`` and ``graded`` are both reachable, so they must agree.

    A consumer that reads only ``relevant`` and one that reads only ``graded`` would
    otherwise score the same set differently, silently, in whichever direction the last
    editor of the file happened to leave it.
    """
    with pytest.raises(ValueError, match="do not support"):
        GoldQuery(
            id="contradiction",
            query="which runbook",
            relevant=["runbook", "other"],
            graded=[_judged("runbook", 4)],
        )

    # grade 1 is background, not an answer, so it must not appear in `relevant`.
    with pytest.raises(ValueError, match="do not support"):
        GoldQuery(
            id="background-only",
            query="which runbook",
            relevant=["runbook"],
            graded=[_judged("runbook", 1)],
        )

    agreeing = GoldQuery(
        id="agreeing",
        query="which runbook",
        relevant=["runbook"],
        graded=[_judged("runbook", ANSWERING_GRADE), _judged("other", ANSWERING_GRADE - 1)],
    )
    assert agreeing.grades == {"runbook": 2, "other": 1}


def test_a_judgment_knows_which_callers_it_was_made_for() -> None:
    """Empty groups mean no group access, never unrestricted access.

    The distinction the Phase 7 permission work had to establish on the retrieval path,
    applied to the judgments: a document restricted to group 3 is not available to a
    caller with no groups, and treating the empty set as "unrestricted" is how a
    group-restricted corpus becomes silently visible to everyone.
    """
    restricted = _judged("g3-only", 4, group_ids=frozenset({3}))
    assert restricted.applies_to(group_ids=frozenset({3}))
    assert restricted.applies_to(group_ids=frozenset({3, 4}))
    assert not restricted.applies_to(group_ids=frozenset({4}))
    assert not restricted.applies_to(group_ids=frozenset())

    tenant_bound = _judged("acme-only", 4, tenant_id=TENANT)
    assert tenant_bound.applies_to(tenant_id=TENANT)
    assert not tenant_bound.applies_to(tenant_id=uuid4())

    # A judgment about the corpus, not about a caller, travels with everyone.
    assert _judged("public", 4).applies_to(tenant_id=uuid4(), group_ids=frozenset())


def test_a_nugget_records_where_its_fact_is_stated() -> None:
    """Nugget completeness is a second question from recall, so it needs its own data.

    A ranked answer can cite the right document and still omit the fact the answer
    needed; nothing in a `relevant` list can express that, which is why §3.4 asks for
    nuggets beside the retrieval metrics.
    """
    nugget = Nugget(
        id="n1",
        text="rebinding requires re-verifying the caller's identity first",
        supporting=[NuggetSupport(parent_chunk_id="runbook#0", span="Step 1: verify the caller")],
    )
    gold = GoldQuery(
        id="with-nuggets",
        query="how do I rebind",
        relevant=["runbook"],
        graded=[_judged("runbook", 4)],
        nuggets=[nugget, Nugget(id="n2", text="detail", required=False)],
    )
    assert [item.id for item in gold.required_nuggets] == ["n1"]
    assert gold.nuggets[0].supporting[0].parent_chunk_id == "runbook#0"
