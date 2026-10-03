from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "audit_rag_quality_state", ROOT / "scripts/audit_rag_quality_state.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


PROXY = ROOT / "evaluation/reports/phase4_proxy_release_latest.json"
SMOKE = ROOT / "evaluation/reports/phase4_retrieval_latest.json"
SELECTION = ROOT / "evaluation/gold/phase4_proxy_release_v1.2.json"
TENANT_DOMAIN = ROOT / "evaluation/reports/tenant_domain_release_latest.json"
LABEL_DIAGNOSTIC = ROOT / "evaluation/reports/phase4_label_diagnostic_latest.json"
PACKING_CEILING = ROOT / "evaluation/reports/phase4_packing_ceiling_latest.json"
TECHQA = ROOT / "evaluation/reports/phase4_techqa_production_latest.json"


def _inputs() -> tuple[dict, dict, dict, dict, dict, dict]:
    """Every input ``build_status`` reads, loaded here rather than inside it.

    Loaded from the committed reports, so these tests fail if a producer's output stops
    carrying a field this derivation depends on -- which is the failure the audit exists to
    surface, and one a hand-built fixture would hide.
    """
    return (
        json.loads(PROXY.read_text()),
        json.loads(SMOKE.read_text()),
        json.loads(SELECTION.read_text()),
        json.loads(TENANT_DOMAIN.read_text()),
        json.loads(LABEL_DIAGNOSTIC.read_text()),
        json.loads(PACKING_CEILING.read_text()),
    )


def _techqa() -> dict:
    """The report the production path itself writes, loaded from disk for the same reason.

    Kept out of ``_inputs`` because only ``build_status`` needs it: the tests that call one
    derivation directly would otherwise pay for a file they never read, and a fixture that
    reads more than the function under test hides which input that function depends on.
    """
    return json.loads(TECHQA.read_text())


def test_quality_state_separates_domain_gate_proxy_and_smoke() -> None:
    proxy, smoke, selection, tenant_domain, label_diagnostic, packing_ceiling = _inputs()
    state = MODULE.build_status(
        proxy, smoke, selection, tenant_domain, label_diagnostic, packing_ceiling, _techqa()
    )
    assert state["overall_status"] == "DOMAIN_QUALITY_NOT_CERTIFIED"
    gate = state["tenant_release_gate"]
    assert gate["status"] == "NOT_EVALUATED"
    assert gate["gate_count"] == 6
    assert gate["evaluated_count"] == 0
    assert gate["required_input"] == "tenant-domain release queries with independent human qrels"
    # The reason the gate is shut is a derived list, and it must be the same list the
    # harness wrote into the gates themselves -- one definition, read twice.
    assert gate["blockers"] == proxy["release_gate_blockers"]
    assert gate["blockers"] == MODULE.release_gate_blockers_for(selection)
    assert len(gate["blockers"]) == 7
    assert state["release_blockers"][: len(gate["blockers"])] == gate["blockers"]
    assert state["external_silver"]["status"] == "BELOW_TARGET_DIAGNOSTIC"
    assert len(state["external_silver"]["below_reference_targets"]) == 4
    # The rate is the abstention sweep's own number, carried through rather than
    # recomputed here -- so it is asserted against its source, not against a literal.
    # Written as ``== 0.275`` until 2026-10-01, which froze a reading of the underlying
    # sweep into the test: re-recording ``phase4_proxy_release_latest.json`` turned a
    # statement about the transform into a red test about the data.
    held_out_abstention = proxy["retrieval"]["held_out_abstention"]
    signal = state["answerability_signal"]
    assert signal["answerable_answer_rate"] == held_out_abstention["answerable_answer_rate"]
    assert signal["impossible_abstention_rate"] == held_out_abstention["impossible_abstention_rate"]
    assert signal["roc_auc"] == held_out_abstention["top_score_roc_auc"]
    assert 0.0 <= signal["answerable_answer_rate"] <= 1.0
    # What makes the number unusable as an acceptance result is a property of the
    # measurement, not of its value, and that is what is pinned here.
    assert signal["is_end_to_end_reviewer_measurement"] is False
    assert signal["measurement_kind"] == "held-out retrieval top-score threshold proxy"
    assert signal["reviewer_semantic_calibration"] == "NOT_EVALUATED"
    assert state["committed_gold"]["status"] == "SMOKE_ONLY_SATURATED"
    assert set(state["committed_gold"]["saturated_arms"]) == {
        "dense",
        "bm25",
        "hybrid",
        "hybrid_rerank",
    }


