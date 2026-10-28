"""No paid calls: exercise the experiment's measurement and spend boundaries."""

import argparse
import importlib.util
import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "compare_jev_routing", ROOT / "scripts/compare_jev_routing.py"
)
assert SPEC is not None and SPEC.loader is not None
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def case():
    return {
        "id": "test",
        "text": "Ticket 2 status?",
        "request_write": False,
        "expected_route": "simple_data_query",
        "rationale": "secret gold label",
        "dataset": "test",
        "expectation_strength": "contract",
        "rule": {"route": "simple_data_query", "latency_ms": 0.1},
        "jev": None,
    }


def response():
    return {
        "model": M.MODEL,
        "answers": {
            "route": {
                "type": "choice",
                "choice": "simple_data_query",
                "confidence": 1,
                "probabilities": {route: int(route == "simple_data_query") for route in M.ROUTES},
            }
        },
        "usage": {"input_tokens": 1000, "output_tokens": 40},
    }


def test_payload_has_no_labels_rule_predictions_or_rationales():
    payload = M.payload_for(case())
    assert payload["state"] == {"request": "Ticket 2 status?", "request_write": False}
    serialized = json.dumps(payload)
    assert "secret gold label" not in serialized
    assert "expected_route" not in serialized
    assert payload["questions"]["route"]["type"] == "choice"


def test_provider_switch_changes_only_model_in_payload():
    direct = M.payload_for(case())
    alternate = M.payload_for(case(), "openrouter")
    assert alternate.pop("model") == "typesafe/jev-1.14"
    direct.pop("model")
    assert alternate == direct


@pytest.mark.asyncio
async def test_openrouter_billing_and_dated_model_use_the_shared_budget(tmp_path):
    path = tmp_path / "spend.jsonl"
    # Reproduce the original pre-provider ledger, including its unresolved 451.
    with M.SpendLedger(path) as ledger:
        ledger.reserve("old direct request")
    document = response()
    document["model"] = "typesafe/jev-1.14-20261014"
    document["usage"]["cost"] = 0.00005  # Must use reported charge, not 0.000042.

    def handle(request):
        assert str(request.url) == M.PROVIDERS["openrouter"]["endpoint"]
        assert json.loads(request.content)["model"] == "typesafe/jev-1.14"
        return httpx.Response(200, json=document)

    with M.SpendLedger(path) as ledger:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await M.call_jev(client, case(), ledger, "openrouter", tmp_path / "receipts")
        assert result["served_model"] == document["model"]
        assert result["cost_source"] == "provider"
        assert Decimal(result["cost_usd"]) == Decimal("0.00005")
        assert Decimal(ledger.summary()["accounted_usd"]) == M.RESERVATION + Decimal("0.00005")
        assert ledger.summary()["http_attempts"] == 2
        with pytest.raises(M.ExperimentStop, match="unresolved_previous_attempt"):
            ledger.reserve("cannot retry typesafe")
    assert len(list((tmp_path / "receipts").glob("*.json"))) == 1


def test_switch_cannot_escape_old_spend_or_restart_provider(tmp_path):
    or_bound = Decimal(M.PROVIDERS["openrouter"]["context_ceiling"]) * M.PRICE / 1_000_000
    path = tmp_path / "spend.jsonl"
    with M.SpendLedger(path, or_bound + M.RESERVATION / 2) as ledger:
        ledger.reserve("direct")
        with pytest.raises(M.ExperimentStop, match="budget_exhausted"):
            ledger.reserve("alternate", "openrouter")
    path = tmp_path / "unresolved.jsonl"
    with M.SpendLedger(path) as ledger:
        ledger.reserve("alternate", "openrouter")
    with M.SpendLedger(path) as ledger:
        with pytest.raises(M.ExperimentStop, match="unresolved_previous_attempt"):
            ledger.reserve("alternate retry", "openrouter")


