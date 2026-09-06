import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from servicemind.domain.routing import RouteType
from servicemind.orchestration.router import FastPathRouter


def load_cases(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def evaluate_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    router = FastPathRouter()
    confusion: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    failures: list[dict[str, Any]] = []
    fast_path_total = 0
    fast_path_correct = 0
    unnecessary = 0
    complex_total = 0
    complex_misroutes = 0
    correct = 0
    fast_routes = {
        RouteType.SIMPLE_DATA_QUERY.value,
        RouteType.SIMPLE_KNOWLEDGE_QUERY.value,
    }
    for case in cases:
        expected = str(case["expected_route"])
        actual = router.route(
            str(case["text"]), request_write=bool(case["request_write"])
        ).route.value
        confusion[expected][actual] += 1
        if actual == expected:
            correct += 1
        else:
            failures.append({**case, "actual_route": actual})
        if expected in fast_routes:
            fast_path_total += 1
            fast_path_correct += int(actual == expected)
            unnecessary += int(actual == RouteType.COMPLEX_WORKFLOW.value)
        if expected == RouteType.COMPLEX_WORKFLOW.value:
            complex_total += 1
            complex_misroutes += int(actual != expected)

    total = len(cases)
    return {
        "samples": total,
        "accuracy": correct / total if total else 0,
        "fast_path_recall": fast_path_correct / fast_path_total if fast_path_total else 0,
        "unnecessary_agent_rate": unnecessary / fast_path_total if fast_path_total else 0,
        "complex_task_misroute_rate": complex_misroutes / complex_total if complex_total else 0,
        "confusion_matrix": {
            expected: dict(predictions) for expected, predictions in confusion.items()
        },
        "failures": failures,
    }