def test_a_tenant_gate_cannot_silently_become_applicable() -> None:
    proxy, smoke, selection, tenant_domain, label_diagnostic, packing_ceiling = _inputs()
    proxy["gates"]["recall_at_5"]["applicable"] = True
    proxy["gates"]["recall_at_5"]["passed"] = False
    with pytest.raises(ValueError, match="while the selection blocks"):
        MODULE.build_status(
            proxy,
            smoke,
            selection,
            tenant_domain,
            label_diagnostic,
            packing_ceiling,
            _techqa(),
        )


def test_a_gate_cannot_name_blockers_the_selection_does_not_support() -> None:
    """The other direction: a gate may not invent a reason for its own inapplicability.

    Previously the guard only checked that every gate was marked not-applicable, which a
    report could satisfy while giving any reason at all. The reason is now compared
    against a re-derivation from the selection, so a gate that stays shut for a cause the
    data does not record is refused rather than believed.
    """
    proxy, smoke, selection, tenant_domain, label_diagnostic, packing_ceiling = _inputs()
    proxy["gates"]["mrr_at_10"]["blockers"] = ["we have not got round to it"]
    with pytest.raises(ValueError, match="the selection supports"):
        MODULE.build_status(
            proxy,
            smoke,
            selection,
            tenant_domain,
            label_diagnostic,
            packing_ceiling,
            _techqa(),
        )


def test_an_inapplicable_gate_may_not_record_a_verdict() -> None:
    proxy, smoke, selection, tenant_domain, label_diagnostic, packing_ceiling = _inputs()
    proxy["gates"]["ndcg_at_10"]["passed"] = False
    with pytest.raises(ValueError, match="yet it records a verdict"):
        MODULE.build_status(
            proxy,
            smoke,
            selection,
            tenant_domain,
            label_diagnostic,
            packing_ceiling,
            _techqa(),
        )


def test_the_derivation_is_not_a_constant() -> None:
    """A selection recording §3's strata stops blocking, with no code change.

    The guard above is only worth anything if the function behind it can come out the
    other way; otherwise "derived from the data" would be a rename of the hardcoded
    boolean it replaced.
    """
    from servicemind.evaluation.gold import ReleaseSetShape, release_gate_blockers

    qualifying = ReleaseSetShape(graded_queries=1, hard_negative_queries=1, development_queries=1)
    assert release_gate_blockers(qualifying) != []
    assert not [b for b in release_gate_blockers(qualifying) if "0--4" in b]


def test_the_unreachable_thresholds_are_derived_from_the_ceiling() -> None:
    """Which recall thresholds sit above the funnel is arithmetic, not a stored verdict.

    Recomputed here from the raw counts so the audit cannot report a list that its own
    input does not support -- e.g. by keeping a stale literal after the pool depth or the
    threshold table changes.
    """
    _, _, _, _, label_diagnostic, _ = _inputs()
    ceiling = label_diagnostic["candidate_funnel"]["contains_gold_rate"]
    expected_above = sorted(
        name
        for name, target in MODULE.TARGETS.items()
        if name.startswith("recall_at_") and target > ceiling
    )
    derived = MODULE.check_the_thresholds_can_be_reached(label_diagnostic)
    assert derived["thresholds_above_the_ceiling"] == expected_above
    # Non-recall thresholds (mrr, ndcg) are not recall@k and must not be swept in: they
    # are ratios over the returned list, not functions of whether the gold was in the pool.
    assert set(derived["thresholds_above_the_ceiling"]) <= {
        name for name in MODULE.TARGETS if name.startswith("recall_at_")
    }
    assert set(derived["thresholds_above_the_ceiling"]) | set(derived["thresholds_below_it"]) == {
        name for name in MODULE.TARGETS if name.startswith("recall_at_")
    }
    # The ceiling itself is carried, not restated: a reader of the status file gets the
    # number the derivation used.
    assert derived["candidate_pool"] == label_diagnostic["candidate_funnel"]


