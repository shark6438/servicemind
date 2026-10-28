"""The resume evidence must come from measured decisions, with no new requests."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "replay_jev_routing", ROOT / "scripts/replay_jev_routing.py"
)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def source():
    return {
        "status": "COMPLETED",
        "rows": [
            {
                "id": "a",
                "text": "工单 2",
                "request_write": False,
                "dataset": "d",
                "expectation_strength": "contract",
                "expected_route": "complex_workflow",
                "rule": {"route": "complex_workflow", "latency_ms": 0.02},
                "jev": {"route": "simple_data_query", "confidence": 0.4, "latency_ms": 1200},
            }
        ],
    }


def test_replay_uses_real_decisions_and_marks_its_exploratory_status(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("offline replay attempted network access")

    monkeypatch.setattr(M.hybrid.__globals__["httpx"], "AsyncClient", forbidden)
    result = M.build(source(), 0.85)
    assert result["status"] == "EXPLORATORY_REPLAY"
    assert result["new_api_calls"] == 0
    assert result["overall"]["hybrid"]["correct"] == 1
    assert result["overall"]["jev"]["dangerous"] == 1
    assert result["overall"]["hybrid"]["dangerous"] == 0


def test_replay_refuses_missing_decisions_instead_of_imputing_success():
    report = source()
    report["rows"][0]["jev"] = None
    with pytest.raises(ValueError, match="Missing"):
        M.build(report, 0.85)


def test_replay_refuses_incomplete_provider_batches():
    report = source()
    report["status"] = "STOPPED_http_403"
    with pytest.raises(ValueError, match="completed"):
        M.build(report, 0.85)
