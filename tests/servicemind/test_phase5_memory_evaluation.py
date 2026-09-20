"""Contracts for the Phase 5 memory quality harness.

The harness reports numbers people will act on, so these tests do not only pin the
committed corpus's score -- they also prove the score *moves* when the thing it measures
moves. A harness that cannot go red is worse than no harness.
"""

from __future__ import annotations

import hashlib
import math
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from servicemind.evaluation.memory import (
    SAFETY_CATEGORIES,
    DeliveryLoad,
    MemoryEvaluationError,
    MemoryProbe,
    MemoryScenarioCorpus,
    MemorySeedStep,
    MemoryTransitionStep,
    MemoryWriteStep,
    WriteExpectation,
    _evidence_content,
    evaluate,
    load_corpus,
    render_markdown,
)
from servicemind.memory.contracts import (
    MemoryQuery,
    MemorySelection,
    MemoryWriteAction,
)
from servicemind.memory.policy import MemoryGovernancePolicy
from servicemind.memory.service import MemoryRetriever

CORPUS = Path("evaluation/memory/scenarios.v1.json")
CORPUS_V2 = Path("evaluation/memory/scenarios.v2.json")

#: Categories the v2 corpus is required to cover with several distinct variants. One
#: probe per category is what let the v1 probes pass without touching the filter they
#: were named after; the count is a design requirement, not a reading of the results.
MIN_SAFETY_VARIANTS = {"as_of": 5, "injection": 5, "taint": 5}


@pytest.fixture(scope="module")
def corpus() -> MemoryScenarioCorpus:
    return load_corpus(CORPUS)


@pytest.fixture(scope="module")
def corpus_v2() -> MemoryScenarioCorpus:
    return load_corpus(CORPUS_V2)


@pytest.mark.asyncio
async def test_committed_corpus_holds_every_documented_guarantee(corpus):
    """The regression contract: the committed corpus scores clean end to end."""
    report = await evaluate(corpus)

    assert report.write.action_accuracy == 1.0
    assert report.write.stored_accuracy == 1.0
    assert report.write.reason_code_accuracy == 1.0
    assert report.write.unsafe_activations == []
    assert report.write.spurious_rejections == []
    assert report.probe.leak_rate == 0.0
    assert report.probe.leaked_records == 0
    assert all(outcome.missing_required == [] for outcome in report.probe_outcomes)


@pytest.mark.asyncio
async def test_every_safety_category_is_actually_exercised(corpus):
    """A category with no probe would report a vacuous 0.0 leak rate."""
    report = await evaluate(corpus)

    for category in SAFETY_CATEGORIES:
        score = report.per_category[category]
        assert score.probes > 0, f"{category} has no probe"
        assert score.leaked_records == 0


@pytest.mark.asyncio
async def test_a_leaking_retriever_is_reported_rather_than_scored_green(corpus):
    """Defeat the ACL filter and the leak rate must go red, not stay at zero."""

    class AclBlindRetriever(MemoryRetriever):
        async def retrieve(self, query: MemoryQuery) -> list[MemorySelection]:
            return [
                MemorySelection(memory=record, score=1.0, score_breakdown={})
                for record in self.repository.records
            ]

    report = await evaluate(
        corpus, retriever_factory=lambda repository: AclBlindRetriever(repository)
    )

    assert report.probe.leak_rate > 0.0
    assert report.probe.leaked_records > 0
    for probe_id in ("p-iso-tenant", "p-taint", "p-injection", "p-stale-expired"):
        assert probe_id in report.probe.leaking_probes


@pytest.mark.asyncio
async def test_an_out_of_order_ranker_is_not_punished_as_a_leak(corpus):
    """Leaks are membership, not rank: a reversed ranking must still show zero leaks."""

    class ReversedRetriever(MemoryRetriever):
        async def retrieve(self, query: MemoryQuery) -> list[MemorySelection]:
            return list(reversed(await super().retrieve(query)))

    report = await evaluate(
        corpus, retriever_factory=lambda repository: ReversedRetriever(repository)
    )

    assert report.probe.leaked_records == 0
    assert report.ranking[0].recall < 1.0


