"""Replay existing provider decisions with a fixed exploratory hybrid threshold.

No API calls, credentials or new labels. This is retrospective analysis of an
already exposed dataset, not independent validation of the hybrid threshold.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from evaluate_jev_robustness import hybrid, metrics

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / (
    "evaluation/experiments/jev_router_2026-10-28/run_20261028T062828Z_89823e/report.json"
)
DESTINATION = ROOT / "evaluation/experiments/jev_robustness_v3/replay"


def summarize(rows: list[dict]) -> dict:
    arms = {arm: metrics(rows, arm) for arm in ("rule", "jev", "hybrid")}

    def correct_before(row):
        return row["rule"]["route"] == row["expected_route"]

    def correct_after(row):
        return row["hybrid"]["route"] == row["expected_route"]

    return {
        **arms,
        "hybrid_jev_selected": sum(row["hybrid"]["source"] == "jev" for row in rows),
        "hybrid_improved_ids": [
            row["id"] for row in rows if correct_after(row) and not correct_before(row)
        ],
        "hybrid_regressed_ids": [
            row["id"] for row in rows if correct_before(row) and not correct_after(row)
        ],
    }


def build(report: dict, threshold: float) -> dict:
    if report["status"] != "COMPLETED" or not report["rows"]:
        raise ValueError("Replay needs a completed provider batch")
    if any(row.get("jev") is None for row in report["rows"]):
        raise ValueError("Missing Jev observation")
    rows = [{**row, "hybrid": hybrid(row, threshold)} for row in report["rows"]]
    return {
        "status": "EXPLORATORY_REPLAY",
        "threshold": threshold,
        "new_api_calls": 0,
        "new_api_cost_usd": "0",
        "overall": summarize(rows),
        "by_dataset": {
            name: summarize([r for r in rows if r["dataset"] == name])
            for name in sorted({r["dataset"] for r in rows})
        },
        "by_label_strength": {
            name: summarize([r for r in rows if r["expectation_strength"] == name])
            for name in sorted({r["expectation_strength"] for r in rows})
        },
        "rows": rows,
        "limitations": [
            "The threshold and source results were previously observed; this is not a new held-out test.",
            "The semantic probe is assistant-authored, unreviewed and synthetic.",
            "All hybrid rows still require Jev inference to obtain its confidence; coverage is not API savings.",
            "No independent human annotation, production traffic, deployment or end-to-end savings measured.",
        ],
    }


def markdown(analysis: dict) -> str:
    lines = [
        "# 已有 Jev 观测的探索性回放",
        "",
        f"固定阈值：{analysis['threshold']}。新增 API 调用：0，新增费用：0。",
        "阈值及源结果已经暴露；本报告不构成独立留出验证。",
        "",
        "| 数据 | n | 规则正确 | Jev 正确 | 混合正确 | 危险误分：规则 / Jev / 混合 |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for name, group in analysis["by_dataset"].items():
        r, j, h = (group[arm] for arm in ("rule", "jev", "hybrid"))
        lines.append(
            f"| {name} | {r['n']} | {r['correct']} | {j['correct']} | {h['correct']} "
            f"| {r['dangerous']} / {j['dangerous']} / {h['dangerous']} |"
        )
    lines += ["", "## 解释边界", "", *[f"- {note}" for note in analysis["limitations"]]]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=SOURCE)
    parser.add_argument("--out", type=Path, default=DESTINATION)
    parser.add_argument("--threshold", type=float, default=0.85)
    arguments = parser.parse_args()
    if not 0 <= arguments.threshold <= 1.01:
        parser.error("threshold must be within [0, 1.01]")
    report = json.loads(arguments.report.read_text())
    analysis = build(report, arguments.threshold)
    analysis["source_sha256"] = hashlib.sha256(arguments.report.read_bytes()).hexdigest()
    arguments.out.mkdir(parents=True, exist_ok=False)
    (arguments.out / "report.json").write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2) + "\n"
    )
    (arguments.out / "report.md").write_text(markdown(analysis))
    print(f"EXPLORATORY_REPLAY: {arguments.out}; no API calls")


if __name__ == "__main__":
    main()
