"""Offline statistics for the Jev routing comparison; reads a run report and spends nothing.

The measurement script (``compare_jev_routing.py``) answers "how many were right". This
one answers the questions a replacement decision actually needs: is the difference larger
than sampling noise, how wide is that difference, and what happens when Jev is allowed to
abstain. It never calls a provider, never rewrites a label, and never picks the operating
point for you -- it reports the whole risk/coverage curve and the pre-registered grid.

Input is an existing ``report.json``; nothing else is read and nothing is written outside
the output directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import statistics
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = ROOT / "evaluation/experiments/jev_router_2026-10-28"
DEFAULT_GRID = (0.0, 0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 0.99)
DANGEROUS_EXPECTATIONS = ("complex_workflow", "unsupported")
FAST_PATHS = ("simple_data_query", "simple_knowledge_query")


def mcnemar_exact(improved: int, regressed: int) -> float:
    """Two-sided exact McNemar p-value for ``improved`` vs ``regressed`` discordant pairs."""
    total = improved + regressed
    if total == 0:
        return 1.0
    tail = math.fsum(math.comb(total, index) for index in range(min(improved, regressed) + 1))
    return min(1.0, 2 * tail / (2**total))


def paired_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows where both arms produced a decision; the only rows a paired test may use."""
    return [row for row in rows if row.get("rule") is not None and row.get("jev") is not None]


def accuracy(rows: list[dict[str, Any]], arm: str) -> float | None:
    if not rows:
        return None
    return sum(row[arm]["route"] == row["expected_route"] for row in rows) / len(rows)


def discordant_counts(rows: list[dict[str, Any]], left: str, right: str) -> tuple[int, int]:
    improved = sum(
        row[right]["route"] == row["expected_route"] != row[left]["route"] for row in rows
    )
    regressed = sum(
        row[left]["route"] == row["expected_route"] != row[right]["route"] for row in rows
    )
    return improved, regressed


def bootstrap_delta_ci(
    rows: list[dict[str, Any]],
    left: str,
    right: str,
    iterations: int,
    seed: int,
) -> dict[str, float] | None:
    """Percentile bootstrap of ``accuracy(right) - accuracy(left)`` over paired rows."""
    if not rows or iterations <= 0:
        return None
    generator = random.Random(seed)
    sample_size = len(rows)
    deltas: list[float] = []
    for _ in range(iterations):
        sample = [rows[generator.randrange(sample_size)] for _ in range(sample_size)]
        deltas.append(accuracy(sample, right) - accuracy(sample, left))  # type: ignore[operator]
    deltas.sort()
    low = deltas[max(0, math.floor(0.025 * iterations) - 1)]
    high = deltas[min(iterations - 1, math.ceil(0.975 * iterations) - 1)]
    return {"low": low, "high": high, "mean": statistics.fmean(deltas)}


def dangerous_downgrades(rows: list[dict[str, Any]], arm: str) -> dict[str, int]:
    dangerous = [row for row in rows if row["expected_route"] in DANGEROUS_EXPECTATIONS]
    downgrade = sum(row[arm]["route"] in FAST_PATHS for row in dangerous)
    return {"count": downgrade, "denominator": len(dangerous)}


def hybrid_route(row: dict[str, Any], threshold: float) -> str:
    """Arm H: Jev decides above ``threshold``; the rule router decides below it."""
    if row["jev"]["confidence"] >= threshold:
        return row["jev"]["route"]
    return row["rule"]["route"]


def risk_coverage(rows: list[dict[str, Any]], grid: tuple[float, ...]) -> list[dict[str, Any]]:
    curve: list[dict[str, Any]] = []
    for threshold in grid:
        decided = [row for row in rows if row["jev"]["confidence"] >= threshold]
        correct = sum(hybrid_route(row, threshold) == row["expected_route"] for row in rows)
        downgrade = sum(
            hybrid_route(row, threshold) in FAST_PATHS
            for row in rows
            if row["expected_route"] in DANGEROUS_EXPECTATIONS
        )
        curve.append(
            {
                "threshold": threshold,
                "coverage": len(decided) / len(rows) if rows else None,
                "accuracy": correct / len(rows) if rows else None,
                "jev_decided": len(decided),
                "dangerous_fast_path_count": downgrade,
            }
        )
    return curve


