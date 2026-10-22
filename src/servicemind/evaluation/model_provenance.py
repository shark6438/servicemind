"""Reconcile model gateway records with an independently observed local HTTP transport."""

from collections import defaultdict
from typing import Any


def grade_model_provenance(
    *,
    run_id: str,
    calls: list[dict[str, Any]],
    wires: list[dict[str, Any]],
    expected_model: str,
    starts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    issues = []
    if not calls:
        issues.append("No durable model invocations were recorded")
    by_request: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for wire in wires:
        if wire.get("run_id") == run_id:
            by_request[str(wire.get("request_id"))].append(wire)
    call_ids = {str(call["request_id"]) for call in calls}
    if len(call_ids) != len(calls):
        issues.append("Duplicate durable request identities")
    if set(by_request) - call_ids:
        issues.append("Wire requests have no matching durable invocation")
    if starts is not None:
        started: dict[str, int] = defaultdict(int)
        for start in starts:
            if start.get("run_id") == run_id:
                started[str(start.get("request_id"))] += 1
        if started != {key: len(value) for key, value in by_request.items()}:
            issues.append("Started requests are still in flight or have missing completion traces")
    for call in calls:
        request_id = str(call["request_id"])
        observed = by_request.get(request_id, [])
        if str(call["run_id"]) != run_id:
            issues.append(f"{request_id}: foreign Run in model ledger")
        if call["provider"] != "openai" or call["model"] != expected_model:
            issues.append(f"{request_id}: unexpected model route")
        if len(observed) != call["attempts"]:
            issues.append(f"{request_id}: wire attempt count differs from ledger")
        if observed and observed[0]["prompt_hash"] != call["prompt_hash"]:
            issues.append(f"{request_id}: first prompt hash differs from ledger")
        for wire in observed:
            if wire["model"] != expected_model or wire.get("agent_role") != call["agent_role"]:
                issues.append(f"{request_id}: model or role changed at transport")
            if wire.get("http_status") == 200 and wire.get("response_model") not in {
                None,
                expected_model,
            }:
                issues.append(f"{request_id}: unexpected response model")
        if call["status"] == "succeeded" and not any(
            wire.get("http_status") == 200
            and wire.get("transport_status") == "completed"
            and wire.get("response_model") == expected_model
            for wire in observed
        ):
            issues.append(f"{request_id}: no completed inference response for succeeded call")
        if call["status"] == "cache_hit":
            issues.append(f"{request_id}: cached output is not fresh model evidence")
    return {
        "verdict": "BLOCKED" if issues else "VALID",
        "issues": issues,
        "run_id": run_id,
        "durable_invocations": len(calls),
        "wire_attempts": sum(len(v) for v in by_request.values()),
        "scope": "transport provenance; does not certify business utility or model robustness",
    }
