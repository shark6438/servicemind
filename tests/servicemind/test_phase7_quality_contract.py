"""The quality contract, judged on its own. No services, no cluster, no model.

The grader is a pure function over recorded observations, so every rule the 200-case batch
relies on can be pinned here, in CI, on every pull request, against no infrastructure: that
each class has a judge, that a class whose assertion is "this must not happen" cannot be
satisfied by a run that did nothing, that an unobserved case is never a pass, and that the
headline rate is computed over the denominator the case list registered a target for.

The shipped ``cases.v1.json`` and its corpus are loaded here too. A case list nobody
validates is one that goes stale quietly, and the failure that matters is the one where the
file still parses: a case that lost the citation it turns on still loads, still grades, and
still reports a pass.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from servicemind.evaluation.quality import (
    QUALITY_SCHEMA_VERSION,
    REQUIRED_COMPOSITION,
    CaseKind,
    QualityCase,
    QualityCaseSet,
    case_set_digest,
    citations_in,
    load_quality_cases,
    read_citations,
    reviewer_decision,
)
from servicemind.evaluation.quality_grader import (
    ASSERTING_DECISION,
    TERMINAL_STATUSES,
    CaseObservation,
    Verdict,
    grade,
    outcome_summary,
    render_report,
    wilson_interval,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
QUALITY = REPO_ROOT / "evaluation" / "quality"
CASES_PATH = QUALITY / "cases.v1.json"
MANIFEST_PATH = QUALITY / "manifest.json"
CORPUS_DIR = QUALITY / "corpus"
ACCEPTANCE_MANIFEST = (
    REPO_ROOT / "evaluation" / "acceptance" / "fixtures" / "globex" / "manifest.json"
)

TENANT = "22222222-2222-4222-8222-222222222222"
NOW = datetime(2026, 9, 24, tzinfo=UTC)
DIGEST = "0" * 64


@pytest.fixture(scope="module")
def shipped() -> QualityCaseSet:
    return load_quality_cases(CASES_PATH)


def observation(case: QualityCase, **changes: object) -> CaseObservation:
    """An observation that *passes* its case, before ``changes`` is applied.

    Built from the case rather than written out, so a test about one rule cannot
    accidentally also be a test about a stale expected citation.

    What passing looks like differs by class, and it is not the same reviewer decision for
    all four: an answerable case passes when the reviewer asserts an answer, and a case in
    either of the two classes built on "the platform should not answer this" passes when it
    does not. A single default decision would make half the tests assertions about the
    wrong thing.
    """
    asserts = case.kind in (CaseKind.ANSWERABLE, CaseKind.VERSION_CONFLICT)
    fields: dict[str, object] = {
        "case_id": case.id,
        "observed_at": NOW,
        "deployed_revision": "test",
        "cases_digest": DIGEST,
        "run_id": "00000000-0000-4000-8000-000000000000",
        "terminal_status": "succeeded",
        "reviewer_decision": ASSERTING_DECISION if asserts else "abstain",
        "citations": tuple(case.expected_citations) if asserts else (),
        "errors": (),
        "elapsed_seconds": 1.0,
        # A run that answered nothing asserted nothing it could not support. Zero, not the
        # -1 default: the insufficient-evidence rule reads -1 as "not measured" and blocks,
        # so a fixture that left these unset would turn every test of that class into a test
        # of the missing-field guard.
        "unsupported_claims": 0,
        "missing_evidence": 0,
        "proposed_actions": 0,
        "review_findings": 0,
    }
    fields.update(changes)
    return CaseObservation(**fields)


def judge_one(case: QualityCase, **changes: object) -> Verdict:
    """Grade a single case and hand back its verdict."""
    outcome = grade(
        case_set_for([case]),
        [observation(case, **changes)],
        generated_at=NOW,
    )
    assert len(outcome.cases) == 1
    return outcome.cases[0].verdict


def case_set_for(cases: list[QualityCase]) -> QualityCaseSet:
    """A set that holds these cases whatever their classes, bypassing the composition rule.

    The composition rule is a property of the shipped list, asserted separately below. A
    per-rule test needs a set of one case, and routing every such test through the real
    120/40/20/20 requirement would mean either padding each one to 200 cases or not testing
    the rules at all.
    """
    return QualityCaseSet.model_construct(
        schema_version=QUALITY_SCHEMA_VERSION,
        description="constructed for a rule test",
        # A real ``UUID`` rather than the string: ``model_construct`` skips validation, so
        # the string would survive into the digest and serialise with a warning.
        tenant_id=UUID(TENANT),
        answerable_rate_target=0.85,
        answerable_rate_target_source="constructed",
        cases=tuple(cases),
    )


def make(kind: CaseKind, **fields: object) -> QualityCase:
    base: dict[str, object] = {
        "id": "Q-TEST",
        "kind": kind,
        "subject": "globex-analyst-g3",
        "ticket_ref": "globex-quality-access",
        "question": "a question",
    }
    base.update(fields)
    return QualityCase(**base)


@lru_cache(maxsize=1)
def load_quality_runner() -> Any:
    """``scripts/verify_phase7_quality_live.py`` as a module.

    Loaded by path because ``scripts/`` is not a package. Its rules about who may ask are
    live-runner rules -- they read the corpus manifest, which the pure grader deliberately
    does not hold -- so testing them means loading the runner. The module is guarded by
    ``if __name__ == "__main__"``, so importing it starts nothing.
    """
    spec = importlib.util.spec_from_file_location(
        "_quality_runner", REPO_ROOT / "scripts" / "verify_phase7_quality_live.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_valid(kind: CaseKind, **fields: object) -> QualityCase:
    """A case of this class with a declaration the class validator accepts.

    The three hard classes disagree about which citation fields they may carry -- that is
    the declaration table -- so a test that wants to vary some *other* field cannot pass one
    fixed pair of citation lists through all four classes.
    """
    declarations = {
        CaseKind.ANSWERABLE: {"expected_citations": ("KB-Q-A",)},
        CaseKind.INSUFFICIENT_EVIDENCE: {},
        CaseKind.VERSION_CONFLICT: {
            "expected_citations": ("KB-Q-A",),
            "forbidden_citations": ("KB-Q-B",),
        },
        CaseKind.MUST_REFUSE_ACCESS: {"forbidden_citations": ("KB-Q-B",)},
    }[kind]
    return make(kind, **{**declarations, **fields})


# ------------------------------------------------------------------ the shipped list


def test_the_shipped_list_is_exactly_the_declared_composition(shipped: QualityCaseSet) -> None:
    actual: dict[str, int] = {}
    for case in shipped.cases:
        actual[case.kind.value] = actual.get(case.kind.value, 0) + 1
    assert actual == REQUIRED_COMPOSITION, (
        f"the shipped set is {actual}, the requirement is {REQUIRED_COMPOSITION}"
    )
    assert sum(actual.values()) == 200


def test_the_digest_describes_the_file_including_its_order(shipped: QualityCaseSet) -> None:
    """Reordering the cases moves the digest, and that is the intended answer.

    The digest's job is to detect that the file the observations were taken under is not
    the file on disk now. A reorder is a change to the file, so it is a change to report --
    collapsing order away would mean shipping a drift check that stays quiet through an
    edit it cannot see. Whether the *grader* cares about order is a separate property, and
    it does not; that is asserted below.
    """
    reversed_set = QualityCaseSet.model_construct(
        **{**shipped.model_dump(mode="python"), "cases": tuple(reversed(shipped.cases))}
    )
    assert case_set_digest(reversed_set) != case_set_digest(shipped)


def test_the_grader_does_not_care_what_order_it_is_handed(shipped: QualityCaseSet) -> None:
    """Every case is judged against its own declaration, so the batch order is free."""
    observations = [observation(case) for case in shipped.cases]
    forwards = grade(shipped, observations, generated_at=NOW)
    backwards = grade(shipped, list(reversed(observations)), generated_at=NOW)
    assert [case.verdict for case in forwards.cases] == [case.verdict for case in backwards.cases]
    assert forwards.verdict is backwards.verdict
    assert forwards.observations_digest == backwards.observations_digest


def test_changing_one_expectation_moves_the_digest(shipped: QualityCaseSet) -> None:
    """The drift check is only worth anything if this is true."""
    mutated = list(shipped.cases)
    target = next(case for case in mutated if case.kind is CaseKind.ANSWERABLE)
    mutated[mutated.index(target)] = QualityCase.model_construct(
        **{**target.model_dump(mode="python"), "expected_citations": ("KB-Q-SOMETHING-ELSE",)}
    )
    changed = QualityCaseSet.model_construct(
        **{**shipped.model_dump(mode="python"), "cases": tuple(mutated)}
    )
    assert case_set_digest(changed) != case_set_digest(shipped)


def test_a_set_with_the_right_total_but_the_wrong_proportion_is_rejected() -> None:
    """200 cases is not the requirement; 120/40/20/20 is."""
    cases = [
        make(CaseKind.ANSWERABLE, id=f"Q-{n}", expected_citations=("KB-Q-A",)) for n in range(200)
    ]
    with pytest.raises(ValidationError, match="frozen composition"):
        QualityCaseSet(
            schema_version=QUALITY_SCHEMA_VERSION,
            description="wrong proportion",
            tenant_id=TENANT,
            answerable_rate_target=0.85,
            answerable_rate_target_source="constructed",
            cases=tuple(cases),
        )


def test_duplicate_case_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate case ids"):
        QualityCaseSet(
            schema_version=QUALITY_SCHEMA_VERSION,
            description="duplicates",
            tenant_id=TENANT,
            answerable_rate_target=0.85,
            answerable_rate_target_source="constructed",
            cases=tuple([make(CaseKind.ANSWERABLE, expected_citations=("KB-Q-A",))] * 200),
        )


def test_the_threshold_carries_its_provenance(shipped: QualityCaseSet) -> None:
    """A threshold with no stated source is indistinguishable from one picked after the run."""
    assert shipped.answerable_rate_target_source.strip()
    assert 0 < shipped.answerable_rate_target <= 1


def test_every_case_declaration_matches_its_class(shipped: QualityCaseSet) -> None:
    """The validator's own table, over the real list rather than over examples of it."""
    for case in shipped.cases:
        match case.kind:
            case CaseKind.ANSWERABLE:
                assert case.expected_citations, case.id
            case CaseKind.INSUFFICIENT_EVIDENCE:
                assert not case.expected_citations and not case.forbidden_citations, case.id
            case CaseKind.VERSION_CONFLICT:
                assert case.expected_citations and case.forbidden_citations, case.id
            case CaseKind.MUST_REFUSE_ACCESS:
                assert case.forbidden_citations and not case.expected_citations, case.id
        assert not set(case.expected_citations) & set(case.forbidden_citations), case.id


