"""Build the single machine-readable truth for current RAG quality evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from servicemind.evaluation.gold import (
    AnnotationProvenance,
    ReleaseSetShape,
    label_tier_from_recorded,
    release_gate_blockers,
)

ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / "evaluation/reports/phase4_proxy_release_latest.json"
SMOKE = ROOT / "evaluation/reports/phase4_retrieval_latest.json"
SELECTION = ROOT / "evaluation/gold/phase4_proxy_release_v1.2.json"
TENANT_DOMAIN = ROOT / "evaluation/reports/tenant_domain_release_latest.json"
LABEL_DIAGNOSTIC = ROOT / "evaluation/reports/phase4_label_diagnostic_latest.json"
PACKING_CEILING = ROOT / "evaluation/reports/phase4_packing_ceiling_latest.json"
TECHQA = ROOT / "evaluation/reports/phase4_techqa_production_latest.json"
OUTPUT_JSON = ROOT / "evaluation/reports/rag_quality_status_latest.json"
OUTPUT_MD = ROOT / "evaluation/reports/rag_quality_status_latest.md"
TARGETS = {
    "recall_at_5": 0.85,
    "recall_at_10": 0.90,
    "mrr_at_10": 0.75,
    "ndcg_at_10": 0.80,
}


def release_gate_blockers_for(selection: dict[str, Any]) -> list[str]:
    """Re-derive, from the selection file alone, why §4.1 does or does not apply.

    Deliberately computed here rather than read out of the report: the whole question is
    whether the report's claim about applicability is supported by the data it was produced
    from, and reading the claim back would answer that by assumption. This is the same
    function the harness calls, so a disagreement means one of the two is wrong, not that
    the two use different definitions.
    """
    return release_gate_blockers(
        ReleaseSetShape(
            provenance=AnnotationProvenance(
                label_tier=label_tier_from_recorded(selection["label_tier"]),
                notes=f"labels supplied by {selection['source']}@{selection['source_revision']}",
            )
        )
    )


def check_gates_match_the_selection(proxy: dict[str, Any], selection: dict[str, Any]) -> list[str]:
    """Every gate must agree with the set it was scored on, in both directions.

    The previous version of this guard asserted ``applicable is False`` for all gates,
    which held the right answer for the wrong reason: it would have rejected a report
    whose gate legitimately opened, and it could not tell a harness that measured a
    qualifying set from one that hardcoded the conclusion. This compares the recorded
    verdict against what the selection supports, so a gate that opens without the data
    behind it fails, and a gate that stays shut while naming something other than the
    actual blockers fails too.
    """
    blockers = release_gate_blockers_for(selection)
    problems: list[str] = []
    for name, gate in proxy["gates"].items():
        recorded = gate.get("blockers")
        if recorded is None:
            problems.append(
                f"{name}: the gate records no blockers, so its applicability is unstated"
            )
            continue
        if sorted(recorded) != sorted(blockers):
            problems.append(
                f"{name}: records blockers {sorted(recorded)} but the selection supports "
                f"{sorted(blockers)}"
            )
        if gate.get("applicable") is not (not blockers):
            problems.append(
                f"{name}: applicable={gate.get('applicable')!r} while the selection "
                f"{'blocks' if blockers else 'permits'} the gate"
            )
        if gate.get("applicable") is False and gate.get("passed") is not None:
            problems.append(f"{name}: not applicable, yet it records a verdict")
    if problems:
        raise ValueError("gate applicability disagrees with the selection: " + "; ".join(problems))
    return blockers


#: The strata below which the §4.1 count is a shape rather than a finding. Below either of
#: these the count is zero because there is nothing for it to be nonzero about, which is the
#: reading the whole tenant-domain fixture exists to make unavailable.
MIN_STRATA = {"tenants": 2, "group_restricted": 1}


def check_the_strata_can_carry_the_count(report: dict[str, Any]) -> dict[str, int]:
    """Refuse to report a §4.1 count over a corpus that could not have leaked.

    The same guard the gate applies to itself. A zero over a single-tenant corpus with no
    group-restricted documents is not evidence that the ACL filter works; it is evidence
    that the corpus never asked it to. This raises rather than annotating because an
    annotated zero is exactly the thing a reader skims past on the way to the number.
    """
    strata = report["corpus"]["strata"]
    thin = {
        axis: strata.get(axis, 0)
        for axis, floor in MIN_STRATA.items()
        if strata.get(axis, 0) < floor
    }
    if thin:
        raise ValueError(
            f"the tenant-domain report measures a §4.1 count over a corpus with no stratum "
            f"to be nonzero about: {thin}. Report the count over a corpus that could leak, "
            f"or report it as unmeasured -- not as zero"
        )
    return strata


#: The recall cutoffs the §4.1 clauses and ``TARGETS`` are stated at. Named once, so the
#: audit, the evaluations that feed it and its own tests cannot disagree about which
#: cutoffs are under discussion.
RECALL_CUTOFFS = (5, 10, 20)


def _cutoff_of(name: str) -> int:
    """The depth a §4.1 metric name is stated at: ``recall_at_5`` -> 5, ``ndcg_at_10`` -> 10.

    Every metric in ``TARGETS`` ends in the depth it is defined over, which is what makes
    them comparable to a packed count at all -- and what makes "the pack is shorter than
    the metric" a question that can be asked of the whole table rather than of recall alone.
    """
    return int(name.rsplit("_", 1)[1])


def _distinguishes(recalls: dict[str, float], name: str) -> bool:
    """Whether a threshold at ``name`` is a bar this arm's own numbers separate.

    A threshold at ``k`` adds nothing over the next narrower cutoff when the two reads are
    equal: the wider window bought no gold, so the bar was already decided by the narrow
    one. The narrowest cutoff has nothing below it to collapse onto, so it is always a
    bar of its own.
    """
    k = int(name.removeprefix("recall_at_"))
    narrower = [other for other in RECALL_CUTOFFS if other < k]
    if not narrower:
        return True
    return recalls[name] != recalls[f"recall_at_{max(narrower)}"]


def check_the_recall_cutoffs_match_the_pack(report: dict[str, Any]) -> dict[str, Any]:
    """Refuse a recall figure the arm's own pack depth could not have produced.

    ``recall_at_k`` counts gold inside ``ranked[:k]``, so an arm that packed at most ``n``
    documents reports the *same* number at every cutoff ``k >= n``: there is nothing past
    ``n`` for the wider window to add. That equality is not a coincidence to tolerate, it
    is the arithmetic of the cut -- and it decides which §4.1 bars the production path can
    be measured against at all. An arm that packs four documents has no Recall@10 distinct
    from its Recall@5, so a threshold stated at Recall@10 is, on that arm, a bar on the
    whole pack wearing the name of a top ten.

    The equality is therefore returned rather than merely checked. What is checked is the
    converse, the one that cannot be true: two different numbers at cutoffs the list never
    reached. A report carrying that is describing a ranking it did not measure, and it
    raises rather than annotating, because an annotated contradiction reads as a caveat.
    """
    packed = report["packing"]["documents_packed_per_answerable_query"]
    arms: dict[str, Any] = {}
    for arm, row in report["results"].items():
        deepest = packed[arm]["max"]
        recalls = {k: row[f"recall_at_{k}"] for k in RECALL_CUTOFFS}
        for narrow, wide in zip(RECALL_CUTOFFS, RECALL_CUTOFFS[1:]):
            if deepest <= narrow and recalls[narrow] != recalls[wide]:
                raise ValueError(
                    f"{arm} reports recall_at_{narrow}={recalls[narrow]} and "
                    f"recall_at_{wide}={recalls[wide]} while it packed at most {deepest} "
                    f"documents: a list that never reached {narrow} items cannot lose gold "
                    f"between {narrow} and {wide}"
                )
        arms[arm] = {
            "packed_mean": packed[arm]["mean"],
            "packed_min": packed[arm]["min"],
            "packed_max": deepest,
            "recall": {f"recall_at_{k}": recalls[k] for k in RECALL_CUTOFFS},
            "cutoffs_the_pack_cannot_tell_apart": [k for k in RECALL_CUTOFFS if k >= deepest],
            "metrics_the_pack_is_too_short_for": sorted(
                name for name in TARGETS if _cutoff_of(name) > deepest
            ),
            "one_measurement": len(set(recalls.values())) == 1,
        }
    return {
        "final_k": report["packing"]["final_k"],
        "context_token_budget": report["packing"]["context_token_budget"],
        "distinct_sources": report["packing"]["distinct_sources"],
        "source_ceiling_enforced": report["packing"]["source_ceiling_enforced"],
        "arms": arms,
        "arms_where_the_cutoffs_are_one_measurement": sorted(
            arm for arm, block in arms.items() if block["one_measurement"]
        ),
        "thresholds_no_arm_can_distinguish": sorted(
            name
            for name in TARGETS
            if name.startswith("recall_at_")
            and not any(_distinguishes(block["recall"], name) for block in arms.values())
        ),
        # The §4.1 metrics no arm in this report ranked deep enough to be scored over their
        # full window. Their values are still real; what they measure is the pack.
        "metrics_no_arm_reaches": sorted(
            name
            for name in TARGETS
            if all(_cutoff_of(name) > block["packed_max"] for block in arms.values())
        ),
        "reading": report["packing"]["reading"],
    }


def _production_path_section(report: dict[str, Any]) -> dict[str, Any]:
    """The §4.1 recall cutoffs, measured where the pipeline actually cuts.

    The external-silver columns above come from the proxy's blended ranking, which keeps
    the whole depth-100 pool and says so in its own limitations. This reads the report
    produced by ``EnterpriseRAG.retrieve`` itself, so the cutoffs in it are the ones a
    tenant's prompt is built under.
    """
    return {
        "status": report["status"],
        "report": str(TECHQA.relative_to(ROOT)),
        **check_the_recall_cutoffs_match_the_pack(report),
    }


def check_the_thresholds_can_be_reached(report: dict[str, Any]) -> dict[str, Any]:
    """Which §4.1 recall thresholds sit above the funnel's own ceiling.

    Derived rather than asserted, because the answer is a fact about the thresholds and
    about ``candidate_k``, not about anyone's judgement. RRF, the cross-encoder and the
    blend only reorder the candidates, so the share of answerable queries whose gold
    document is anywhere in the pool bounds *every* Recall@k on this set. A threshold
    above that bound cannot be met by ranking better, and saying so is the difference
    between "we are 0.14 short" and "0.14 of this gap is not ours to close".
    """
    ceiling = report["candidate_funnel"]["contains_gold_rate"]
    unreachable = sorted(
        name
        for name, threshold in TARGETS.items()
        if name.startswith("recall_at_") and threshold > ceiling
    )
    return {
        "candidate_pool": report["candidate_funnel"],
        "thresholds_above_the_ceiling": unreachable,
        "thresholds_below_it": sorted(
            name for name in TARGETS if name.startswith("recall_at_") and name not in unreachable
        ),
        "selection": report["inputs"]["selection"],
        "label_tier": report["inputs"]["label_tier"],
        "reading": report["reading"],
    }


def _label_diagnostic_section(report: dict[str, Any]) -> dict[str, Any]:
    split = report["loss_split"]
    return {
        "status": report["status"],
        "report": str(LABEL_DIAGNOSTIC.relative_to(ROOT)),
        **check_the_thresholds_can_be_reached(report),
        "answerable": report["queries"]["answerable"],
        "gold_documents": report["queries"]["gold_documents"],
        "arms_contains_gold": {
            arm: row["contains_gold_rate"] for arm, row in report["arms"].items()
        },
        "title_overlap": {group: block["title_overlap"] for group, block in split.items()},
        "gold_documents_no_query_retrieves": split["out_of_pool"][
            "gold_documents_no_query_retrieves"
        ],
    }


def check_the_packing_verdict_follows(report: dict[str, Any]) -> dict[str, Any]:
    """Refuse a packing verdict whose own two inputs do not support it.

    The diagnostic publishes four possible causes, and each is decided by booleans that
    are in the same file: whether the budget had room for another parent, whether the pack
    reached the source ceiling, and whether that ceiling was in force at all. A verdict
    naming a cause those booleans exclude is a report contradicting itself -- exactly the
    failure a reader cannot catch by eye, because the sentence reads fine.
    """
    derivation = report["derivation"]
    verdict = derivation["attributed_to"]
    room = derivation["budget_would_admit_another_parent"]
    at_cap = derivation["at_the_per_source_ceiling"]
    reachable = derivation["final_k_is_reachable"]
    if verdict == "the per-source ceiling" and not (room and at_cap):
        raise ValueError(
            f"the packing report attributes the cut to {verdict!r} while "
            f"budget_would_admit_another_parent={room} and at_the_per_source_ceiling={at_cap}"
        )
    if verdict == "the token budget" and room:
        raise ValueError(
            f"the packing report attributes the cut to {verdict!r} while the budget would "
            "have admitted another parent"
        )
    if verdict.startswith("neither ceiling") and (at_cap or not room):
        raise ValueError(
            f"the packing report attributes the cut to {verdict!r} while "
            f"at_the_per_source_ceiling={at_cap} and budget_would_admit_another_parent={room}"
        )
    if verdict == "final_k" and reachable:
        raise ValueError(
            f"the packing report attributes the cut to {verdict!r} while the list stopped "
            "short of that depth"
        )
    return derivation


def _packing_ceiling_section(report: dict[str, Any]) -> dict[str, Any]:
    derivation = check_the_packing_verdict_follows(report)
    sampled = report["sampled"]
    return {
        "status": report["status"],
        "report": str(PACKING_CEILING.relative_to(ROOT)),
        "attributed_to": derivation["attributed_to"],
        "context_token_budget": report["settings"]["context_token_budget"],
        "max_parents_per_source": report["settings"]["max_parents_per_source"],
        "max_parents_per_document": report["settings"]["max_parents_per_document"],
        "distinct_sources": sampled["distinct_sources"],
        "source_ceiling_enforced": derivation["source_ceiling_enforced"],
        "packed_max": derivation["packed_max"],
        "median_parent_tokens": derivation["median_parent_tokens"],
        "next_parent_headroom_tokens": derivation["next_parent_headroom_tokens"],
        "share_of_parents_that_still_fit": derivation["share_of_parents_that_still_fit"],
        "reading": report["reading"],
    }


def _tenant_domain_section(report: dict[str, Any]) -> dict[str, Any]:
    strata = check_the_strata_can_carry_the_count(report)
    probes = report["acl_probes"]
    controls = [probe for probe in probes if probe["control"] is not None]
    arms = report["results"]
    return {
        "status": "STRATA_PRESENT_NOT_POWERED",
        "report": str(TENANT_DOMAIN.relative_to(ROOT)),
        "corpus_strata": strata,
        "queries_by_category": report["queries"]["by_category"],
        "refusal_reasons": report["queries"]["refusal_reasons"],
        "visible_evidence_violations": {
            arm: row["visible_evidence_violations"] for arm, row in arms.items()
        },
        "acl_probes": {
            "probes": len(probes),
            "isolation_held": sum(1 for probe in probes if probe["isolation_held"]),
            "with_a_positive_control": len(controls),
            "controls_held": sum(1 for probe in controls if probe["control"]),
        },
        "release_gate_blockers": report["release_gate_blockers"],
        "reading": (
            "the §4.1 clauses each have a document here to be about, and the ACL count is "
            "computed against the loader's own coordinates. The set is synthetic, so its "
            "blockers still name human signoff -- which is what makes it evidence that the "
            "only unmet clause is the one no fixture can supply. It is not a quality "
            "measurement: the corpus is fixture-sized and its recall is saturated"
        ),
    }


def build_status(
    proxy: dict[str, Any],
    smoke: dict[str, Any],
    selection: dict[str, Any],
    tenant_domain: dict[str, Any],
    label_diagnostic: dict[str, Any],
    packing_ceiling: dict[str, Any],
    techqa: dict[str, Any],
) -> dict[str, Any]:
    gates = proxy["gates"]
    blockers = check_gates_match_the_selection(proxy, selection)
    tenant_domain_section = _tenant_domain_section(tenant_domain)
    label_section = _label_diagnostic_section(label_diagnostic)
    packing_section = _packing_ceiling_section(packing_ceiling)
    production_path_section = _production_path_section(techqa)
    production = proxy["retrieval"]["production"]
    diagnostic = {name: production[name] for name in TARGETS}
    below = [name for name, target in TARGETS.items() if diagnostic[name] < target]
    abstention = proxy["retrieval"]["held_out_abstention"]
    smoke_results = smoke["results"]
    saturated_arms = [
        name
        for name, metrics in smoke_results.items()
        if all(metrics[f"recall_at_{k}"] == 1.0 for k in (5, 10, 20))
        and metrics["mrr_at_10"] == 1.0
    ]
    return {
        "schema_version": "servicemind-rag-quality-state-v1",
        "overall_status": "DOMAIN_QUALITY_NOT_CERTIFIED",
        "quality_exception": "QUALITY_EXCEPTION_ACCEPTED",
        "tenant_release_gate": {
            "status": "APPLICABLE" if not blockers else "NOT_EVALUATED",
            "gate_count": len(gates),
            "evaluated_count": sum(item["applicable"] is True for item in gates.values()),
            "required_input": "tenant-domain release queries with independent human qrels",
            # Derived from the selection, not written here: this list is the reason the
            # gate is shut, and a hand-maintained copy of it would be the same unfalsifiable
            # prose the gate itself used to carry.
            "blockers": blockers,
        },
        "external_silver": {
            "dataset": proxy["source"]["dataset"],
            "status": "BELOW_TARGET_DIAGNOSTIC",
            "quality_certification": False,
            "metrics": diagnostic,
            "reference_targets": TARGETS,
            "below_reference_targets": below,
            "interpretation": (
                "All four point estimates are below the tenant thresholds, but this external "
                "silver distribution is not allowed to decide the tenant release gate."
            ),
        },
        "answerability_signal": {
            "status": "WEAK_RETRIEVAL_SCORE_SIGNAL",
            "measurement_kind": "held-out retrieval top-score threshold proxy",
            "is_end_to_end_reviewer_measurement": False,
            "answerable_answer_rate": abstention["answerable_answer_rate"],
            "impossible_abstention_rate": abstention["impossible_abstention_rate"],
            "roc_auc": abstention["top_score_roc_auc"],
            "best_case_balanced_accuracy": abstention["best_case_balanced_accuracy"],
            "reviewer_semantic_calibration": "NOT_EVALUATED",
        },
        "label_diagnostic": label_section,
        "packing_ceiling": packing_section,
        "production_path": production_path_section,
        "tenant_domain": tenant_domain_section,
        "committed_gold": {
            "status": "SMOKE_ONLY_SATURATED",
            "documents": smoke["ingested"]["documents"],
            "children": smoke["ingested"]["children"],
            "queries": smoke["queries"]["total"],
            "saturated_arms": saturated_arms,
            "precision_at_5": smoke_results["hybrid_rerank"]["precision_at_5"],
            "mean_dedupe_rate": smoke_results["hybrid_rerank"]["mean_dedupe_rate"],
            "allowed_use": "deterministic regression smoke only",
        },
        "production_change": {
            "retrieval_quality_improved_by_current_measurement_work": False,
            "configuration_change_approved": False,
            "next_plan": "docs/PHASE4_RAG_QUALITY_IMPROVEMENT_PLAN_V2_0.md",
        },
        "release_blockers": [
            *blockers,
            "Reviewer answerability and abstention correctness are not calibrated on domain labels",
            "production evidence/context-cap impact has no non-synthetic observations",
        ],
    }


def render_markdown(status: dict[str, Any]) -> str:
    gate = status["tenant_release_gate"]
    silver = status["external_silver"]
    signal = status["answerability_signal"]
    smoke = status["committed_gold"]
    rows = "\n".join(
        f"| {name} | {value:.4f} | {silver['reference_targets'][name]:.2f} | below |"
        for name, value in silver["metrics"].items()
    )
    blockers = "\n".join(f"- {item}" for item in status["release_blockers"])
    labels = status["label_diagnostic"]
    pool = labels["candidate_pool"]
    unreachable = ", ".join(labels["thresholds_above_the_ceiling"]) or "none"
    overlap = labels["title_overlap"]
    packing = status["packing_ceiling"]
    production = status["production_path"]
    arms = production["arms"]
    domain = status["tenant_domain"]
    strata = domain["corpus_strata"]
    probes = domain["acl_probes"]
    cutoff_rows = "\n".join(
        "| {arm} | {mean} | {low} | {high} | {collapsed} | {short} |".format(
            arm=arm,
            mean=block["packed_mean"],
            low=block["packed_min"],
            high=block["packed_max"],
            collapsed=",".join(str(k) for k in block["cutoffs_the_pack_cannot_tell_apart"])
            or "none",
            short=", ".join(block["metrics_the_pack_is_too_short_for"]) or "none",
        )
        for arm, block in arms.items()
    )
    one_measurement = ", ".join(production["arms_where_the_cutoffs_are_one_measurement"]) or "none"
    indistinguishable = ", ".join(production["thresholds_no_arm_can_distinguish"]) or "none"
    too_short = ", ".join(production["metrics_no_arm_reaches"]) or "none"
    collapsed_note = (
        f"Arms whose three recall cutoffs are one measurement: **{one_measurement}**. On "
        "those arms a §4.1 bar stated at Recall@10 is the narrower bar it collapses onto: "
        "the wider window bought no gold because there was nothing at the deeper positions "
        "to buy.\n\n"
        if production["arms_where_the_cutoffs_are_one_measurement"]
        else "No arm's three recall cutoffs are one measurement, so each §4.1 recall bar is "
        "a window the pack reached rather than the whole pack under a wider name.\n\n"
    )
    collapsed_note += (
        f"Recall thresholds no arm's own numbers can distinguish from the cutoff below "
        f"them: **{indistinguishable}**.\n\n"
    )
    too_short_note = (
        f"§4.1 metrics no arm ranked deep enough to be scored over their full window: "
        f"**{too_short}**. A value under such a name is real, but it measures the pack "
        f"rather than the ranking the name describes -- a statement about where the bar "
        f"may be *read*, not about whether it was met.\n\n"
        if production["metrics_no_arm_reaches"]
        else "Every §4.1 metric was scored over the window its name claims: no arm in this "
        "run was cut short of the depth a threshold is stated at.\n\n"
    )
    too_short_note += (
        "The §4.1 clauses are bars on the ranking; the delivery path decides how much of "
        "that ranking a tenant's prompt carries."
    )
    violations = "\n".join(
        f"| {arm} | {counts['wrong_tenant']} | {counts['unauthorized_group']} | "
        f"{counts['expired_version']} | {counts['total']} |"
        for arm, counts in domain["visible_evidence_violations"].items()
    )
    return f"""# RAG quality status

