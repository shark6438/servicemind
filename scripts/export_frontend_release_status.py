#!/usr/bin/env python3
"""Export a narrow, non-sensitive release snapshot for the operator console."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "evaluation" / "reports"
OUTPUT = ROOT / "frontend" / "public" / "release-status.json"


def _read(name: str) -> dict[str, Any]:
    value = json.loads((REPORTS / name).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{name} must contain a JSON object")
    return value


def build_snapshot() -> dict[str, Any]:
    structure = _read("project_structure_latest.json")
    phase5 = _read("phase5_acceptance_latest.json")
    phase6 = _read("phase6_acceptance_latest.json")
    rag = _read("rag_quality_status_latest.json")
    runtime = _read("servicemind_runtime_latest.json")
    structure_checks = structure.get("checks", {})
    rag_metrics = rag.get("external_silver", {}).get("metrics", {})
    blockers = rag.get("release_blockers", [])
    if not isinstance(structure_checks, dict) or not isinstance(blockers, list) or len(blockers) < 3:
        raise ValueError("release reports do not match the expected schema")
    return {
        "generated_at": phase5["evaluated_at"],
        "release_decision": rag["quality_exception"],
        "structure": {
            "status": structure["status"],
            "checks_passed": sum(value is True for value in structure_checks.values()),
            "checks_total": len(structure_checks),
        },
        "phase5": {
            "status": phase5["result"],
            "tests_passed": phase5.get("tests", {}).get("full_suite", {}).get("passed"),
        },
        "phase6": {
            "status": phase6["result"],
            "tests_passed": phase6.get("tests", {}).get("full_suite", {}).get("passed"),
        },
        "rag": {
            "status": rag["overall_status"],
            "recall_at_5": rag_metrics.get("recall_at_5"),
            "recall_at_10": rag_metrics.get("recall_at_10"),
            "mrr_at_10": rag_metrics.get("mrr_at_10"),
            "ndcg_at_10": rag_metrics.get("ndcg_at_10"),
            "scope": (
                "4 项外部银标点估计均低于租户参考门槛；外部数据分布仅用于诊断，"
                "不能替代租户域人工标注集作出发布裁定。"
            ),
        },
        "runtime": {
            "status": runtime["status"],
            "health_ok": runtime.get("health_http_status") == 200,
        },
        "caveats": [
            "缺少租户域人工相关性标注集（qrels）。",
            "Reviewer 的可答性与弃答正确率尚未在领域标签上完成校准。",
            "生产 evidence 与 context cap 的影响尚无非合成观测样本。",
            "Phase 5 没有非合成生产记忆样本；工程通过不代表生产记忆质量通过。",
            "Phase 6 尚未认证生产负载能力；容量仍是后续发布门禁。",
        ],
    }


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(build_snapshot(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