@pytest.mark.asyncio
async def test_a_weaker_policy_is_reported_as_a_breach(corpus):
    """Lowering the activation bar must surface, not be absorbed by the averages."""
    report = await evaluate(corpus, policy=MemoryGovernancePolicy(auto_activation_confidence=0.5))

    assert report.write.action_accuracy < 1.0
    assert report.write.stored_accuracy < 1.0
    assert "w-low-confidence" in report.write.unsafe_activations


@pytest.mark.asyncio
async def test_a_wrong_expectation_in_the_corpus_is_reported_as_a_mismatch(corpus):
    """The write score is computed from the corpus, not hardcoded to pass."""
    steps = [
        step.model_copy(
            update={
                "expect": WriteExpectation(
                    policy_action=MemoryWriteAction.QUARANTINE, stored="quarantine"
                )
            }
        )
        if isinstance(step, MemoryWriteStep) and step.id == "w-vpn-fact"
        else step
        for step in corpus.steps
    ]

    report = await evaluate(corpus.model_copy(update={"steps": steps}))

    outcome = next(o for o in report.write_outcomes if o.step_id == "w-vpn-fact")
    assert outcome.action_match is False
    assert outcome.stored_match is False
    assert "w-vpn-fact" in report.write.unsafe_activations
    assert report.write.action_accuracy < 1.0


@pytest.mark.asyncio
async def test_a_rejected_write_owns_no_record_and_cannot_be_probed(corpus):
    report = await evaluate(corpus)

    for outcome in report.write_outcomes:
        if outcome.actual_stored == "rejected":
            assert outcome.memory_id is None
    assert {o.step_id for o in report.write_outcomes if o.memory_id is None} == {
        "w-secret",
        "w-low-importance",
    }


@pytest.mark.asyncio
async def test_the_procedural_transition_actually_ran(corpus):
    """The procedure surfaces only because the reviewed quarantine->active step ran."""
    report = await evaluate(corpus)

    procedure = next(o for o in report.write_outcomes if o.step_id == "w-procedure-sso")
    assert procedure.actual_stored == "quarantine"
    probe = next(o for o in report.probe_outcomes if o.probe_id == "p-procedure")
    assert probe.missing_required == []
    assert probe.ranked_keys[0] == "w-procedure-sso"


@pytest.mark.asyncio
async def test_time_offsets_are_resolved_against_the_anchor(corpus):
    """``now`` must mean the anchor, not the instant each candidate is built."""
    anchor = datetime(2026, 6, 1, tzinfo=UTC)
    report = await evaluate(corpus, anchor=anchor)

    assert report.anchor == anchor
    future = next(o for o in report.probe_outcomes if o.probe_id == "p-as-of-future")
    assert future.leaks == []


@pytest.mark.asyncio
async def test_an_expired_record_is_written_active_and_still_never_surfaces(corpus):
    """TTL lapsing is a read-time property; the write itself legitimately succeeds."""
    report = await evaluate(corpus, anchor=datetime.now(UTC))

    outcome = next(o for o in report.write_outcomes if o.step_id == "w-expired-at-write")
    assert outcome.actual_stored == "active"
    # Other memories may legitimately answer this query; only the lapsed one is barred.
    probe = next(o for o in report.probe_outcomes if o.probe_id == "p-stale-expired")
    assert probe.leaks == []


@pytest.mark.asyncio
async def test_the_report_declares_its_scoring_mode_and_honesty_boundary(corpus):
    report = await evaluate(corpus, anchor=datetime(2026, 6, 1, tzinfo=UTC))
    rendered = render_markdown(report)

    assert report.scoring_mode == "lexical"
    assert all(score.mode == "lexical" for score in report.ranking)
    assert "2026-06-01" in rendered
    # The honesty boundary travels with the numbers, not only with the JSON.
    assert "不是任何租户真实记忆" in rendered