Overall: **{status["overall_status"]}**
Tenant release gate: **{gate["status"]}** ({gate["evaluated_count"]}/{gate["gate_count"]} applicable gates evaluated; {len(gate["blockers"])} unmet preconditions, derived from the selection rather than transcribed -- they head the Release blockers list)
External silver: **{silver["status"]}**
Tenant-domain strata: **{domain["status"]}** ({strata["tenants"]} tenants, {strata["group_restricted"]} group-restricted, {strata["inactive"]} withdrawn, {strata["with_an_effective_end"]} past their window, {strata["not_yet_in_force"]} not yet in force)

| metric | external silver | tenant reference | diagnostic |
| --- | ---: | ---: | --- |
{rows}

The 0.275 answerable answer rate is a **held-out retrieval-score threshold proxy**, not an
end-to-end Reviewer result. Its ROC-AUC is {signal["roc_auc"]:.4f} and the best balanced
accuracy over all score cuts is {signal["best_case_balanced_accuracy"]:.4f}; Reviewer semantic
calibration remains **{signal["reviewer_semantic_calibration"]}**.

## What the external-silver figures are counting

Everything downstream of candidate generation only reorders what the funnel returned, so
the share of answerable queries whose gold document is anywhere in the \
{pool["depth"]}-deep pool bounds every Recall@k on this set. That share is \
**{pool["contains_gold"]}/{labels["answerable"]} = {pool["contains_gold_rate"]:.4f}** \
(`{pool["arm"]}`, depth {pool["depth"]}).

