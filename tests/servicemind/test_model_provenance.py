from copy import deepcopy

import pytest

from servicemind.evaluation.model_provenance import grade_model_provenance


def evidence():
    call = {
        "request_id": "r1",
        "run_id": "run1",
        "provider": "openai",
        "model": "local",
        "agent_role": "analysis",
        "prompt_hash": "abc",
        "attempts": 1,
        "status": "succeeded",
    }
    wire = {
        "request_id": "r1",
        "run_id": "run1",
        "model": "local",
        "response_model": "local",
        "agent_role": "analysis",
        "prompt_hash": "abc",
        "http_status": 200,
        "transport_status": "completed",
    }
    return call, wire


def verdict(call, wire):
    return grade_model_provenance(run_id="run1", calls=[call], wires=[wire], expected_model="local")


def test_independent_trace_admits_a_matching_actual_model_call():
    assert verdict(*evidence())["verdict"] == "VALID"


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_id", None),
        ("run_id", "foreign-run"),
        ("model", "simulator"),
        ("response_model", "other-model"),
        ("agent_role", "supervisor"),
        ("prompt_hash", "changed"),
        ("transport_status", "cancelled"),
        ("http_status", 401),
    ],
)
def test_unmatched_or_changed_model_evidence_cannot_certify_success(field, value):
    call, wire = evidence()
    wire[field] = value
    assert verdict(call, wire)["verdict"] == "BLOCKED"


def test_missing_ledger_and_hidden_transport_retries_fail_closed():
    call, wire = evidence()
    assert (
        grade_model_provenance(run_id="run1", calls=[], wires=[wire], expected_model="local")[
            "verdict"
        ]
        == "BLOCKED"
    )
    assert (
        grade_model_provenance(
            run_id="run1", calls=[call], wires=[wire, deepcopy(wire)], expected_model="local"
        )["verdict"]
        == "BLOCKED"
    )


def test_failed_model_inference_is_valid_evidence_but_not_a_business_pass():
    call, wire = evidence()
    call["status"] = "failed"
    wire.update(transport_status="cancelled", response_model=None)
    assert verdict(call, wire)["verdict"] == "VALID"


def test_an_in_flight_request_cannot_be_hidden_by_an_unfinished_trace():
    call, wire = evidence()
    started = {**wire, "request_id": "still-running"}
    result = grade_model_provenance(
        run_id="run1", calls=[call], wires=[wire], starts=[wire, started], expected_model="local"
    )
    assert result["verdict"] == "BLOCKED"