@pytest.mark.parametrize(
    ("kind", "fields"),
    [
        (CaseKind.ANSWERABLE, {}),
        (CaseKind.ANSWERABLE, {"forbidden_citations": ("KB-Q-X",)}),
        (CaseKind.INSUFFICIENT_EVIDENCE, {"expected_citations": ("KB-Q-X",)}),
        (CaseKind.INSUFFICIENT_EVIDENCE, {"forbidden_citations": ("KB-Q-X",)}),
        (CaseKind.VERSION_CONFLICT, {"forbidden_citations": ("KB-Q-X",)}),
        (CaseKind.VERSION_CONFLICT, {"expected_citations": ("KB-Q-X",)}),
        (CaseKind.MUST_REFUSE_ACCESS, {}),
        (
            CaseKind.MUST_REFUSE_ACCESS,
            {"expected_citations": ("KB-Q-X",), "forbidden_citations": ("KB-Q-Y",)},
        ),
        (
            CaseKind.ANSWERABLE,
            {"expected_citations": ("KB-Q-X",), "forbidden_citations": ("KB-Q-X",)},
        ),
    ],
)
def test_a_case_filed_under_the_wrong_class_does_not_load(
    kind: CaseKind, fields: dict[str, object]
) -> None:
    """A ``must-refuse-access`` row with no forbidden document asserts nothing about access.

    Graded, it would be PASS for any run -- including one that never happened -- which is
    the failure the declaration table exists to make impossible.
    """
    with pytest.raises(ValidationError):
        make(kind, **fields)