Recall thresholds that sit **above** that ceiling and therefore cannot be reached by
ranking better on this label set: **{unreachable}**.

| group | queries | question vs gold title | question vs first hit title | first hit closer |
| --- | ---: | ---: | ---: | ---: |
| gold in the pool | {overlap["in_pool"]["queries"]} | {overlap["in_pool"]["mean_question_vs_gold_title"]} | {overlap["in_pool"]["mean_question_vs_first_title"]} | {overlap["in_pool"]["first_hit_title_closer_rate"]:.4f} |
| gold out of the pool | {overlap["out_of_pool"]["queries"]} | {overlap["out_of_pool"]["mean_question_vs_gold_title"]} | {overlap["out_of_pool"]["mean_question_vs_first_title"]} | {overlap["out_of_pool"]["first_hit_title_closer_rate"]:.4f} |

In the group the funnel does cover, the label and the first hit are topically equivalent.
In the group it does not, the first hit is systematically closer to the question than the
document the labels call correct, and {labels["gold_documents_no_query_retrieves"]} of those
gold documents are retrieved by no query in the set at all. Detail and the rule-selected
examples: `{labels["report"]}`.

### Which ceiling the production prompt actually hits

The external-silver columns above are the *proxy's*. On the production path the arms pack
at most **{packing["packed_max"]}** documents against a budget of
{packing["context_token_budget"]} tokens ({packing["median_parent_tokens"]} tokens at the
median parent; one more would leave {packing["next_parent_headroom_tokens"]["p50"]} at the
median and {packing["next_parent_headroom_tokens"]["p90"]} at p90). The corpus names
{packing["distinct_sources"]} distinct `provenance.source`, and
`SERVICEMIND_RAG_MAX_PARENTS_PER_SOURCE` is {packing["max_parents_per_source"]} — a
diversity guard with no second source to balance. **Attributed to:
{packing["attributed_to"]}** (`{packing["report"]}`).

