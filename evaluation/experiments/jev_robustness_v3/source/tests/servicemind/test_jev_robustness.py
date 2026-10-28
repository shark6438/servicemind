"""Budget, provenance and held-out evaluation protections; no network calls."""

import asyncio
import copy
import importlib.util
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/{name}.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


M = module("evaluate_jev_robustness")
B = module("build_jev_robustness_dataset")


def sample(identifier, expected, rule, jev, confidence=0.9, split="dev", group="topic"):
    return {
        "id": identifier,
        "expected_route": expected,
        "split": split,
        "dataset": "d.jsonl",
        "group_id": group,
        "rule": {"route": rule},
        "jev": {"route": jev, "confidence": confidence},
    }


def test_dataset_is_machine_labelled_balanced_by_phenomenon_and_grouped():
    cases = B.make_cases()
    assert len(cases) == len({c["id"] for c in cases}) == len({c["text"] for c in cases}) == 500
    assert Counter(c["dataset"] for c in cases) == {
        "routing_semantic_machine.v3.jsonl": 200,
        "routing_scenarios_machine.v3.jsonl": 300,
    }
    assert set(Counter(c["family"] for c in cases).values()) == {100}
    assert all(not c["human_reviewed"] and c["source_kind"] == "synthetic_challenge" for c in cases)
    splits = B.split_groups(cases)
    assert Counter(splits[c["group_id"]] for c in cases) == {"dev": 240, "test": 260}
    assert len(splits) == 25


def test_labels_preserve_write_priority_negation_and_insufficient_information():
    cases = B.make_cases()
    for case in cases:
        if case["family"] == "prohibited_execution":
            assert case["request_write"] and case["expected_route"] == "unsupported"
        elif case["form"] == "explicit_write":
            assert case["request_write"] and case["expected_route"] == "complex_workflow"
        elif case["form"] == "ambiguous_subject":
            assert case["expected_route"] == "complex_workflow"
        elif case["form"] in {"negated_operation", "operation_documentation"}:
            assert not case["request_write"]
            assert case["expected_route"] == "simple_knowledge_query"


def test_every_paid_payload_excludes_labels_and_split_information():
    for case in B.make_cases():
        state = M.wire.payload_for(case, "openrouter")["state"]
        assert state == {"request": case["text"], "request_write": case["request_write"]}


def test_threshold_selector_refuses_test_data_and_uses_fixed_tiebreak():
    dev = [sample("a", "complex_workflow", "simple_data_query", "complex_workflow", 0.9)]
    guard = [sample("b", "complex_workflow", "complex_workflow", "simple_data_query", 0.6)]
    selection = M.select_threshold(dev, guard, [0.5, 0.85, 0.95, 1.01])
    assert selection["threshold"] == 0.85
    assert selection["test_observed_at_selection"] == 0
    assert not selection["candidates"][0]["eligible"]
    with pytest.raises(ValueError, match="dev rows only"):
        M.select_threshold([{**dev[0], "split": "test"}], guard, [0.5])


def test_unsafe_dev_threshold_is_ineligible_even_when_more_accurate():
    dev = [
        sample("a", "complex_workflow", "complex_workflow", "simple_data_query", 0.9),
        sample("b", "simple_data_query", "complex_workflow", "simple_data_query", 0.9),
        sample("c", "simple_data_query", "complex_workflow", "simple_data_query", 0.9),
    ]
    result = M.select_threshold(dev, [], [0.5, 1.01])
    assert result["threshold"] == 1.01
    assert not result["candidates"][0]["eligible"]


@pytest.mark.parametrize("failure", ["timeout", "http_500", "invalid_response"])
def test_missing_provider_decision_has_explicit_degraded_rule_fallback(failure):
    row = {
        "rule": {"route": "complex_workflow"},
        "jev": None,
        "jev_status": failure,
    }
    assert M.hybrid(row, 0.85) == {
        "route": "complex_workflow",
        "source": "rule",
        "degraded": True,
    }


def test_report_binds_every_label_and_rejects_missing_or_extra_rows():
    cases = [{**c, "split": B.split_groups(B.make_cases())[c["group_id"]]} for c in B.make_cases()]
    rows = [
        {**c, "jev": {"route": c["expected_route"]}, "hybrid": {"route": c["expected_route"]}}
        for c in cases
    ]
    M.validate_report(rows, cases)
    with pytest.raises(ValueError, match="Missing"):
        M.validate_report(rows[:-1], cases)
    changed = copy.deepcopy(rows)
    changed[0]["expected_route"] = "unsupported"
    with pytest.raises(ValueError, match="inputs/labels"):
        M.validate_report(changed, cases)
    with pytest.raises(ValueError, match="Missing"):
        M.validate_report([*rows, rows[0]], cases)