# ------------------------------------------------------------------- the corpus


def test_every_case_named_document_is_declared_in_the_manifest() -> None:
    manifest_ids = {
        str(entry["source_record_id"])
        for entry in json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))["documents"]
    }
    named = set(load_quality_cases(CASES_PATH).corpus_record_ids())
    assert named, "no case names a document at all"
    assert not named - manifest_ids, (
        "a case names a document the manifest never seeds, so its failure would be "
        f"reported as a retrieval miss: {sorted(named - manifest_ids)}"
    )


def test_the_manifest_and_the_corpus_directory_agree_in_both_directions() -> None:
    declared = {
        str(entry["file"])
        for entry in json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))["documents"]
    }
    on_disk = {path.name for path in CORPUS_DIR.glob("*.md")}
    assert declared == on_disk, (
        f"declared but not on disk: {sorted(declared - on_disk)}; "
        f"on disk but undeclared: {sorted(on_disk - declared)}"
    )


def test_the_corpus_can_actually_support_its_three_hard_classes() -> None:
    """Each hard class needs the situation it tests to exist in the corpus.

    A corpus with nothing restricted makes every must-refuse-access case pass, and one with
    nothing retired makes every version-conflict case pass -- both by never having created
    the condition, which is indistinguishable from surviving it.
    """
    entries = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))["documents"]
    restricted = [entry for entry in entries if entry["group_ids"]]
    superseded = [entry for entry in entries if not entry["is_active"]]
    assert len(restricted) >= 10, f"only {len(restricted)} documents are group-restricted"
    assert len(superseded) >= 10, f"only {len(superseded)} documents are retired"
    # Both groups have to be represented, or one subject's cases have nothing to refuse.
    assert {group for entry in restricted for group in entry["group_ids"]} >= {3, 4}


