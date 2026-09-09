"""Evaluate the deterministic Phase 3 router against the committed golden dataset."""

import json
from pathlib import Path

from servicemind.evaluation.routing import evaluate_cases, load_cases

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET = PROJECT_ROOT / "evaluation" / "routing" / "routing.jsonl"
REPORT = PROJECT_ROOT / "evaluation" / "reports" / "phase3_routing_latest.json"


def main() -> None:
    report = evaluate_cases(load_cases(DATASET))
    report["dataset"] = str(DATASET.relative_to(PROJECT_ROOT))
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["samples"] < 100:
        raise SystemExit("Routing dataset must contain at least 100 samples")
    if report["accuracy"] < 0.95:
        raise SystemExit("Routing accuracy is below the Phase 3 gate")


if __name__ == "__main__":
    main()