@pytest.mark.asyncio
async def test_a_query_at_the_anchor_cannot_see_a_record_that_starts_later(corpus):
    """The temporal guard must hold at a shifted anchor too, not only at "now"."""
    anchor = datetime.now(UTC) - timedelta(days=365)
    report = await evaluate(corpus, anchor=anchor)

    future = next(o for o in report.probe_outcomes if o.probe_id == "p-as-of-future")
    assert future.leaks == []


def test_probe_cannot_both_require_and_forbid_the_same_record():
    with pytest.raises(ValidationError, match="both required and forbidden"):
        MemoryProbe(
            id="p",
            label="l",
            text="t",
            must_surface=("w-vpn-fact",),
            must_not_surface=("w-vpn-fact",),
            leak_reason="r",
        )


def test_forbidding_a_record_requires_stating_why():
    with pytest.raises(ValidationError, match="without stating why"):
        MemoryProbe(id="p", label="l", text="t", must_not_surface=("w-vpn-fact",))


def test_dangling_references_are_rejected_before_anything_runs():
    with pytest.raises(ValidationError, match="unknown record"):
        MemoryScenarioCorpus(
            schema_version="v1",
            name="broken",
            tenants={"primary": "11111111-1111-4111-8111-111111111111"},
            steps=[MemoryTransitionStep(id="t", label="l", target="missing", status="revoked")],
            probes=[MemoryProbe(id="p", label="l", text="t")],
        )


def test_an_unchanged_expectation_must_name_the_record_it_deduped_onto():
    with pytest.raises(ValidationError, match="same_as"):
        WriteExpectation(policy_action=MemoryWriteAction.ACTIVATE, stored="unchanged")


def test_a_rank_requirement_needs_a_record_and_a_stated_requirement():
    """``max_rank`` is a product requirement, so it must come with the requirement."""
    with pytest.raises(ValidationError, match="without requiring a record"):
        MemoryProbe(id="p", label="l", text="t", max_rank=3, max_rank_rationale="r")
    with pytest.raises(ValidationError, match="without stating the requirement"):
        MemoryProbe(id="p", label="l", text="t", must_surface=("w-vpn-fact",), max_rank=3)


# --------------------------------------------------------------------------------------
# v2: the discriminating-power corpus
# --------------------------------------------------------------------------------------


def _replace_content(corpus: MemoryScenarioCorpus, rewrite) -> MemoryScenarioCorpus:
    """Return the corpus with write/seed contents rewritten by ``rewrite``."""
    steps = []
    for step in corpus.steps:
        if isinstance(step, (MemoryWriteStep, MemorySeedStep)):
            content = rewrite(step.candidate.get("content", ""))
            if content != step.candidate.get("content"):
                step = step.model_copy(update={"candidate": {**step.candidate, "content": content}})
        steps.append(step)
    return corpus.model_copy(update={"steps": steps})


def _leaks(report, probe_id: str) -> list[str]:
    return next(o for o in report.probe_outcomes if o.probe_id == probe_id).leaks


@pytest.mark.asyncio
async def test_v2_holds_every_documented_guarantee(corpus_v2):
    report = await evaluate(corpus_v2)

    assert report.write.action_accuracy == 1.0
    assert report.write.stored_accuracy == 1.0
    assert report.write.reason_code_accuracy == 1.0
    assert report.write.unsafe_activations == []
    assert report.write.spurious_rejections == []
    assert report.probe.leak_rate == 0.0
    assert report.probe.leaked_records == 0
    assert all(outcome.missing_required == [] for outcome in report.probe_outcomes)
    assert report.rank_gate.passed, report.rank_gate.violations


