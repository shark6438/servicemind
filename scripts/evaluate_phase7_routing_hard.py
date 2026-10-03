"""Grade the router on a discriminating set, not on the golden set it was written against.

**Why a second set.** ``evaluation/routing/routing.jsonl`` has 107 cases and the router
scores 107/107 on it. A perfect score on the set the rules were written against is not
evidence that the rules generalise -- the rules and the cases were edited together, and the
comments in ``orchestration/router.py`` cite those very cases as the boundary they pin. What
a reader needs is a second set written from the *contract* rather than from the cases: each
row says which documented rule must decide it, and separately whether the row is decidable
by that contract alone (``contract``) or is a design trade-off the contract does not settle
(``boundary``). The two are reported apart, because a boundary disagreement is a question
for the maintainer, not a defect.

**What is measured.** The router is a pure function of ``(text, request_write)``, so this
batch needs no stack and can run in CI. For each row it records the route, the reason code
and the confidence, and it grades the route against the row's declared expectation. It also
records, for every mismatch, the row's own rationale, so the report says not only that the
route differed but which rule the author expected to fire.

**What it is not.** It does not judge whether the router is *right* to behave as it does. A
``boundary`` mismatch is recorded as a disagreement between the contract's edges and the
implementation's, with both sides written down; resolving it is a decision, and this script
does not make decisions.

Usage:

    uv run python scripts/evaluate_phase7_routing_hard.py [--check]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from servicemind.evaluation.routing import evaluate_cases, load_cases
from servicemind.orchestration.router import FastPathRouter

REPO_ROOT = Path(__file__).resolve().parents[1]
ROUTING = REPO_ROOT / "evaluation" / "routing"
GOLDEN = ROUTING / "routing.jsonl"
HARD = ROUTING / "routing_hard.v1.jsonl"
OUT_JSON = REPO_ROOT / "evaluation" / "reports" / "phase7_routing_hard_latest.json"
OUT_MD = REPO_ROOT / "evaluation" / "reports" / "phase7_routing_hard_latest.md"


def grade(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-case route and correctness, plus the family and strength breakdowns."""
    router = FastPathRouter()
    rows: list[dict[str, Any]] = []
    for case in cases:
        decision = router.route(str(case["text"]), request_write=bool(case["request_write"]))
        expected = str(case["expected_route"])
        rows.append(
            {
                "id": case["id"],
                "text": case["text"],
                "request_write": bool(case["request_write"]),
                "family": case["family"],
                "expectation_strength": case["expectation_strength"],
                "expected_route": expected,
                "actual_route": decision.route.value,
                "reason_code": decision.reason_code,
                "confidence": decision.confidence,
                "correct": decision.route.value == expected,
                "rationale": case["rationale"],
            }
        )

    def rate(subset: list[dict[str, Any]]) -> dict[str, Any]:
        good = sum(1 for row in subset if row["correct"])
        return {"cases": len(subset), "correct": good, "accuracy": round(good / len(subset), 4)}

    by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_strength: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_family[row["family"]].append(row)
        by_strength[row["expectation_strength"]].append(row)

    return {
        "rows": rows,
        "overall": rate(rows),
        "by_family": {name: rate(subset) for name, subset in sorted(by_family.items())},
        "by_strength": {name: rate(subset) for name, subset in sorted(by_strength.items())},
        "mismatches": [row for row in rows if not row["correct"]],
    }


def build() -> dict[str, Any]:
    hard = load_cases(HARD)
    golden = load_cases(GOLDEN)
    graded = grade(hard)
    golden_result = evaluate_cases(golden)
    return {
        "schema_version": "phase7-routing-hard-v1",
        "golden": {
            "source": str(GOLDEN.relative_to(REPO_ROOT)),
            "cases": golden_result["samples"],
            "accuracy": golden_result["accuracy"],
            "note": (
                "the set the router rules were written against; a perfect score here is "
                "saturation, not evidence of discrimination"
            ),
        },
        "hard": {
            "source": str(HARD.relative_to(REPO_ROOT)),
            "overall": graded["overall"],
            "by_family": graded["by_family"],
            "by_strength": graded["by_strength"],
        },
        "mismatches": graded["mismatches"],
        "reading": (
            "contract rows are those the router's own documented rules decide outright; "
            "boundary rows are design trade-offs the contract does not settle. A contract "
            "mismatch is an implementation defect; a boundary mismatch is a decision for a "
            "maintainer. The two are reported apart so neither hides behind the other."
        ),
    }


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Router hard-case evaluation",
        "",
        f"- golden set: {payload['golden']['cases']} cases, accuracy "
        f"{payload['golden']['accuracy']:.4f} ({payload['golden']['source']})",
        f"- hard set: {payload['hard']['overall']['cases']} cases, accuracy "
        f"{payload['hard']['overall']['accuracy']:.4f} ({payload['hard']['source']})",
        "",
        "## By expectation strength",
        "",
        "| strength | cases | correct | accuracy |",
        "|---|---|---|---|",
    ]
    for name, row in payload["hard"]["by_strength"].items():
        lines.append(f"| {name} | {row['cases']} | {row['correct']} | {row['accuracy']:.4f} |")
    lines += [
        "",
        "## By family",
        "",
        "| family | cases | correct | accuracy |",
        "|---|---|---|---|",
    ]
    for name, row in payload["hard"]["by_family"].items():
        lines.append(f"| {name} | {row['cases']} | {row['correct']} | {row['accuracy']:.4f} |")
    lines += ["", "## Mismatches", ""]
    if not payload["mismatches"]:
        lines.append("none")
    for row in payload["mismatches"]:
        lines += [
            f"### {row['id']} ({row['expectation_strength']}, {row['family']})",
            "",
            f"- text: `{row['text']}` (request_write={row['request_write']})",
            f"- expected `{row['expected_route']}`, got `{row['actual_route']}` "
            f"via `{row['reason_code']}` (confidence {row['confidence']})",
            f"- why the expectation: {row['rationale']}",
            "",
        ]
    lines += ["## Reading", "", payload["reading"], ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify the markdown matches the json")
    args = parser.parse_args()

    payload = build()
    markdown = render_markdown(payload)

    if args.check:
        if not OUT_JSON.exists() or not OUT_MD.exists():
            print("no report on disk to check", file=sys.stderr)
            return 2
        on_disk = json.loads(OUT_JSON.read_text(encoding="utf-8"))
        problems = []
        if on_disk.get("hard") != payload["hard"]:
            problems.append("the hard-set numbers moved since the report was written")
        if OUT_MD.read_text(encoding="utf-8") != markdown:
            problems.append("the markdown does not match the json")
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1 if problems else 0

    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    OUT_MD.write_text(markdown, encoding="utf-8")
    print(
        json.dumps(
            {
                "golden": payload["golden"],
                "hard": payload["hard"],
                "mismatch_ids": [row["id"] for row in payload["mismatches"]],
            },
            ensure_ascii=False,
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
