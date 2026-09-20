"""Build the single machine-readable truth for current RAG quality evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / "evaluation/reports/phase4_proxy_release_latest.json"
SMOKE = ROOT / "evaluation/reports/phase4_retrieval_latest.json"
OUTPUT_JSON = ROOT / "evaluation/reports/rag_quality_status_latest.json"
OUTPUT_MD = ROOT / "evaluation/reports/rag_quality_status_latest.md"
TARGETS = {
    "recall_at_5": 0.85,
    "recall_at_10": 0.90,
    "mrr_at_10": 0.75,
    "ndcg_at_10": 0.80,
}


def build_status(proxy: dict[str, Any], smoke: dict[str, Any]) -> dict[str, Any]:
    gates = proxy["gates"]
    if not all(
        item.get("applicable") is False and item.get("passed") is None for item in gates.values()
    ):
        raise ValueError("tenant release gates must remain explicitly not evaluated")
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
            "status": "NOT_EVALUATED",
            "gate_count": len(gates),
            "evaluated_count": sum(item["applicable"] is True for item in gates.values()),
            "required_input": "tenant-domain release queries with independent human qrels",
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
            "tenant-domain human qrels are unavailable",
            "Reviewer answerability and abstention correctness are not calibrated on domain labels",
            "production evidence/context-cap impact has no non-synthetic observations",
        ],
    }


def render_markdown(status: dict[str, Any]) -> str:
    silver = status["external_silver"]
    signal = status["answerability_signal"]
    smoke = status["committed_gold"]
    rows = "\n".join(
        f"| {name} | {value:.4f} | {silver['reference_targets'][name]:.2f} | below |"
        for name, value in silver["metrics"].items()
    )
    blockers = "\n".join(f"- {item}" for item in status["release_blockers"])
    return f"""# RAG quality status

Overall: **{status["overall_status"]}**  
Tenant release gate: **{status["tenant_release_gate"]["status"]}** (0/{status["tenant_release_gate"]["gate_count"]} applicable gates evaluated)  
External silver: **{silver["status"]}**

| metric | external silver | tenant reference | diagnostic |
| --- | ---: | ---: | --- |
{rows}

The 0.275 answerable answer rate is a **held-out retrieval-score threshold proxy**, not an
end-to-end Reviewer result. Its ROC-AUC is {signal["roc_auc"]:.4f} and the best balanced
accuracy over all score cuts is {signal["best_case_balanced_accuracy"]:.4f}; Reviewer semantic
calibration remains **{signal["reviewer_semantic_calibration"]}**.

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