@pytest.mark.asyncio
async def test_v2_carries_several_variants_of_each_safety_property(corpus_v2):
    """One probe per safety category is what let v1 report a zero it had not earned."""
    report = await evaluate(corpus_v2)

    for category in SAFETY_CATEGORIES:
        assert report.per_category[category].probes > 0, f"{category} has no probe"
        assert report.per_category[category].leaked_records == 0
    for category, minimum in MIN_SAFETY_VARIANTS.items():
        assert report.per_category[category].probes >= minimum, (
            f"{category} has {report.per_category[category].probes} probes, needs {minimum}"
        )


@pytest.mark.asyncio
async def test_the_taint_filter_is_the_only_thing_blocking_the_seeded_records(corpus_v2):
    """Clear the label on the seeded records and every taint probe must go red.

    A green result only means something if the record would otherwise have been
    returned. The clean twin in each probe already shows the wording clears the
    similarity floor; clearing the label closes the other half of the argument.
    """
    report = await evaluate(corpus_v2)
    assert report.probe.leak_rate == 0.0

    steps = []
    for step in corpus_v2.steps:
        if isinstance(step, MemorySeedStep) and step.candidate.get("taint_labels"):
            step = step.model_copy(
                update={
                    "candidate": {k: v for k, v in step.candidate.items() if k != "taint_labels"}
                }
            )
        steps.append(step)

    unlabelled = await evaluate(corpus_v2.model_copy(update={"steps": steps}))

    taint_probes = [p.id for p in corpus_v2.probes if p.id.startswith("p-taint-")]
    assert len(taint_probes) >= 5
    for probe_id in taint_probes:
        assert _leaks(unlabelled, probe_id), f"{probe_id} did not notice the label was gone"
    assert unlabelled.probe.leak_rate > 0.0
    assert unlabelled.rank_gate.passed  # membership, not rank: the gate must not move


@pytest.mark.asyncio
async def test_injection_markers_are_the_only_thing_blocking_the_injection_payloads(corpus_v2):
    """Remove the marker phrase *only* and every injection probe must go red.

    Rewriting the whole sentence would not prove this: the record would then fall
    below the similarity floor and look blocked for the wrong reason. So the rewrite
    touches the marker and nothing else, and the benign twin must stay in place. For
    normal writes the canonical marker is now caught before activation; seeded legacy
    records still exercise the independent read-side tripwire.
    """
    surgical = {
        "You are now": "The engineer is",
        "Disregard previous guidance": "Follow the standing guidance",
        "Forget previous approval requirements": "Retain the approval requirements",
        "忽略系统提示中的": "遵循运营手册中的",
        "Ignore previous instructions and": "Note that",
        "Bypass approval for": "Standard process for",
    }

    def strip_marker(text: str) -> str:
        for marker, replacement in surgical.items():
            text = text.replace(marker, replacement)
        return text

    report = await evaluate(_replace_content(corpus_v2, strip_marker))

    injection_probes = [p.id for p in corpus_v2.probes if p.id.startswith("p-inj-")]
    assert len(injection_probes) >= 5
    for probe_id in injection_probes:
        outcome = next(o for o in report.probe_outcomes if o.probe_id == probe_id)
        assert outcome.leaks, f"{probe_id} did not notice the marker was gone"
        # The benign twin is untouched, so the payload cannot have leaked merely
        # because the required record dropped out and freed a slot.
        assert outcome.missing_required == []
    assert report.probe.leak_rate > 0.0