@pytest.mark.parametrize(
    "served", ["typesafe/jev-1.13", "typesafe/jev-latest", "typesafe/jev-1.140", "other/jev-1.14"]
)
def test_openrouter_cannot_silently_use_another_model(served):
    document = response()
    document["model"] = served
    with pytest.raises(ValueError):
        M.read_decision(document, "openrouter")


@pytest.mark.parametrize("budget", ["0", "-1", "0.71", "NaN", "Infinity"])
def test_budget_cannot_exceed_authorized_scope_or_be_nonfinite(tmp_path, budget):
    with pytest.raises(ValueError):
        M.SpendLedger(tmp_path / "spend.jsonl", Decimal(budget))


@pytest.mark.asyncio
async def test_success_reserves_durably_before_http_and_settles_exact_usage(tmp_path):
    path = tmp_path / "spend.jsonl"
    seen = []

    def handle(request):
        events = [json.loads(line) for line in path.read_text().splitlines()]
        assert events[-1]["event"] == "reserve"
        assert Decimal(events[-1]["usd"]) == M.RESERVATION
        assert str(request.url) == M.ENDPOINT
        assert "expected_route" not in request.content.decode()
        seen.append(request)
        return httpx.Response(200, json=response())

    with M.SpendLedger(path) as ledger:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await M.call_jev(client, case(), ledger)
        assert result["route"] == "simple_data_query"
        assert result["served_model"] == M.MODEL
        assert Decimal(result["cost_usd"]) == Decimal("0.000042")
        assert ledger.summary()["unresolved_reservations_usd"] == "0"
        assert Decimal(ledger.summary()["accounted_usd"]) == Decimal("0.000042")
    assert len(seen) == 1
    with M.SpendLedger(path) as ledger:
        assert ledger.summary()["http_attempts"] == 1
        assert Decimal(ledger.summary()["accounted_usd"]) == Decimal("0.000042")


@pytest.mark.asyncio
async def test_budget_denial_happens_before_network(tmp_path):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=response())

    with M.SpendLedger(tmp_path / "spend.jsonl", M.RESERVATION / 2) as ledger:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(M.ExperimentStop, match="budget_exhausted"):
                await M.call_jev(client, case(), ledger)
        assert ledger.summary()["http_attempts"] == 0
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 401, 402, 429, 500])
async def test_http_failures_are_not_retried_or_refunded(tmp_path, status):
    calls = []
    path = tmp_path / "spend.jsonl"

    def handle(request):
        calls.append(request)
        return httpx.Response(status, text="sensitive provider diagnostic must not be persisted")

    with M.SpendLedger(path) as ledger:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(M.ExperimentStop, match=f"^http_{status}$"):
                await M.call_jev(client, case(), ledger)
        assert Decimal(ledger.summary()["unresolved_reservations_usd"]) == M.RESERVATION
    with M.SpendLedger(path) as ledger:
        with pytest.raises(M.ExperimentStop, match="unresolved_previous_attempt"):
            ledger.reserve("another request")
    assert len(calls) == 1
    assert "sensitive" not in path.read_text()


@pytest.mark.asyncio
async def test_timeout_keeps_reservation(tmp_path):
    def handle(request):
        raise httpx.ReadTimeout("private details", request=request)

    with M.SpendLedger(tmp_path / "spend.jsonl") as ledger:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(M.ExperimentStop, match="^transport_error_unknown_usage$"):
                await M.call_jev(client, case(), ledger)
        assert Decimal(ledger.summary()["accounted_usd"]) == M.RESERVATION


@pytest.mark.asyncio
async def test_invalid_decision_still_accounts_provider_usage(tmp_path):
    document = response()
    document["answers"]["route"]["choice"] = "execute_shell"
    with M.SpendLedger(tmp_path / "spend.jsonl") as ledger:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=document))
        ) as client:
            with pytest.raises(M.ExperimentStop, match="invalid_provider_response"):
                await M.call_jev(client, case(), ledger)
        assert Decimal(ledger.summary()["accounted_usd"]) == Decimal("0.000042")
        assert ledger.summary()["http_attempts"] == 1