def summarize(rows: list[dict[str, Any]], iterations: int, seed: int) -> dict[str, Any]:
    paired = paired_rows(rows)
    improved, regressed = discordant_counts(paired, "rule", "jev")
    rule_accuracy = accuracy(paired, "rule")
    jev_accuracy = accuracy(paired, "jev")
    delta = None
    if rule_accuracy is not None and jev_accuracy is not None:
        delta = jev_accuracy - rule_accuracy
    return {
        "planned": len(rows),
        "paired": len(paired),
        "rule_correct": sum(row["rule"]["route"] == row["expected_route"] for row in paired),
        "jev_correct": sum(row["jev"]["route"] == row["expected_route"] for row in paired),
        "rule_accuracy": rule_accuracy,
        "jev_accuracy": jev_accuracy,
        "delta_jev_minus_rule": delta,
        "delta_ci95": bootstrap_delta_ci(paired, "rule", "jev", iterations, seed),
        "mcnemar": {
            "improved": improved,
            "regressed": regressed,
            "p_value": mcnemar_exact(improved, regressed),
        },
        "dangerous": {
            "rule": dangerous_downgrades(paired, "rule"),
            "jev": dangerous_downgrades(paired, "jev"),
        },
    }


def build_analysis(
    report: dict[str, Any], grid: tuple[float, ...], iterations: int, seed: int
) -> dict[str, Any]:
    rows = report["rows"]
    groups = sorted({row["dataset"] for row in rows})
    families = sorted({row.get("family", "unknown") for row in rows})
    return {
        "source_status": report.get("status"),
        "source_updated_at": report.get("updated_at"),
        "method": {
            "mcnemar": "two-sided exact binomial over discordant pairs",
            "interval": f"paired percentile bootstrap, {iterations} iterations, seed {seed}",
            "hybrid": "Jev decides when confidence >= threshold, else the rule router",
            "not_measured": "no retrieval, generation, review, writes or end-to-end cost",
        },
        "overall": summarize(rows, iterations, seed),
        "by_dataset": {
            name: summarize([row for row in rows if row["dataset"] == name], iterations, seed)
            for name in groups
        },
        "by_family": {
            name: summarize(
                [row for row in rows if row.get("family", "unknown") == name], iterations, seed
            )
            for name in families
        },
        "risk_coverage": {
            name: risk_coverage(paired_rows([row for row in rows if row["dataset"] == name]), grid)
            for name in groups
        },
    }