def test_only_recall_at_k_can_be_measured_against_the_ceiling() -> None:
    """MRR and nDCG are ratios over the returned list, not functions of pool membership.

    Pinned at a ceiling of zero, where *every* threshold in the table is above it: the
    derivation must still return only the ``recall_at_*`` names. At the real ceiling the
    two ratio thresholds happen to sit below it anyway, so a version that compared every
    threshold would be indistinguishable from the correct one there -- which is how this
    hole survived the first pass.
    """
    _, _, _, _, label_diagnostic, _ = _inputs()
    label_diagnostic["candidate_funnel"]["contains_gold_rate"] = 0.0
    derived = MODULE.check_the_thresholds_can_be_reached(label_diagnostic)
    assert derived["thresholds_above_the_ceiling"] == sorted(
        name for name in MODULE.TARGETS if name.startswith("recall_at_")
    )
    assert "mrr_at_10" not in derived["thresholds_above_the_ceiling"]
    assert "ndcg_at_10" not in derived["thresholds_above_the_ceiling"]


def test_a_funnel_that_covers_everything_leaves_no_unreachable_threshold() -> None:
    """The other direction, so the derivation is not a constant.

    A set whose candidate pool always contains the gold document bounds every threshold at
    1.0, and no recall threshold may then be called unreachable. Without this, the list
    could be hardcoded to ``["recall_at_10"]`` and every test above would still pass.
    """
    _, _, _, _, label_diagnostic, _ = _inputs()
    label_diagnostic["candidate_funnel"]["contains_gold_rate"] = 1.0
    derived = MODULE.check_the_thresholds_can_be_reached(label_diagnostic)
    assert derived["thresholds_above_the_ceiling"] == []
    assert "recall_at_5" in derived["thresholds_below_it"]


def test_the_label_diagnostic_section_carries_the_split_it_claims() -> None:
    """The section publishes the loss split, so it must point at the numbers it reports.

    The two title-overlap blocks are the finding; if the section stopped carrying them the
    markdown would keep rendering its prose sentence with nothing behind it.
    """
    _, _, _, _, label_diagnostic, _ = _inputs()
    section = MODULE._label_diagnostic_section(label_diagnostic)
    assert set(section["title_overlap"]) == {"in_pool", "out_of_pool"}
    assert section["answerable"] == label_diagnostic["queries"]["answerable"]
    assert (
        section["gold_documents_no_query_retrieves"]
        == (label_diagnostic["loss_split"]["out_of_pool"]["gold_documents_no_query_retrieves"])
    )
    # The in-pool group is the working-retriever group: the label and the first hit are
    # topically equivalent there, and the out-of-pool group is where they are not. Asserted
    # as an ordering rather than as literals, so re-recording the dump does not turn this
    # into a test about the data.
    in_pool = section["title_overlap"]["in_pool"]
    out_of_pool = section["title_overlap"]["out_of_pool"]
    assert out_of_pool["first_hit_title_closer_rate"] > in_pool["first_hit_title_closer_rate"]
    assert out_of_pool["mean_question_vs_gold_title"] < in_pool["mean_question_vs_gold_title"]


def test_the_packing_section_carries_the_verdict_it_names() -> None:
    """The section's claim is a cause, so it must carry every fact that decides it.

    Carrying the verdict without the booleans would leave the reader with a sentence and no
    way to check it -- and the markdown renders exactly this dict. What is asserted is the
    relation that holds in either regime, not the value one regime happened to produce:
    the per-source ceiling can be named as the cause only when it was in force *and* the
    pack reached it, so any other verdict means one of those two failed.
    """
    _, _, _, _, _, packing = _inputs()
    section = MODULE._packing_ceiling_section(packing)
    derivation = packing["derivation"]
    assert section["attributed_to"] == derivation["attributed_to"]
    assert section["packed_max"] == derivation["packed_max"]
    assert section["distinct_sources"] == packing["sampled"]["distinct_sources"]
    # Whether the ceiling was in force at all: a section that carried the verdict without
    # it would let a reader check the arithmetic and still not know which rule ran.
    assert section["source_ceiling_enforced"] == derivation["source_ceiling_enforced"]
    # The ceiling is the one the settings actually impose, not a copy of the number.
    assert section["max_parents_per_source"] == packing["settings"]["max_parents_per_source"]
    assert section["context_token_budget"] == packing["settings"]["context_token_budget"]
    assert section["next_parent_headroom_tokens"] == derivation["next_parent_headroom_tokens"]
    if section["attributed_to"] == "the per-source ceiling":
        assert section["source_ceiling_enforced"] is True
        assert section["packed_max"] >= section["max_parents_per_source"]
    else:
        assert (
            not section["source_ceiling_enforced"]
            or section["packed_max"] < section["max_parents_per_source"]
            or not derivation["budget_would_admit_another_parent"]
        )