def test_the_quality_corpus_does_not_collide_with_the_acceptance_corpus() -> None:
    """The two share a tenant and a serving index, so a shared record id merges them."""
    if not ACCEPTANCE_MANIFEST.exists():  # pragma: no cover -- acceptance is shipped
        pytest.skip("the acceptance corpus is not present")
    theirs = {
        str(entry["source_record_id"])
        for entry in json.loads(ACCEPTANCE_MANIFEST.read_text(encoding="utf-8"))["documents"]
    }
    ours = {
        str(entry["source_record_id"])
        for entry in json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))["documents"]
    }
    assert not theirs & ours, f"claimed by both corpora: {sorted(theirs & ours)}"


def test_the_two_subjects_ask_the_cases_they_are_supposed_to() -> None:
    """A must-refuse-access case is only that if the asker cannot reach the document."""
    case_set = load_quality_cases(CASES_PATH)
    assert set(case_set.subject_usernames()) == {"globex-analyst-g3", "globex-analyst-g4"}
    entries = {
        str(entry["source_record_id"]): set(entry["group_ids"])
        for entry in json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))["documents"]
    }
    held = {"globex-analyst-g3": {3}, "globex-analyst-g4": {4}}
    for case in case_set.cases:
        if case.kind is not CaseKind.MUST_REFUSE_ACCESS:
            continue
        for record_id in case.forbidden_citations:
            groups = entries[record_id]
            assert groups, f"{record_id} is not restricted, so refusing it proves nothing"
            assert not (groups & held[case.subject]), (
                f"{case.id}: {case.subject} actually holds {sorted(groups)}, so the document "
                "is reachable and the case asserts no isolation"
            )


# -------------------------------------------------------------- the reading of a run


def _run_payload(citations: list[dict[str, object]], decision: str | None) -> dict[str, object]:
    from servicemind.domain.evidence import CITATION_KEY

    return {
        "evidence": [{"metadata": {CITATION_KEY: payload}} for payload in citations],
        "review": {"decision": decision} if decision else {},
    }


def _citation(record_id: str, seed: int = 0) -> dict[str, object]:
    """A citation payload the platform's own ``Citation`` model accepts.

    Written against the real model rather than a loose dict, and validated below, because
    the reader validates through it: a fixture whose ``citation_id`` did not match the
    platform's own ``^cite-[0-9a-f]{16}$`` would exercise the *unreadable* path instead of
    the readable one, and a test asserting on the readable path would pass for the wrong
    reason -- or, as happened when this was first written, fail for one.
    """
    return {
        "citation_id": f"cite-{seed:016x}",
        "document_id": "00000000-0000-4000-8000-000000000001",
        "parent_chunk_id": "00000000-0000-4000-8000-000000000002",
        "source": "servicemind_quality_fixture",
        "source_uri": f"quality://globex/{record_id}",
        "source_record_id": record_id,
        "source_version": QUALITY_SCHEMA_VERSION,
        "content_hash": f"{seed:064x}",
        "title": "t",
    }


def _assert_the_fixture_is_a_citation() -> None:
    """The fixture above is only a citation if the platform says so. Pin that here."""
    from servicemind.domain.knowledge import Citation

    Citation.model_validate(_citation("KB-Q-A", seed=1))


def test_the_citation_fixture_is_itself_a_valid_citation() -> None:
    """Otherwise the two tests below would be testing the unreadable path."""
    _assert_the_fixture_is_a_citation()


def test_the_citation_reader_reports_the_ids_the_run_cited() -> None:
    payload = _run_payload(
        [_citation("KB-Q-A", 1), _citation("KB-Q-B", 2), _citation("KB-Q-A", 1)], "passed"
    )
    assert citations_in(payload) == ["KB-Q-A", "KB-Q-B"], "duplicates and order both matter"


def test_the_citation_reader_counts_rows_it_cannot_parse() -> None:
    """The count has to travel, or an unparseable citation looks like no citation at all."""
    payload = _run_payload([_citation("KB-Q-A", 1)], "passed")
    payload["evidence"].append({"metadata": {"citation": {"nonsense": True}}})  # type: ignore[union-attr]
    reading = read_citations(payload)
    assert reading.source_record_ids == ("KB-Q-A",)
    assert reading.unreadable == 1