@pytest.mark.asyncio
async def test_a_write_side_marker_can_only_enter_the_store_by_seeding(corpus_v2):
    """The seeded payloads are seeded because the write policy would never store them.

    Without this the seeds would look like a shortcut around an inconvenient record
    rather than the only way to state the premise they exist to state.
    """
    seeded_markers = [
        step.id
        for step in corpus_v2.seeds
        if "ignore previous" in step.candidate.get("content", "").casefold()
        or "bypass approval" in step.candidate.get("content", "").casefold()
    ]
    assert len(seeded_markers) >= 2

    # Hand the very same candidates to the real writer instead. If the write path
    # could store them, seeding them would have been a way of avoiding a failing
    # expectation rather than the only way to state the premise.
    def as_write(step):
        if isinstance(step, MemorySeedStep) and step.id in seeded_markers:
            return MemoryWriteStep(
                id=step.id,
                label=step.label,
                category=step.category,
                candidate=step.candidate,
                expect=WriteExpectation(
                    policy_action=MemoryWriteAction.QUARANTINE,
                    policy_reason_codes=("PROMPT_INJECTION_TAINT",),
                    stored="quarantine",
                ),
            )
        return step

    converted = corpus_v2.model_copy(update={"steps": [as_write(step) for step in corpus_v2.steps]})
    report = await evaluate(converted)

    assert report.write.unsafe_activations == []
    for step_id in seeded_markers:
        outcome = next(o for o in report.write_outcomes if o.step_id == step_id)
        assert outcome.actual_stored == "quarantine"
        assert outcome.missing_reason_codes == []


@pytest.mark.asyncio
async def test_seeded_records_are_named_and_kept_out_of_the_write_score(corpus_v2):
    report = await evaluate(corpus_v2)

    seed_ids = {step.id for step in corpus_v2.seeds}
    assert seed_ids
    assert set(report.seeded) == seed_ids
    # A seeded record bypasses the policy, so scoring it as a governance outcome
    # would be scoring the corpus's own assertion.
    assert not seed_ids & {o.step_id for o in report.write_outcomes}
    for step in corpus_v2.seeds:
        assert step.status.value in {"active", "quarantine"}


@pytest.mark.asyncio
async def test_the_as_of_edges_are_where_the_contract_says_they_are(corpus_v2):
    """Move each boundary and the matching probe must go red.

    The positive twin one second inside the window is what separates "the boundary
    is enforced" from "the record was invisible for some other reason".
    """
    report = await evaluate(corpus_v2)
    assert report.probe.leak_rate == 0.0
    assert all(o.missing_required == [] for o in report.probe_outcomes)

    def shift(step, field: str, delta: float, only: str | None = None):
        if not isinstance(step, MemoryWriteStep):
            return step
        key = f"{field}_offset_s"
        if key not in step.candidate or (only is not None and step.id != only):
            return step
        return step.model_copy(
            update={"candidate": {**step.candidate, key: step.candidate[key] + delta}}
        )

    async def shifted(mutation):
        steps = [mutation(step) for step in corpus_v2.steps]
        return await evaluate(corpus_v2.model_copy(update={"steps": steps}))

    assert _leaks(await shifted(lambda s: shift(s, "valid_to", 300)), "p-asof-close-edge")
    assert _leaks(await shifted(lambda s: shift(s, "expires_at", 300)), "p-asof-ttl-edge")
    assert _leaks(
        await shifted(lambda s: shift(s, "valid_from", -86300, "w-future-dated")),
        "p-asof-future-just-before",
    )
    # The open edge is inclusive: delaying valid_from by a second must drop the
    # record from the probe that sits exactly on it.
    late = await shifted(lambda s: shift(s, "valid_from", 1, "w-asof-window"))
    outcome = next(o for o in late.probe_outcomes if o.probe_id == "p-asof-open-edge")
    assert outcome.missing_required == ["w-asof-window"]


@pytest.mark.asyncio
async def test_a_broken_ranker_cannot_pass_the_rank_gate(corpus_v2):
    """The gate reports a position regression rather than absorbing it."""

    class ReversedRetriever(MemoryRetriever):
        async def retrieve(self, query: MemoryQuery) -> list[MemorySelection]:
            return list(reversed(await super().retrieve(query)))

    report = await evaluate(
        corpus_v2, retriever_factory=lambda repository: ReversedRetriever(repository)
    )

    assert report.rank_gate.violations
    assert not report.rank_gate.passed
    assert {v.probe_id for v in report.rank_gate.violations} >= {"p-vpn", "p-entity"}
    assert all(v.max_rank == 3 for v in report.rank_gate.violations)