def test_a_packing_verdict_its_own_numbers_exclude_is_refused() -> None:
    """A report may not name a cause that its own booleans contradict.

    Two directions, because the guard has one branch each: claiming the cap cut the list
    while the budget had no room, and claiming the budget cut it while there was room.

    Both mutate the verdict *and* the boolean it depends on, rather than leaning on
    whichever verdict the committed run happens to record -- a guard test that reads its
    own premise out of the data stops testing the guard the moment the data moves, which
    is exactly what happened here when the ceiling stopped applying.
    """
    _, _, _, _, _, packing = _inputs()
    packing["derivation"]["attributed_to"] = "the per-source ceiling"
    packing["derivation"]["budget_would_admit_another_parent"] = False
    with pytest.raises(ValueError, match="attributes the cut to 'the per-source ceiling'"):
        MODULE.check_the_packing_verdict_follows(packing)

    _, _, _, _, _, packing = _inputs()
    packing["derivation"]["attributed_to"] = "the token budget"
    packing["derivation"]["budget_would_admit_another_parent"] = True
    with pytest.raises(ValueError, match="the budget would have admitted another parent"):
        MODULE.check_the_packing_verdict_follows(packing)

    # A third case the two above do not reach: the cap reached, and *no* room in the
    # budget. Both conditions have to hold for the cap to be the cause, so a guard that
    # checked only the cap would wave this through -- and this is the only input that
    # tells the two guards apart.
    _, _, _, _, _, packing = _inputs()
    packing["derivation"]["attributed_to"] = "the per-source ceiling"
    packing["derivation"]["at_the_per_source_ceiling"] = True
    packing["derivation"]["budget_would_admit_another_parent"] = False
    with pytest.raises(ValueError, match="attributes the cut to 'the per-source ceiling'"):
        MODULE.check_the_packing_verdict_follows(packing)


def test_a_packing_verdict_naming_a_cause_the_numbers_exclude_is_refused() -> None:
    """The other two verdicts have their own exclusions, and they are checked too.

    "neither ceiling" says both ceilings were passed over, so it cannot stand while the
    pack reached one or the budget had no room; "final_k" says the list ended at its own
    depth, so it cannot stand while that depth was still out of reach. Without these the
    guard would only cover the two causes the original report happened to consider.
    """
    _, _, _, _, _, packing = _inputs()
    packing["derivation"]["attributed_to"] = "neither ceiling: the candidate list ran out"
    packing["derivation"]["at_the_per_source_ceiling"] = True
    with pytest.raises(ValueError, match="neither ceiling"):
        MODULE.check_the_packing_verdict_follows(packing)

    _, _, _, _, _, packing = _inputs()
    packing["derivation"]["attributed_to"] = "final_k"
    packing["derivation"]["at_the_per_source_ceiling"] = False
    packing["derivation"]["final_k_is_reachable"] = True
    with pytest.raises(ValueError, match="stopped short of that depth"):
        MODULE.check_the_packing_verdict_follows(packing)


def _techqa_report(
    *,
    packed_max: int,
    recalls: tuple[float, float, float] = (0.5, 0.5, 0.5),
    arms: tuple[str, ...] = ("dense", "bm25"),
) -> dict:
    """A minimal production-path report, sized by the one field the guard turns on.

    Built here rather than mutated out of the committed run because the committed run's own
    pack depth moves: before the source-ceiling fix every arm packed four documents, and a
    fixture that pinned that would stop being able to express the contradiction the moment
    the pipeline got better. The field the guard reads is the pack depth, so that is the
    parameter.
    """
    return {
        "status": "BELOW_TARGET_DIAGNOSTIC",
        "results": {
            arm: {
                "recall_at_5": recalls[0],
                "recall_at_10": recalls[1],
                "recall_at_20": recalls[2],
            }
            for arm in arms
        },
        "packing": {
            "final_k": 24,
            "context_token_budget": 8000,
            "distinct_sources": 1,
            "source_ceiling_enforced": False,
            "documents_packed_per_answerable_query": {
                arm: {"mean": 3.5, "min": 1, "max": packed_max} for arm in arms
            },
            "reading": "a reading",
        },
    }