{packing["reading"]}

This identified a defect, not a lever: the guard's own comment says it exists so that no
single source drowns the others, and with one source there is nothing to drown. Its
*value* is unchanged (`configuration_change_approved` is
**{str(status["production_change"]["configuration_change_approved"]).lower()}**); what
changed is where it applies, so how much evidence one answer carries is now the token
budget's decision rather than an unrelated counter's.

### The recall cutoffs the production path can be measured at

`recall_at_k` counts gold inside the first `k` items the pipeline returned, so an arm that
packed at most `n` documents reports the same number at every cutoff above `n`. Read
against the report the production path itself wrote (`{production["report"]}`, `final_k`
= {production["final_k"]}, {production["context_token_budget"]}-token budget,
{production["distinct_sources"]} distinct source):

| arm | mean packed | min | max | cutoffs the pack cannot tell apart | §4.1 metrics the pack is too short for |
| --- | ---: | ---: | ---: | --- | --- |
{cutoff_rows}

{collapsed_note}{too_short_note}

{production["reading"]}

## §4.1 visible evidence, over a corpus that has strata

The tenant-domain set carries the strata the gate's clauses are about, so its count of
wrong-tenant / unauthorized-group / expired-version evidence could have been nonzero:

| arm | wrong tenant | unauthorized group | expired version | total |
| --- | ---: | ---: | ---: | ---: |
{violations}