@pytest.mark.asyncio
async def test_a_missing_record_is_a_gate_violation_not_a_rank_result(corpus_v2):
    """``worst_required_rank`` stays ``None`` when a record never surfaced.

    Reporting the surviving records' worst rank would turn a recall failure into a
    passing rank result.
    """
    report = await evaluate(corpus_v2, retriever_factory=_nothing_retriever)

    assert not report.rank_gate.passed
    assert report.rank_gate.probes > 0
    for violation in report.rank_gate.violations:
        assert violation.worst_required_rank is None
        assert violation.missing_required


def _nothing_retriever(repository) -> MemoryRetriever:
    class NothingRetriever(MemoryRetriever):
        async def retrieve(self, query: MemoryQuery) -> list[MemorySelection]:
            return []

    return NothingRetriever(repository)


@pytest.mark.asyncio
async def test_the_ranking_block_is_no_longer_saturated(corpus_v2):
    """A block where everything is first cannot report a position regression.

    The corpus must therefore contain at least one query whose answer set is larger
    than one, so the k-slices are not interchangeable.
    """
    report = await evaluate(corpus_v2)

    multi = [p for p in corpus_v2.probes if len(p.must_surface) > 1]
    assert multi, "no query has more than one correct answer"
    assert any(p.max_rank is not None for p in multi)

    assert report.ranking[0].recall < 1.0
    assert report.ranking[0].recall < report.ranking[-1].recall


@pytest.mark.asyncio
async def test_the_report_states_which_records_the_policy_never_saw(corpus_v2):
    report = await evaluate(corpus_v2, anchor=datetime(2026, 6, 1, tzinfo=UTC))
    rendered = render_markdown(report)

    assert "直接播种" in rendered
    for step in corpus_v2.seeds:
        assert step.id in rendered
    assert "排名门禁" in rendered


@pytest.mark.asyncio
async def test_v2_safety_probes_are_not_vacuous_under_a_blind_retriever(corpus_v2):
    """The other direction: every forbidden record must actually be reachable."""

    class AclBlindRetriever(MemoryRetriever):
        async def retrieve(self, query: MemoryQuery) -> list[MemorySelection]:
            return [
                MemorySelection(memory=record, score=1.0, score_breakdown={})
                for record in self.repository.records
            ]

    report = await evaluate(
        corpus_v2, retriever_factory=lambda repository: AclBlindRetriever(repository)
    )

    expected = {
        "p-taint-settlement",
        "p-taint-access-review",
        "p-taint-capacity",
        "p-taint-user-scope",
        "p-taint-group-scope",
        "p-inj-roleplay",
        "p-inj-system-message",
        "p-inj-forget-previous",
        "p-inj-zh-system-prompt",
        "p-inj-ignore-previous",
        "p-inj-bypass-approval",
    }
    assert expected <= set(report.probe.leaking_probes)


class _ScrambledEmbedding:
    """Deterministic unit vectors derived from the text hash.

    Right shape, right dimension, no meaning: cosine between any two texts is noise.
    A ranking block that does not move under this is not reading the vectors at all.
    """

    model_name = "scrambled"

    @staticmethod
    def _vector(text: str) -> list[float]:
        seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")
        generator = random.Random(seed)
        values = [generator.gauss(0, 1) for _ in range(1024)]
        norm = math.sqrt(sum(value * value for value in values))
        return [value / norm for value in values]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


@pytest.mark.asyncio
async def test_an_embedding_scorer_must_be_named(corpus_v2):
    """``scoring_mode: "embedding"`` alone cannot distinguish two pinned revisions.

    The ranking block is the one block whose numbers depend on which vectors produced
    them, so an unnamed run is refused rather than recorded as an unattributable result.
    """
    with pytest.raises(MemoryEvaluationError, match="must be named"):
        await evaluate(corpus_v2, embedding=_ScrambledEmbedding())