def test_the_citation_reader_separates_unparseable_from_absent() -> None:
    """Most evidence is not a citation, and that is not the same as a broken one."""
    payload = _run_payload([_citation("KB-Q-A", 1)], "passed")
    payload["evidence"].append({"metadata": {"something_else": 1}})  # type: ignore[union-attr]
    reading = read_citations(payload)
    assert reading.unreadable == 0
    assert reading.without_citation == 1


def test_a_missing_review_is_not_a_passed_review() -> None:
    """The whole insufficient-evidence class rests on this being false."""
    assert reviewer_decision(_run_payload([], None)) is None
    assert reviewer_decision(None) is None
    assert reviewer_decision({}) is None
    assert reviewer_decision(_run_payload([], "abstain")) == "abstain"


def test_a_run_made_as_somebody_else_blocks_the_case() -> None:
    """Every rule here is about what happens when *this* identity asks.

    A run by a different subject is not a weak observation of the case -- it is an
    observation of a different case, and folding the two together is how twenty
    must-refuse-access cases pass against a token that was never the wrong side of the wall.
    """
    case = make(CaseKind.MUST_REFUSE_ACCESS, forbidden_citations=("KB-Q-G3-ONLY",))
    assert judge_one(case, observed_username="globex-analyst-g4") is Verdict.BLOCKED
    assert judge_one(case, observed_username=case.subject) is Verdict.PASS


def test_the_reachability_rule_fires_when_the_asker_holds_the_owning_group() -> None:
    """The runner's precondition, which the grader cannot check and must not duplicate.

    Exercised through ``importlib`` rather than by import, because ``scripts/`` is not a
    package. The one thing this test is for is that the arithmetic is right: a case is
    vacuous exactly when the subject's groups intersect the document's.
    """
    runner = load_quality_runner()
    case = make(CaseKind.MUST_REFUSE_ACCESS, forbidden_citations=("KB-Q-G3-ONLY",))
    restrictions = {"KB-Q-G3-ONLY": frozenset({3})}

    assert runner.reachability_errors(case, restrictions, frozenset({4})) == []
    assert runner.reachability_errors(case, restrictions, frozenset()) == []
    held = runner.reachability_errors(case, restrictions, frozenset({3, 4}))
    assert len(held) == 1 and "no isolation" in held[0]


def test_the_reachability_rule_refuses_an_unrestricted_forbidden_document() -> None:
    """A forbidden document nobody is restricted from was never out of reach."""
    runner = load_quality_runner()
    case = make(CaseKind.MUST_REFUSE_ACCESS, forbidden_citations=("KB-Q-PUBLIC",))
    problems = runner.reachability_errors(case, {"KB-Q-PUBLIC": frozenset()}, frozenset({3}))
    assert len(problems) == 1 and "proves nothing" in problems[0]


def test_the_shipped_must_refuse_cases_are_not_vacuous() -> None:
    """The runner's own rule, applied to the two identities that will actually run them."""
    runner = load_quality_runner()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    restrictions = {
        str(entry["source_record_id"]): frozenset(int(value) for value in entry["group_ids"])
        for entry in manifest["documents"]
    }
    held = {"globex-analyst-g3": frozenset({3}), "globex-analyst-g4": frozenset({4})}
    cases = [
        case
        for case in load_quality_cases(CASES_PATH).cases
        if case.kind is CaseKind.MUST_REFUSE_ACCESS
    ]
    assert cases, "there are no must-refuse-access cases to be vacuous"
    for case in cases:
        assert runner.reachability_errors(case, restrictions, held[case.subject]) == [], case.id


# ---------------------------------------------------------------------- the grading


def test_an_answerable_case_passes_on_success_passed_and_the_expected_citation() -> None:
    case = make(CaseKind.ANSWERABLE, expected_citations=("KB-Q-A",))
    assert judge_one(case) is Verdict.PASS


def test_an_answerable_case_that_never_ran_is_not_a_pass() -> None:
    case = make(CaseKind.ANSWERABLE, expected_citations=("KB-Q-A",))
    assert (
        judge_one(case, terminal_status=None, reviewer_decision=None, citations=())
        is Verdict.BLOCKED
    )


def test_an_answerable_case_that_cited_the_wrong_document_fails() -> None:
    case = make(CaseKind.ANSWERABLE, expected_citations=("KB-Q-A",))
    assert judge_one(case, citations=("KB-Q-B",)) is Verdict.FAIL