def markdown(analysis: dict[str, Any]) -> str:
    lines = [
        "# Jev routing analysis",
        "",
        f"Source status: **{analysis['source_status']}** "
        f"({analysis['source_updated_at']}); statistics only, no new calls.",
        "",
        "## Accuracy, paired samples",
        "",
        "| Group | n | Rule | Jev | Δ (Jev − Rule) | 95% CI | McNemar +/− | p |",
        "| --- | ---: | ---: | ---: | ---: | --- | --- | ---: |",
    ]
    groups = [("overall", analysis["overall"]), *analysis["by_dataset"].items()]
    for name, group in groups:
        interval = group["delta_ci95"]
        ci = "n/a" if interval is None else f"[{interval['low']:+.3f}, {interval['high']:+.3f}]"
        delta = group["delta_jev_minus_rule"]
        lines.append(
            f"| {name} | {group['paired']} | {group['rule_accuracy']:.3f} "
            f"| {group['jev_accuracy']:.3f} | {delta:+.3f} | {ci} "
            f"| {group['mcnemar']['improved']}/{group['mcnemar']['regressed']} "
            f"| {group['mcnemar']['p_value']:.2e} |"
        )
    lines += [
        "",
        "## Dangerous downgrades (complex/unsupported answered on a fast path)",
        "",
        "| Group | Rule | Jev |",
        "| --- | ---: | ---: |",
    ]
    for name, group in groups:
        rule, jev = group["dangerous"]["rule"], group["dangerous"]["jev"]
        lines.append(
            f"| {name} | {rule['count']}/{rule['denominator']} | {jev['count']}/{jev['denominator']} |"
        )
    lines += [
        "",
        "## Hybrid risk/coverage (Jev above threshold, rules below)",
        "",
        "Coverage is the share of rows Jev decided; accuracy is over all paired rows.",
        "",
    ]
    for name, curve in analysis["risk_coverage"].items():
        lines += [
            f"### {name}",
            "",
            "| τ | coverage | accuracy | dangerous |",
            "| ---: | ---: | ---: | ---: |",
        ]
        for point in curve:
            if point["accuracy"] is None:
                continue
            lines.append(
                f"| {point['threshold']:.2f} | {point['coverage']:.3f} "
                f"| {point['accuracy']:.3f} | {point['dangerous_fast_path_count']} |"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_preregistration(path: Path, rows: list[dict[str, Any]]) -> list[str]:
    """Every declared dataset must exist, be frozen by digest, and cover what was run."""
    spec = json.loads(path.read_text(encoding="utf-8"))
    declared = spec.get("datasets") or spec.get("strata") or []
    problems: list[str] = []
    if not declared:
        problems.append("preregistration declares no datasets")
    names = set()
    for entry in declared:
        relative = entry.get("path")
        if not relative:
            problems.append("dataset entry without a path")
            continue
        target = Path(relative) if Path(relative).is_absolute() else ROOT / relative
        names.add(Path(relative).name)
        if not target.is_file():
            problems.append(f"declared dataset missing: {relative}")
            continue
        expected = entry.get("sha256")
        if not expected:
            problems.append(f"dataset not frozen by digest: {relative}")
            continue
        actual = sha256_of(target)
        if actual != expected:
            problems.append(
                f"dataset digest changed: {relative} ({expected[:12]} != {actual[:12]})"
            )
    for name in sorted({row["dataset"] for row in rows} - names):
        problems.append(f"report used an undeclared dataset: {name}")
    return problems


def latest_report() -> Path:
    candidates = sorted(EXPERIMENT.glob("run_*/report.json"))
    if not candidates:
        raise SystemExit(f"no run reports under {EXPERIMENT}")
    return candidates[-1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--iterations", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=20261028)
    parser.add_argument(
        "--grid", type=float, nargs="+", default=list(DEFAULT_GRID), help="hybrid thresholds"
    )
    parser.add_argument(
        "--preregistration",
        type=Path,
        default=None,
        help="refuse (exit 3) when a declared dataset is missing, unfrozen or changed",
    )
    arguments = parser.parse_args()
    path = arguments.report or latest_report()
    report = json.loads(path.read_text(encoding="utf-8"))
    analysis = build_analysis(report, tuple(arguments.grid), arguments.iterations, arguments.seed)
    problems: list[str] = []
    if arguments.preregistration is not None:
        if not arguments.preregistration.is_file():
            print(f"preregistration not found: {arguments.preregistration}")
            return 2
        problems = verify_preregistration(arguments.preregistration, report["rows"])
        analysis["preregistration"] = {
            "path": str(arguments.preregistration.relative_to(ROOT))
            if arguments.preregistration.is_relative_to(ROOT)
            else str(arguments.preregistration),
            "ok": not problems,
            "problems": problems,
        }
    destination = arguments.out or path.parent
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "analysis.json").write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (destination / "analysis.md").write_text(markdown(analysis), encoding="utf-8")
    print(f"wrote {destination / 'analysis.json'}")
    if problems:
        for problem in problems:
            print(f"preregistration: {problem}")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
