from pathlib import Path

from servicemind.evaluation.routing import evaluate_cases, load_cases


def test_routing_golden_dataset_meets_phase3_gate() -> None:
    cases = load_cases(Path("evaluation/routing/routing.jsonl"))
    report = evaluate_cases(cases)
    assert len(cases) >= 100
    assert report["accuracy"] >= 0.95, report["failures"]
    assert report["fast_path_recall"] >= 0.95
    assert report["unnecessary_agent_rate"] <= 0.05
    assert report["complex_task_misroute_rate"] <= 0.05