def test_an_answerable_case_the_reviewer_abstained_on_fails() -> None:
    """Answerable means the corpus answers it. Abstaining is the platform getting it wrong."""
    case = make(CaseKind.ANSWERABLE, expected_citations=("KB-Q-A",))
    assert judge_one(case, reviewer_decision="abstain") is Verdict.FAIL


def test_a_version_conflict_case_passes_on_the_current_document_alone() -> None:
    case = make(
        CaseKind.VERSION_CONFLICT,
        expected_citations=("KB-Q-CURRENT",),
        forbidden_citations=("KB-Q-OLD",),
    )
    assert judge_one(case, citations=("KB-Q-CURRENT",)) is Verdict.PASS


def test_citing_the_superseded_revision_fails_even_when_the_answer_was_good() -> None:
    """The point of the class: a correct-looking answer built on a retired procedure."""
    case = make(
        CaseKind.VERSION_CONFLICT,
        expected_citations=("KB-Q-CURRENT",),
        forbidden_citations=("KB-Q-OLD",),
    )
    verdict = judge_one(case, citations=("KB-Q-CURRENT", "KB-Q-OLD"))
    assert verdict is Verdict.FAIL


def test_a_must_refuse_access_case_with_no_reviewer_decision_is_blocked_not_passed() -> None:
    """The vacuous pass this class exists to refuse.

    A run that reached no review cited nothing, and "it did not cite the restricted
    document" is trivially true of it. Reading that as isolation is the empty-result-as-
    proof failure, so it is BLOCKED -- which blocks the acceptance rather than passing it.
    """
    case = make(CaseKind.MUST_REFUSE_ACCESS, forbidden_citations=("KB-Q-G3-ONLY",))
    assert judge_one(case, reviewer_decision=None, citations=()) is Verdict.BLOCKED
    assert judge_one(case, reviewer_decision="abstain", citations=()) is Verdict.PASS


def test_a_must_refuse_access_case_fails_the_moment_the_document_appears() -> None:
    case = make(CaseKind.MUST_REFUSE_ACCESS, forbidden_citations=("KB-Q-G3-ONLY",))
    assert judge_one(case, citations=("KB-Q-PUBLIC", "KB-Q-G3-ONLY")) is Verdict.FAIL


def test_a_correct_refusal_is_not_a_failure_for_passing_its_own_review() -> None:
    """The rule this class was written with first, and the smoke run that falsified it.

    ``reviewer_decision == "passed"`` means the claims are supported by the evidence they
    cite -- not that the question was answered. Against the seeded corpus the platform
    answered an uncovered question with "none of the cited documents states a procedure for
    this", and the reviewer passed *that*. Failing on ``passed`` failed every correct
    refusal, which is worse than testing nothing: it would have reported a well-behaved
    platform as fabricating forty times.
    """
    case = make(CaseKind.INSUFFICIENT_EVIDENCE)
    assert judge_one(case, reviewer_decision="passed") is Verdict.PASS
    assert judge_one(case, reviewer_decision="abstain") is Verdict.PASS


def test_an_insufficient_evidence_case_fails_on_an_unsupported_claim() -> None:
    """What the class can actually detect: the platform's own review catching a fabrication."""
    case = make(CaseKind.INSUFFICIENT_EVIDENCE)
    assert judge_one(case, unsupported_claims=1) is Verdict.FAIL
    assert judge_one(case, unsupported_claims=0) is Verdict.PASS


def test_an_insufficient_evidence_case_fails_when_it_proposes_steps_it_cannot_ground() -> None:
    """A decline proposes nothing. Steps for an unanswered question came from somewhere else."""
    case = make(CaseKind.INSUFFICIENT_EVIDENCE)
    assert judge_one(case, proposed_actions=2) is Verdict.FAIL
    assert judge_one(case, proposed_actions=0) is Verdict.PASS


def test_an_insufficient_evidence_case_that_did_not_rest_is_a_failure() -> None:
    """Independently of the review: a question the corpus cannot answer is still one to process."""
    case = make(CaseKind.INSUFFICIENT_EVIDENCE)
    assert judge_one(case, terminal_status="failed") is Verdict.FAIL


def test_readings_the_platform_did_not_publish_block_rather_than_pass() -> None:
    """A shape this reader does not recognise is not a clean bill of health.

    This is the guard against the fix for the falsified rule becoming a hole: if the response
    stops carrying ``unsupported_claims``, every insufficient-evidence case would otherwise
    read as "checked, nothing found" -- which is the same verdict the field's absence should
    make unsupportable.
    """
    case = make(CaseKind.INSUFFICIENT_EVIDENCE)
    assert judge_one(case, unsupported_claims=-1) is Verdict.BLOCKED