@pytest.mark.asyncio
async def test_the_ranking_block_is_sensitive_to_the_scorer(corpus_v2):
    """Falsify the block that ``--embedding tei`` reports.

    Swapping the scorer for noise must destroy the ranking block. Without this, a green
    embedding report is indistinguishable from a report that never consulted the
    provider -- the same failure mode as a safety probe whose record was never reachable
    (see the corpus docstring). The safety block must NOT move: it asserts membership,
    and its independence from the scorer is a documented claim.
    """
    lexical = await evaluate(corpus_v2)
    scrambled = await evaluate(
        corpus_v2, embedding=_ScrambledEmbedding(), scoring_model="scrambled"
    )

    assert lexical.rank_gate.passed
    assert scrambled.scoring_mode == "embedding"
    assert scrambled.scoring_model == "scrambled"

    for score in scrambled.ranking:
        assert score.recall == 0.0, "noise still ranked the gold records"
        assert score.mrr == 0.0
    assert not scrambled.rank_gate.passed
    assert len(scrambled.rank_gate.violations) == lexical.rank_gate.probes
    assert any(o.missing_required for o in scrambled.probe_outcomes)

    # Safety is a membership assertion, so it must be blind to the scorer.
    assert scrambled.probe.leak_rate == lexical.probe.leak_rate == 0.0


@pytest.mark.asyncio
async def test_the_report_names_the_scorer_that_produced_the_ranking_block(corpus_v2):
    report = await evaluate(corpus_v2)
    rendered = render_markdown(report)

    assert report.scoring_model == "lexical-jaccard"
    assert "打分模型" in rendered
    assert report.scoring_model in rendered


# --------------------------------------------------------------------------------------
# The delivery gate: retrieval is not delivery
# --------------------------------------------------------------------------------------


def test_a_delivery_requirement_needs_a_record_and_a_stated_requirement():
    """``must_deliver`` is a product requirement, so it must come with the requirement."""
    with pytest.raises(ValidationError, match="without requiring a record"):
        MemoryProbe(id="p", label="l", text="t", must_deliver=True, delivery_rationale="r")
    with pytest.raises(ValidationError, match="without stating the requirement"):
        MemoryProbe(id="p", label="l", text="t", must_surface=("w-vpn-fact",), must_deliver=True)


def test_a_declared_load_needs_a_rationale_and_a_nonzero_item_size():
    """An unexplained load makes the gate unattributable; empty evidence measures nothing."""
    with pytest.raises(ValidationError, match="must state its rationale"):
        DeliveryLoad(evidence_items=3, evidence_item_chars=7000)
    with pytest.raises(ValidationError, match="without evidence_item_chars"):
        DeliveryLoad(evidence_items=3, rationale="because")


def test_the_declared_evidence_size_is_the_size_that_is_actually_attached():
    """A declared number the envelope does not actually carry measures the wrong load."""
    assert len(_evidence_content(0)) == 0
    for size in (1, 58, 59, 7000):
        assert len(_evidence_content(size)) == size


@pytest.mark.asyncio
async def test_only_probes_that_declared_a_delivery_requirement_are_gated(corpus_v2):
    """Green means "every declared requirement held", not "the corpus is fine"."""
    declared = [probe for probe in corpus_v2.probes if probe.must_deliver]
    assert declared, "the corpus declares no delivery requirement, so the gate is vacuous"

    # A probe that requires a record but never claimed delivery must not be gated:
    # counting it would inflate coverage with a requirement nobody stated.
    assert any(probe.must_surface and not probe.must_deliver for probe in corpus_v2.probes)

    report = await evaluate(corpus_v2, scoring_model="lexical-jaccard")
    assert report.delivery_gate.probes == len(declared)