def test_grouped_inference_does_not_count_sibling_variants_as_independent():
    rows = [
        sample(str(i), "complex_workflow", "simple_data_query", "complex_workflow", group="one")
        for i in range(20)
    ]
    result = M.comparison(rows, "jev", 200)
    assert result["cluster_count"] == 1
    assert result["cluster_permutation_p"] == 1.0
    assert result["row_mcnemar_p_descriptive_only"] == 2 / 2**20
    assert result["cluster_bootstrap_ci95"] == [1.0, 1.0]


def test_holm_is_monotone_and_capped():
    assert M.holm({"a": 0.01, "b": 0.04, "c": 0.9}) == {"a": 0.03, "b": 0.08, "c": 0.9}
    assert M.holm({"a": 0.8, "b": 0.9}) == {"a": 1.0, "b": 1.0}


def carry(known="0.006630792", unknown="0.002752512"):
    return {
        "priced_usage_usd": known,
        "unresolved_reservations_usd": unknown,
        "http_attempts": 213,
    }


def test_historical_unknown_spend_still_consumes_new_stage_budget(tmp_path):
    with M.CumulativeLedger(tmp_path / "spend.jsonl", carry("0.04", "0.002")) as ledger:
        with pytest.raises(M.wire.ExperimentStop, match="budget_exhausted"):
            ledger.reserve("payload", "openrouter")
        assert ledger.summary()["http_attempts"] == 0


def test_new_stage_does_not_erase_unknown_outcomes_or_retry_them(tmp_path):
    path = tmp_path / "spend.jsonl"
    with M.CumulativeLedger(path, carry()) as ledger:
        ledger.reserve("payload", "openrouter")
    with M.CumulativeLedger(path, carry()) as ledger:
        assert Decimal(ledger.summary()["accounted_usd"]) > Decimal("0.02")
        with pytest.raises(M.wire.ExperimentStop, match="unresolved_previous_attempt"):
            ledger.reserve("payload", "openrouter")


def test_new_stage_enforces_500_attempts_and_settles_reported_cost(tmp_path):
    with M.CumulativeLedger(tmp_path / "spend.jsonl", carry()) as ledger:
        for _ in range(500):
            identifier = ledger.reserve("payload", "openrouter")
            ledger.settle(identifier, 750, Decimal("0.0000315"))
        summary = ledger.summary()
        assert summary["http_attempts"] == summary["request_cap"] == 500
        assert Decimal(summary["stage_priced_usage_usd"]) == Decimal("0.01575")
        assert Decimal(summary["accounted_usd"]) == Decimal("0.025133304")
        with pytest.raises(M.wire.ExperimentStop, match="request_cap_reached"):
            ledger.reserve("payload", "openrouter")


def test_full_frozen_pipeline_selects_before_test_calls_without_real_network(tmp_path, monkeypatch):
    import json

    cases = [{**c, "split": B.split_groups(B.make_cases())[c["group_id"]]} for c in B.make_cases()]
    by_text = {c["text"]: c for c in cases}
    monkeypatch.setattr(M, "STAGE", tmp_path / "stage")
    monkeypatch.setattr(M.wire, "EXPERIMENT", tmp_path / "history")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-only-placeholder")
    spec = tmp_path / "spec.json"
    spec.write_text("{}")
    monkeypatch.setattr(M, "SPEC", spec)
    monkeypatch.setattr(
        M, "verify_spec", lambda: ({"threshold_grid": [0.5, 0.85, 0.99, 1.01]}, cases, [])
    )
    attempts = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(
                200, json={"data": {"limit": 1, "limit_remaining": 0.9, "usage": 0.01}}
            )
        payload = json.loads(request.content)
        case = by_text[payload["state"]["request"]]
        if case["split"] == "test":
            assert (M.STAGE / "selection.json").exists()
            assert len(attempts) >= 240
        attempts.append(case["id"])
        route = case["expected_route"]
        return httpx.Response(
            200,
            json={
                "model": "typesafe/jev-1.14-20261014",
                "usage": {"input_tokens": 750, "output_tokens": 50, "cost": 0.0000315},
                "answers": {
                    "route": {
                        "type": "choice",
                        "choice": route,
                        "confidence": 0.95,
                        "probabilities": {r: int(r == route) for r in M.wire.ROUTES},
                    }
                },
            },
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        M.httpx,
        "AsyncClient",
        lambda **kw: original_client(transport=httpx.MockTransport(handler), **kw),
    )
    assert asyncio.run(M.run(True)) == 0
    assert len(attempts) == len(set(attempts)) == 500
    report = json.loads((M.STAGE / "report.json").read_text())
    assert report["status"] == "COMPLETED"
    M.validate_report(report["rows"], cases)
    assert Decimal(report["spend"]["stage_priced_usage_usd"]) == Decimal(".01575")
    assert len(list((M.STAGE / "receipts").glob("*.json"))) == 500