{probes["isolation_held"]}/{probes["probes"]} ACL probes held — each asks a protected
document's question once as a caller who must not see it and once as a caller who must,
and {probes["controls_held"]}/{probes["with_a_positive_control"]} controls held, so the
first number is not a pipeline that returned nothing.

{domain["reading"]}

The committed {smoke["documents"]}-document / {smoke["queries"]}-query gold set is
**{smoke["status"]}**. All four retrieval arms saturate Recall@5/10/20 and MRR, while
Precision@5 is {smoke["precision_at_5"]:.4f} and mean dedupe rate is
{smoke["mean_dedupe_rate"]:.4f}. It is valid only as a deterministic regression smoke test.

## Release blockers

{blockers}
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    status = build_status(
        json.loads(PROXY.read_text(encoding="utf-8")),
        json.loads(SMOKE.read_text(encoding="utf-8")),
        json.loads(SELECTION.read_text(encoding="utf-8")),
        json.loads(TENANT_DOMAIN.read_text(encoding="utf-8")),
        json.loads(LABEL_DIAGNOSTIC.read_text(encoding="utf-8")),
        json.loads(PACKING_CEILING.read_text(encoding="utf-8")),
        json.loads(TECHQA.read_text(encoding="utf-8")),
    )
    json_text = json.dumps(status, ensure_ascii=False, indent=2) + "\n"
    markdown = render_markdown(status)
    if args.check:
        matches = (
            OUTPUT_JSON.exists()
            and OUTPUT_MD.exists()
            and OUTPUT_JSON.read_text(encoding="utf-8") == json_text
            and OUTPUT_MD.read_text(encoding="utf-8") == markdown
        )
        print("PASS" if matches else "FAIL", status["overall_status"])
        return 0 if matches else 1
    OUTPUT_JSON.write_text(json_text, encoding="utf-8")
    OUTPUT_MD.write_text(markdown, encoding="utf-8")
    print(status["overall_status"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