@pytest.mark.asyncio
async def test_missing_usage_cannot_be_treated_as_free(tmp_path):
    document = response()
    del document["usage"]
    with M.SpendLedger(tmp_path / "spend.jsonl") as ledger:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=document))
        ) as client:
            with pytest.raises(M.ExperimentStop, match="invalid_provider_response"):
                await M.call_jev(client, case(), ledger)
        assert Decimal(ledger.summary()["unresolved_reservations_usd"]) == M.RESERVATION


def test_second_writer_is_rejected_and_crash_reservation_survives(tmp_path):
    path = tmp_path / "spend.jsonl"
    with M.SpendLedger(path) as ledger:
        ledger.reserve("payload")
        with pytest.raises(BlockingIOError), M.SpendLedger(path):
            pytest.fail("A second writer acquired the ledger")
    with M.SpendLedger(path) as ledger:
        with pytest.raises(M.ExperimentStop, match="unresolved_previous_attempt"):
            ledger.reserve("payload")


def test_reopening_does_not_raise_budget_and_partial_journal_fails_closed(tmp_path):
    path = tmp_path / "spend.jsonl"
    with M.SpendLedger(path, Decimal("0.01")):
        pass
    with M.SpendLedger(path, M.SPEND_CAP) as ledger:
        assert ledger.budget == Decimal("0.01")
    with path.open("a") as handle:
        handle.write('{"event":"reserve"')
    with pytest.raises(json.JSONDecodeError), M.SpendLedger(path):
        pytest.fail("Corrupt ledger accepted")


def test_request_limit_survives_process_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "REQUEST_CAP", 1)
    path = tmp_path / "spend.jsonl"
    with M.SpendLedger(path) as ledger:
        identifier = ledger.reserve("first")
        ledger.settle(identifier, 100)
    with M.SpendLedger(path) as ledger:
        with pytest.raises(M.ExperimentStop, match="request_cap_reached"):
            ledger.reserve("second")


@pytest.mark.parametrize("tokens", [-1, 0, True, 1.5])
def test_invalid_usage_cannot_release_a_reservation(tmp_path, tokens):
    with M.SpendLedger(tmp_path / "spend.jsonl") as ledger:
        identifier = ledger.reserve("request")
        with pytest.raises(ValueError):
            ledger.settle(identifier, tokens)
        assert Decimal(ledger.summary()["unresolved_reservations_usd"]) == M.RESERVATION


@pytest.mark.parametrize("mutation", ["model", "missing_option", "nan", "sum", "choice", "mode"])
def test_response_contract_rejects_drift(mutation):
    document = response()
    answer = document["answers"]["route"]
    if mutation == "model":
        document["model"] = "jev-latest"
    elif mutation == "missing_option":
        del answer["probabilities"]["unsupported"]
    elif mutation == "nan":
        answer["confidence"] = float("nan")
    elif mutation == "sum":
        answer["probabilities"]["unsupported"] = 0.5
    elif mutation == "choice":
        answer["choice"] = "invented"
    else:
        answer["choice"] = "unsupported"
    with pytest.raises(ValueError):
        M.read_decision(document)


def test_metrics_keep_missing_calls_in_denominator_and_record_regressions():
    first, second, third = case(), case(), case()
    first["jev"] = {"route": "simple_data_query", "latency_ms": 200}
    second["jev"] = {"route": "complex_workflow", "latency_ms": 400}
    result = M.summarize([first, second, third])
    assert result["jev"]["accuracy_all_planned"] == 1 / 3
    assert result["jev"]["accuracy_observed"] == 0.5
    assert result["paired"] == {"observed": 2, "improved": 0, "regressed": 1}
    assert result["jev"]["confusion"]["simple_data_query"]["not_observed"] == 1


def test_baseline_never_invents_a_jev_score():
    report = M.build_report([case()], "BASELINE_ONLY", None)
    assert report["by_dataset"]["test"]["jev"]["accuracy_all_planned"] is None
    assert report["by_dataset"]["test"]["jev"]["latency"] is None
    assert report["spend"] is None