def test_an_insufficient_evidence_case_that_never_ran_is_blocked_not_passed() -> None:
    case = make(CaseKind.INSUFFICIENT_EVIDENCE)
    assert judge_one(case, terminal_status=None, reviewer_decision=None) is Verdict.BLOCKED


@pytest.mark.parametrize("kind", list(CaseKind))
def test_a_driver_error_blocks_every_class(kind: CaseKind) -> None:
    """A run whose driver recorded an error is not evidence about the platform."""
    case = make_valid(kind)
    assert judge_one(case, errors=("POST /runs returned 500",)) is Verdict.BLOCKED


@pytest.mark.parametrize("kind", list(CaseKind))
def test_an_unreadable_citation_blocks_every_class(kind: CaseKind) -> None:
    """If the cited set is unknown, no assertion about the cited set can be made."""
    case = make_valid(kind)
    assert judge_one(case, unreadable_citations=1) is Verdict.BLOCKED


@pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES))
def test_every_terminal_status_is_judged_rather_than_blocked(status: str) -> None:
    """Whatever a run rests at, it finished, and a finished run gets a verdict.

    Judged, not necessarily passed: for a question the corpus cannot answer, only a resting
    success is the platform having handled it. A run that failed or was cancelled is a
    finding about the platform, not a gap in the observation, and BLOCKED would file it as
    the second -- which is what this test is here to keep apart. The premise used to be
    "every terminal status passes", which was true only while the class's rule was the one
    the smoke run falsified.
    """
    case = make(CaseKind.INSUFFICIENT_EVIDENCE)
    verdict = judge_one(case, terminal_status=status, reviewer_decision="abstain")
    assert verdict is not Verdict.BLOCKED
    assert verdict is (Verdict.PASS if status == "succeeded" else Verdict.FAIL)


def test_a_run_still_in_flight_is_not_judged(shipped: QualityCaseSet) -> None:
    case = next(case for case in shipped.cases if case.kind is CaseKind.ANSWERABLE)
    assert judge_one(case, terminal_status="running", reviewer_decision="passed") is Verdict.BLOCKED


def test_a_case_with_no_observation_is_blocked_and_named(shipped: QualityCaseSet) -> None:
    outcome = grade(shipped, [], generated_at=NOW)
    assert outcome.verdict is Verdict.BLOCKED
    assert len(outcome.cases) == len(shipped.cases)
    assert all(case.verdict is Verdict.BLOCKED for case in outcome.cases)
    assert len(outcome.blockers) == len(shipped.cases)


# ------------------------------------------------------------------ the headline rate


def _answerable_set(count: int) -> list[QualityCase]:
    return [
        make(CaseKind.ANSWERABLE, id=f"Q-A{n}", expected_citations=("KB-Q-A",))
        for n in range(count)
    ]


def test_the_rate_is_none_when_no_answerable_case_was_observed() -> None:
    """Zero out of 120 unrun cases is not a measurement of zero."""
    cases = _answerable_set(120)
    outcome = grade(
        case_set_for(cases),
        [observation(case, terminal_status=None, reviewer_decision=None) for case in cases],
        generated_at=NOW,
    )
    assert outcome.answerable_rate() is None
    assert outcome.answerable_interval() is None
    assert outcome_summary(outcome)["answerable_unobserved"] == 120


def test_the_rate_is_over_every_answerable_case_not_the_observed_ones() -> None:
    """Shrinking the denominator to the cases that behaved is how a batch flatters itself."""
    cases = _answerable_set(10)
    observed = [observation(case) for case in cases[:5]]
    unobserved = [
        observation(case, terminal_status=None, reviewer_decision=None) for case in cases[5:]
    ]
    outcome = grade(case_set_for(cases), observed + unobserved, generated_at=NOW)
    assert outcome.answerable_rate() == pytest.approx(0.5), "5 of 10, not 5 of 5"


def test_no_interval_is_quoted_for_a_batch_with_holes() -> None:
    cases = _answerable_set(10)
    observed = [observation(case) for case in cases[:8]]
    unobserved = [
        observation(case, terminal_status=None, reviewer_decision=None) for case in cases[8:]
    ]
    outcome = grade(case_set_for(cases), observed + unobserved, generated_at=NOW)
    assert outcome.answerable_rate() is not None
    assert outcome.answerable_interval() is None


def test_a_fully_observed_batch_gets_a_closed_interval() -> None:
    cases = _answerable_set(10)
    outcome = grade(case_set_for(cases), [observation(case) for case in cases], generated_at=NOW)
    low, high = outcome.answerable_interval()
    assert 0.0 <= low <= 1.0 and 0.0 <= high <= 1.0
    assert low <= 1.0 and high >= 1.0 - 1e-9, "ten of ten answered, so the interval reaches 1"


