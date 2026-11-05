"""Check the arithmetic used for the independently recorded Worker observations."""

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "worker_capacity_benchmark",
    Path(__file__).resolve().parents[2] / "scripts/benchmark_worker_capacity.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_percentile_interpolates_without_reordering_raw_observations() -> None:
    values = [20.0, 10.0]
    assert MODULE.percentile(values, 0.5) == 15
    assert MODULE.percentile(values, 0.95) == 19.5
    assert values == [20, 10]
    with pytest.raises(ValueError, match="requires observations"):
        MODULE.percentile([], 0.95)


def test_summary_distinguishes_header_queue_and_run_latency() -> None:
    rows = [
        {
            "created_at_epoch": 0,
            "started_at_epoch": 0.1,
            "finished_at_epoch": 1,
            "status": "succeeded",
            "effects": 1,
            "attempts": 1,
        },
        {
            "created_at_epoch": 0.5,
            "started_at_epoch": 0.7,
            "finished_at_epoch": 1.5,
            "status": "succeeded",
            "effects": 1,
            "attempts": 1,
        },
    ]
    requests = [
        {"sent_at_epoch": 0, "response_header_ms": 10},
        {"sent_at_epoch": 0.5, "response_header_ms": 20},
    ]
    report = MODULE.Experiment.summarize(rows, requests)
    assert report["elapsed_seconds"] == 1.5
    assert report["throughput_per_second"] == 1.333333
    assert report["drain_after_last_submission_seconds"] == 1
    assert report["api_response_header_ms"]["p95"] == 19.5
    assert report["queue_wait_seconds"]["p95"] == 0.195
    assert report["run_latency_seconds"]["p95"] == 1


def test_completion_windows_do_not_count_arrivals_as_completions() -> None:
    rows = [
        {
            "created_at_epoch": index,
            "started_at_epoch": index + 1,
            "finished_at_epoch": finished,
            "status": "succeeded",
            "effects": 1,
            "attempts": 1,
        }
        for index, finished in enumerate([6, 11, 16])
    ]
    requests = [{"sent_at_epoch": index, "response_header_ms": 5} for index in range(3)]
    report = MODULE.Experiment.summarize(rows, requests)
    assert report["steady_five_second_completion_windows"] == [
        {"from_second": 5, "completed": 1},
        {"from_second": 10, "completed": 1},
        {"from_second": 15, "completed": 1},
    ]
