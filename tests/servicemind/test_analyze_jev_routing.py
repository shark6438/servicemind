"""Statistics for the routing comparison must be exact, seeded, and spend nothing."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "analyze_jev_routing", ROOT / "scripts/analyze_jev_routing.py"
)
assert SPEC is not None and SPEC.loader is not None
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def row(identifier, expected, rule, jev, confidence, dataset="d.jsonl", family="f"):
    return {
        "id": identifier,
        "expected_route": expected,
        "dataset": dataset,
        "family": family,
        "rule": {"route": rule, "confidence": 0.99, "latency_ms": 0.01},
        "jev": {"route": jev, "confidence": confidence, "latency_ms": 1000.0},
    }


def test_mcnemar_matches_the_exact_binomial_tail():
    assert M.mcnemar_exact(34, 0) == 2 / 2**34
    assert M.mcnemar_exact(0, 0) == 1.0
    assert M.mcnemar_exact(5, 5) == 1.0
    # Symmetric in its arguments: swapping arms cannot change the p-value.
    assert M.mcnemar_exact(3, 9) == M.mcnemar_exact(9, 3)


def test_bootstrap_is_seeded_and_brackets_the_point_estimate():
    rows = [
        row(f"r{i}", "complex_workflow", "simple_data_query", "complex_workflow", 0.95)
        for i in range(10)
    ]
    rows += [
        row(f"s{i}", "simple_data_query", "simple_data_query", "simple_data_query", 0.95)
        for i in range(10)
    ]
    first = M.bootstrap_delta_ci(rows, "rule", "jev", 500, 7)
    second = M.bootstrap_delta_ci(rows, "rule", "jev", 500, 7)
    assert first == second
    assert first is not None
    assert first["low"] <= first["high"]
    assert 0.0 <= first["low"] and first["high"] <= 1.0


def test_hybrid_falls_back_to_the_rule_below_the_threshold():
    confident = row("a", "complex_workflow", "simple_data_query", "complex_workflow", 0.9)
    unsure = row("b", "complex_workflow", "complex_workflow", "simple_data_query", 0.3)
    assert M.hybrid_route(confident, 0.5) == "complex_workflow"
    assert M.hybrid_route(unsure, 0.5) == "complex_workflow"
    assert M.hybrid_route(unsure, 0.0) == "simple_data_query"


def test_risk_coverage_shrinks_with_the_threshold_and_counts_dangerous_downgrades():
    rows = [
        row("a", "complex_workflow", "complex_workflow", "complex_workflow", 0.9),
        row("b", "complex_workflow", "simple_data_query", "simple_data_query", 0.8),
        row("c", "simple_data_query", "simple_data_query", "simple_data_query", 0.4),
    ]
    curve = M.risk_coverage(M.paired_rows(rows), (0.0, 0.5, 0.85))
    assert [point["jev_decided"] for point in curve] == [3, 2, 1]
    coverages = [point["coverage"] for point in curve]
    assert coverages == sorted(coverages, reverse=True)
    # At threshold 0.0 Jev decides everything: row b becomes a dangerous downgrade.
    assert curve[0]["dangerous_fast_path_count"] == 1
    # At 0.85 row b falls back to the rule, whose own answer for b is the downgrade;
    # the hybrid inherits the rule's error rather than erasing it.
    assert curve[2]["dangerous_fast_path_count"] == 1
    # At 0.85 only row a is decided by Jev; rows b and c fall back to the rule,
    # which is right on c and wrong on b -- two of three correct.
    assert curve[2]["accuracy"] == 2 / 3


def test_summary_uses_paired_rows_and_reports_both_arms():
    rows = [
        row("a", "complex_workflow", "simple_data_query", "complex_workflow", 0.9),
        row("b", "simple_knowledge_query", "simple_knowledge_query", "simple_knowledge_query", 0.9),
        {
            "id": "c",
            "expected_route": "unsupported",
            "dataset": "d.jsonl",
            "family": "f",
            "rule": {"route": "unsupported", "confidence": 0.9, "latency_ms": 0.01},
            "jev": None,
        },
    ]
    summary = M.summarize(rows, iterations=200, seed=1)
    assert summary["planned"] == 3
    assert summary["paired"] == 2
    assert summary["rule_correct"] == 1
    assert summary["jev_correct"] == 2
    assert summary["delta_jev_minus_rule"] == 0.5
    assert summary["mcnemar"]["improved"] == 1
    assert summary["mcnemar"]["regressed"] == 0


def test_cli_writes_analysis_next_to_a_report(tmp_path):
    rows = [row("a", "complex_workflow", "simple_data_query", "complex_workflow", 0.9)]
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps({"status": "complete", "updated_at": "t", "rows": rows}), encoding="utf-8"
    )
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/analyze_jev_routing.py"),
            "--report",
            str(report),
            "--out",
            str(tmp_path / "out"),
            "--iterations",
            "50",
        ],
        check=True,
        capture_output=True,
    )
    analysis = json.loads((tmp_path / "out" / "analysis.json").read_text(encoding="utf-8"))
    assert analysis["overall"]["paired"] == 1
    assert (
        (tmp_path / "out" / "analysis.md")
        .read_text(encoding="utf-8")
        .startswith("# Jev routing analysis")
    )


def frozen_preregistration(tmp_path, digest=None):
    dataset = tmp_path / "d.jsonl"
    dataset.write_text('{"id": "a"}\n', encoding="utf-8")
    spec = {
        "version": "test",
        "datasets": [
            {"path": str(dataset), "sha256": digest or M.sha256_of(dataset), "stratum": "s0"}
        ],
    }
    path = tmp_path / "prereg.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


def test_preregistration_passes_when_every_digest_matches(tmp_path):
    rows = [row("a", "complex_workflow", "complex_workflow", "complex_workflow", 0.9)]
    assert M.verify_preregistration(frozen_preregistration(tmp_path), rows) == []


def test_preregistration_refuses_a_changed_dataset(tmp_path):
    rows = []
    problems = M.verify_preregistration(frozen_preregistration(tmp_path, digest="0" * 64), rows)
    assert any("digest changed" in problem for problem in problems)


def test_preregistration_refuses_an_undeclared_dataset(tmp_path):
    rows = [
        row("a", "complex_workflow", "complex_workflow", "complex_workflow", 0.9, dataset="x.jsonl")
    ]
    problems = M.verify_preregistration(frozen_preregistration(tmp_path), rows)
    assert any("undeclared dataset" in problem for problem in problems)


def test_cli_exits_three_when_preregistration_does_not_hold(tmp_path):
    rows = [
        row(
            "a",
            "complex_workflow",
            "simple_data_query",
            "complex_workflow",
            0.9,
            dataset="other.jsonl",
        )
    ]
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps({"status": "complete", "updated_at": "t", "rows": rows}), encoding="utf-8"
    )
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/analyze_jev_routing.py"),
            "--report",
            str(report),
            "--out",
            str(tmp_path / "out"),
            "--iterations",
            "50",
            "--preregistration",
            str(frozen_preregistration(tmp_path)),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 3
    assert "undeclared dataset" in result.stdout