def test_a_recall_figure_the_pack_depth_could_not_produce_is_refused() -> None:
    """A list of four cannot report different recalls at five and at ten.

    This is the shape the pre-fix production path actually had, and the reason Recall@5,
    Recall@10 and Recall@20 printed the same number: the wider window had nothing to add
    because the pack had already stopped. A report claiming otherwise is describing a
    ranking it did not measure.
    """
    with pytest.raises(ValueError, match="cannot lose gold between 5 and 10"):
        MODULE.check_the_recall_cutoffs_match_the_pack(
            _techqa_report(packed_max=4, recalls=(0.5, 0.6, 0.6))
        )


def test_the_guard_fires_on_the_last_document_that_still_collapses() -> None:
    """The boundary is the cutoff itself: five documents cannot separate five from ten."""
    with pytest.raises(ValueError, match="packed at most 5"):
        MODULE.check_the_recall_cutoffs_match_the_pack(
            _techqa_report(packed_max=5, recalls=(0.5, 0.6, 0.6))
        )


def test_one_document_past_the_cutoff_frees_the_two_reads_to_differ() -> None:
    """Six documents put a position in the window that five did not have, and no more."""
    cutoffs = MODULE.check_the_recall_cutoffs_match_the_pack(
        _techqa_report(packed_max=6, recalls=(0.5, 0.6, 0.6))
    )
    assert cutoffs["arms"]["dense"]["cutoffs_the_pack_cannot_tell_apart"] == [10, 20]


def test_a_pack_shorter_than_the_cutoffs_is_named_as_one_measurement() -> None:
    cutoffs = MODULE.check_the_recall_cutoffs_match_the_pack(_techqa_report(packed_max=4))
    assert cutoffs["arms_where_the_cutoffs_are_one_measurement"] == ["bm25", "dense"]
    assert cutoffs["arms"]["dense"]["cutoffs_the_pack_cannot_tell_apart"] == [5, 10, 20]
    # §4.1 states bars at Recall@5 and Recall@10 and no other recall cutoff, so the only
    # one of them a four-document pack cannot decide for itself is Recall@10. Recall@5
    # keeps its own bar -- there is nothing narrower for it to collapse onto.
    assert cutoffs["thresholds_no_arm_can_distinguish"] == ["recall_at_10"]
    assert set(MODULE.TARGETS) & {"recall_at_20", "mrr_at_20"} == set()


def test_a_pack_deep_enough_to_separate_the_cutoffs_names_no_collapsed_threshold() -> None:
    cutoffs = MODULE.check_the_recall_cutoffs_match_the_pack(
        _techqa_report(packed_max=24, recalls=(0.6, 0.7, 0.8))
    )
    assert cutoffs["arms_where_the_cutoffs_are_one_measurement"] == []
    assert cutoffs["arms"]["dense"]["cutoffs_the_pack_cannot_tell_apart"] == []
    assert cutoffs["thresholds_no_arm_can_distinguish"] == []


def test_a_threshold_a_second_arm_can_distinguish_is_not_reported_as_unmeasured() -> None:
    """One arm's short pack does not make the bar unmeasurable if another arm reaches it.

    The claim is about the *evidence*, not about any one arm: a cutoff collapses only when
    nothing in the report separates it from the cutoff below.
    """
    report = _techqa_report(packed_max=4)
    report["results"]["bm25"] = {
        "recall_at_5": 0.4,
        "recall_at_10": 0.7,
        "recall_at_20": 0.8,
    }
    report["packing"]["documents_packed_per_answerable_query"]["bm25"]["max"] = 24
    cutoffs = MODULE.check_the_recall_cutoffs_match_the_pack(report)
    assert cutoffs["arms_where_the_cutoffs_are_one_measurement"] == ["dense"]
    assert cutoffs["thresholds_no_arm_can_distinguish"] == []