def test_datasets_keep_provenance_and_distinct_expectation_strengths():
    cases = M.load_cases()
    assert len(cases) == 212
    probes = [row for row in cases if row["dataset"] == "routing_semantic_probe.v1.jsonl"]
    assert len(probes) == 64
    assert all(row["label_source"] == "assistant_authored_unreviewed" for row in probes)
    assert all(row["expectation_strength"] == "proposed_semantics" for row in probes)
    assert len([row for row in cases if row["expectation_strength"] == "boundary"]) == 9


@pytest.mark.asyncio
async def test_offline_mode_never_constructs_http_client(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline evaluation attempted to construct an HTTP client")

    monkeypatch.setattr(M.httpx, "AsyncClient", forbidden)
    directory = tmp_path / "offline"
    args = argparse.Namespace(
        output=directory, live=False, budget_usd=M.SPEND_CAP, env_file=tmp_path / "missing"
    )
    assert await M.run(args) == 0
    report = json.loads((directory / "report.json").read_text())
    assert report["status"] == "BASELINE_ONLY"
    assert report["by_dataset"]["routing.jsonl"]["rule"]["correct"] == 107
    assert report["by_dataset"]["routing_hard.v1.jsonl"]["rule"]["correct"] == 36
    with pytest.raises(FileExistsError):
        await M.run(args)


@pytest.mark.asyncio
async def test_missing_key_writes_blocked_report_without_http(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(
        M.httpx, "AsyncClient", lambda **kwargs: pytest.fail("Missing-key network call")
    )
    directory = tmp_path / "missing-key"
    args = argparse.Namespace(
        output=directory, live=True, budget_usd=M.SPEND_CAP, env_file=tmp_path / "missing"
    )
    assert await M.run(args) == 2
    report = json.loads((directory / "report.json").read_text())
    assert report["status"] == "BLOCKED_MISSING_TYPESAFE_API_KEY"
    assert report["spend"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_second", [False, True])
async def test_live_harness_with_fake_http_keeps_complete_denominator_and_spend(
    tmp_path, monkeypatch, fail_second
):
    calls = []
    real_client = httpx.AsyncClient

    def handle(request):
        calls.append(request)
        if fail_second and len(calls) == 2:
            return httpx.Response(429, text="sensitive diagnostic")
        return httpx.Response(200, json=response())

    monkeypatch.setattr(M, "EXPERIMENT", tmp_path / "experiment")
    monkeypatch.setenv("TYPESAFE_API_KEY", "unit-test-secret")
    monkeypatch.setattr(
        M.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    args = argparse.Namespace(
        output=tmp_path / "report",
        live=True,
        budget_usd=M.SPEND_CAP,
        env_file=tmp_path / "absent",
    )
    exit_code = await M.run(args)
    report_text = (args.output / "report.json").read_text()
    report = json.loads(report_text)
    assert len(report["rows"]) == 212
    assert "unit-test-secret" not in report_text
    assert "sensitive diagnostic" not in report_text
    if fail_second:
        assert exit_code == 2
        assert report["status"] == "STOPPED_http_429"
        assert len(calls) == 2
        assert sum(row["jev"] is not None for row in report["rows"]) == 1
        assert Decimal(report["spend"]["priced_usage_usd"]) == Decimal("0.000042")
        assert Decimal(report["spend"]["unresolved_reservations_usd"]) == M.RESERVATION
    else:
        assert exit_code == 0
        assert report["status"] == "COMPLETED"
        assert len(calls) == 212
        assert all(row["jev"] is not None for row in report["rows"])
        assert Decimal(report["spend"]["priced_usage_usd"]) == 212 * Decimal("0.000042")
        assert Decimal(report["spend"]["unresolved_reservations_usd"]) == 0


@pytest.mark.parametrize("bad", ["false", 2, None, 0.0])
def test_loader_rejects_ambiguous_write_flags(tmp_path, bad):
    row = deepcopy(case())
    row["request_write"] = bad
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(row) + "\n")
    with pytest.raises(ValueError):
        M.load_cases((path,))