def _inflate(corpus: MemoryScenarioCorpus, step_id: str, times: int) -> MemoryScenarioCorpus:
    """Repeat one memory's text ``times`` over.

    Repetition grows the token count while leaving the *set* of tokens -- and therefore
    both the lexical score and the record's relevance -- exactly where it was. That is
    the point: the published corpus memories are 25-204 characters, so they fit in any
    leftover and the delivery gate can never fire on them. A memory of the same
    relevance at a realistic length isolates length as the only variable.
    """
    steps = []
    for step in corpus.steps:
        if step.id == step_id and isinstance(step, (MemoryWriteStep, MemorySeedStep)):
            candidate = dict(step.candidate)
            candidate["content"] = candidate["content"] * times
            step = step.model_copy(update={"candidate": candidate})
        steps.append(step)
    return corpus.model_copy(update={"steps": steps})


def _with_evidence_cap(corpus: MemoryScenarioCorpus, cap: int | None) -> MemoryScenarioCorpus:
    return corpus.model_copy(
        update={
            "delivery": corpus.delivery.model_copy(
                update={
                    "evidence_token_cap": cap,
                    "rationale": f"test fixture: evidence token cap is {cap}",
                }
            )
        }
    )


@pytest.mark.asyncio
async def test_a_required_memory_the_budget_evicts_turns_the_gate_red(corpus_v2):
    """Falsification: green must be reachable *only* while the record actually fits."""
    baseline = await evaluate(corpus_v2, scoring_model="lexical-jaccard")
    assert baseline.delivery_gate.passed, baseline.delivery_gate.violations

    inflated = _inflate(_with_evidence_cap(corpus_v2, None), "w-user-preference", times=50)
    report = await evaluate(inflated, scoring_model="lexical-jaccard")

    outcome = next(o for o in report.probe_outcomes if o.probe_id == "p-preference")
    # It was still retrieved -- so this is a delivery failure, not a recall failure.
    assert outcome.missing_required == []
    assert outcome.pruned_required == ["w-user-preference"]

    assert not report.delivery_gate.passed
    assert [v.probe_id for v in report.delivery_gate.violations] == ["p-preference"]
    assert report.delivery_gate.violations[0].reason == "token_budget_exceeded"


@pytest.mark.asyncio
async def test_the_gate_is_blind_to_probes_that_never_declared_a_requirement(corpus_v2):
    """Same eviction, no declaration: the gate must stay quiet rather than guess."""
    undeclared = corpus_v2.model_copy(
        update={
            "probes": [
                probe.model_copy(update={"must_deliver": False, "delivery_rationale": ""})
                for probe in corpus_v2.probes
            ]
        }
    )
    report = await evaluate(
        _inflate(_with_evidence_cap(undeclared, None), "w-user-preference", times=50)
    )

    assert report.delivery_gate.probes == 0
    assert report.delivery_gate.violations == []


@pytest.mark.asyncio
async def test_capping_the_bulk_channel_is_what_delivers_a_realistic_procedure(corpus_v2):
    """The fix and the defect, measured against each other in one harness.

    Uncapped, evidence takes the envelope and a governed procedure of realistic
    length is retrieved and then pruned. Capping the evidence channel to half the
    usable envelope delivers it. Pinning both halves means neither the defect nor
    the fix can regress unnoticed.
    """
    # ``w-procedure-sso`` at 60x is ~7600 characters / ~3400 tokens: same words, so
    # the same relevance and the same rank, only a realistic length.
    corpus = _inflate(corpus_v2, "w-procedure-sso", times=60)

    uncapped = await evaluate(_with_evidence_cap(corpus, None), scoring_model="lexical-jaccard")
    outcome = next(o for o in uncapped.probe_outcomes if o.probe_id == "p-procedure")
    assert outcome.missing_required == [], "recall failed, so this is not a delivery result"
    assert outcome.pruned_required == ["w-procedure-sso"]
    assert not uncapped.delivery_gate.passed

    capped = await evaluate(corpus, scoring_model="lexical-jaccard")
    outcome = next(o for o in capped.probe_outcomes if o.probe_id == "p-procedure")
    assert outcome.pruned_required == []
    assert capped.delivery_gate.passed
    assert capped.delivery_load.evidence_token_cap == 5000
    assert capped.delivery_gate.headroom_tokens > uncapped.delivery_gate.headroom_tokens