def test_the_interval_stays_inside_the_unit_square_at_the_extremes() -> None:
    """The reason for Wilson over the normal approximation, stated as an assertion."""
    assert wilson_interval(0, 10)[0] == 0.0
    assert wilson_interval(10, 10)[1] == 1.0
    low, high = wilson_interval(0, 120)
    assert low == 0.0 and 0 < high < 0.05, "never answered, but 120 cases is not certainty of zero"


def test_an_interval_over_no_observations_is_refused() -> None:
    with pytest.raises(ValueError):
        wilson_interval(0, 0)


# ------------------------------------------------------------- the verdict ordering


def test_one_fabrication_ends_the_batch_whatever_the_rate() -> None:
    cases = _answerable_set(120) + [make(CaseKind.INSUFFICIENT_EVIDENCE, id="Q-FAB")]
    observations = [observation(case) for case in cases if case.id != "Q-FAB"]
    observations.append(observation(cases[-1], unsupported_claims=1))
    outcome = grade(case_set_for(cases), observations, generated_at=NOW)
    assert outcome.verdict is Verdict.FAIL
    assert outcome.answerable_rate() == 1.0, "the rate was perfect and it did not matter"


def test_insufficient_observation_outranks_a_short_rate() -> None:
    """A batch with holes has a rate over cases nobody measured; that is not a quality defect."""
    cases = _answerable_set(120)
    observations = [observation(case) for case in cases[:10]]
    observations += [
        observation(case, terminal_status=None, reviewer_decision=None) for case in cases[10:]
    ]
    outcome = grade(case_set_for(cases), observations, generated_at=NOW)
    assert outcome.verdict is Verdict.BLOCKED
    assert outcome.answerable_rate() < 0.85, "and the rate is genuinely short"


def test_a_fully_observed_batch_below_its_registered_target_fails() -> None:
    cases = _answerable_set(120)
    observations = [observation(case) for case in cases[:100]]
    observations += [observation(case, citations=("KB-Q-WRONG",)) for case in cases[100:]]
    outcome = grade(case_set_for(cases), observations, generated_at=NOW)
    assert outcome.answerable_rate() == pytest.approx(100 / 120)
    assert outcome.verdict is Verdict.FAIL
    assert any("answerable rate" in blocker for blocker in outcome.blockers)


def test_a_fully_observed_batch_at_its_target_passes() -> None:
    cases = _answerable_set(120)
    # 102/120 = 0.85 exactly.
    observations = [observation(case) for case in cases[:102]]
    observations += [observation(case, citations=("KB-Q-WRONG",)) for case in cases[102:]]
    outcome = grade(case_set_for(cases), observations, generated_at=NOW)
    assert outcome.answerable_rate() == pytest.approx(0.85)
    assert outcome.verdict is Verdict.PASS


# ----------------------------------------------------------------------- the report


def test_the_report_states_what_the_batch_did_not_prove(shipped: QualityCaseSet) -> None:
    """Rendered from a fully observed batch.

    An unobserved one renders the same limitations section but not the paragraph about the
    number being replaced, and that paragraph is the one this test is about -- the report
    has to say, in the artifact a reader will actually open, that the headline rate is a
    different measurement from the ``0.275`` it supersedes. Without that sentence the two
    get compared, and they are not comparable.
    """
    observations = [observation(case) for case in shipped.cases]
    markup = render_report(grade(shipped, observations, generated_at=NOW))
    assert "**不**证明的内容" in markup, "the limitations section is folded into a heading"
    assert "0.275" in markup, "the number this replaces has to be named in the report"
    assert "检索 top-score 阈值代理" in markup, "and named as what it actually is"


def test_the_report_is_generated_from_the_outcome_not_written_beside_it() -> None:
    """A hand-written report is one that can disagree with the observations it describes."""
    cases = _answerable_set(10)
    outcome = grade(case_set_for(cases), [observation(case) for case in cases], generated_at=NOW)
    markup = render_report(outcome)
    assert outcome.cases_digest in markup
    assert outcome.observations_digest in markup


def test_the_summary_carries_the_denominator_and_the_target() -> None:
    cases = _answerable_set(4)
    summary = outcome_summary(grade(case_set_for(cases), [], generated_at=NOW))
    assert summary["answerable_total"] == 4
    assert summary["answerable_unobserved"] == 4
    assert summary["answerable_rate_target"] == 0.85