def test_the_production_path_section_names_the_report_it_measured() -> None:
    section = MODULE._production_path_section(_techqa_report(packed_max=4))
    assert section["status"] == "BELOW_TARGET_DIAGNOSTIC"
    assert section["final_k"] == 24
    assert section["context_token_budget"] == 8000


def test_the_quality_state_carries_the_cutoffs_the_production_path_can_measure() -> None:
    """Wired, not merely available: the section must read the run, not a fixture.

    Asserted against the committed report rather than against literals, so re-recording it
    after a pipeline change updates the derivation instead of turning a statement about the
    transform into a red test about the data.
    """
    proxy, smoke, selection, tenant_domain, label_diagnostic, packing_ceiling = _inputs()
    report = _techqa()
    state = MODULE.build_status(
        proxy, smoke, selection, tenant_domain, label_diagnostic, packing_ceiling, report
    )
    production = state["production_path"]
    assert production["report"].endswith("phase4_techqa_production_latest.json")
    assert sorted(production["arms"]) == sorted(report["results"])
    packed = report["packing"]["documents_packed_per_answerable_query"]
    for arm, block in production["arms"].items():
        assert block["packed_max"] == packed[arm]["max"]
        recalls = {report["results"][arm][f"recall_at_{k}"] for k in MODULE.RECALL_CUTOFFS}
        assert block["one_measurement"] is (len(recalls) == 1)


def test_a_metric_the_pack_never_reached_is_named_beside_the_pack_depth() -> None:
    """The depth in a metric's name is a claim about the window it was scored over.

    At four packed documents, every one of the §4.1 metrics is computed over a window the
    arm never filled -- even Recall@5 asks about a fifth document that does not exist -- so
    each of them reports the pack. The metric names are taken from the same table the
    thresholds are, so this cannot drift from §4.1 by being maintained in a second place.
    """
    cutoffs = MODULE.check_the_recall_cutoffs_match_the_pack(_techqa_report(packed_max=4))
    assert cutoffs["arms"]["dense"]["metrics_the_pack_is_too_short_for"] == [
        "mrr_at_10",
        "ndcg_at_10",
        "recall_at_10",
        "recall_at_5",
    ]
    assert cutoffs["metrics_no_arm_reaches"] == [
        "mrr_at_10",
        "ndcg_at_10",
        "recall_at_10",
        "recall_at_5",
    ]


def test_one_arm_deep_enough_clears_the_metric_for_the_whole_report() -> None:
    """It is a claim about the evidence, so a second arm that reaches the window clears it.

    Reporting a metric as unmeasurable because *one* arm was short would be the wrong
    quantity: the question is whether anything in the report scored it over its full
    window, not whether every arm did.
    """
    report = _techqa_report(packed_max=4)
    report["results"]["bm25"] = {"recall_at_5": 0.4, "recall_at_10": 0.7, "recall_at_20": 0.8}
    report["packing"]["documents_packed_per_answerable_query"]["bm25"]["max"] = 12
    cutoffs = MODULE.check_the_recall_cutoffs_match_the_pack(report)
    assert cutoffs["arms"]["bm25"]["metrics_the_pack_is_too_short_for"] == []
    assert cutoffs["arms"]["dense"]["metrics_the_pack_is_too_short_for"] == [
        "mrr_at_10",
        "ndcg_at_10",
        "recall_at_10",
        "recall_at_5",
    ]
    assert cutoffs["metrics_no_arm_reaches"] == []


def test_a_pack_that_stops_at_the_cutoff_still_counts_the_whole_pack_there() -> None:
    """Five documents make Recall@5 the whole pack, not a top five.

    The cutoff a pack exactly reaches is the first one that stops being a window: the list
    is five long, so all five are inside it. Naming it as distinguishable would report a
    bar the pack decides by itself.
    """
    cutoffs = MODULE.check_the_recall_cutoffs_match_the_pack(_techqa_report(packed_max=5))
    assert cutoffs["arms"]["dense"]["cutoffs_the_pack_cannot_tell_apart"] == [5, 10, 20]
