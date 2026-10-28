"""Frozen, topic-grouped synthetic benchmark; offline unless --live is supplied.

Reuses the original Jev wire protocol and prompt. The historical ledger is locked
and carried into a stricter USD 0.05 total cap; this stage has its own 500-call cap.
No production routing or downstream workflow is run.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import itertools
import json
import math
import os
import random
import time
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import compare_jev_routing as wire
import httpx
from dotenv import dotenv_values

from servicemind.orchestration.router import FastPathRouter

ROOT = wire.ROOT
SPEC = ROOT / "evaluation/routing/preregistration.machine.v3.json"
STAGE = ROOT / "evaluation/experiments/jev_robustness_v3"
LIMIT = Decimal("0.05")
MAX_CALLS = 500
INPUT_FIELDS = (
    "id",
    "text",
    "request_write",
    "expected_route",
    "dataset",
    "group_id",
    "scenario_id",
    "family",
    "form",
    "language",
    "split",
    "label_source",
    "expectation_strength",
    "human_reviewed",
    "source_kind",
    "rationale",
)
FAST = {"simple_data_query", "simple_knowledge_query"}
SLOW = {"complex_workflow", "unsupported"}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def verify_spec(path: Path = SPEC) -> tuple[dict, list[dict], list[dict]]:
    spec = json.loads(path.read_text())
    if spec["budget"]["total_usd_including_history"] != str(LIMIT):
        raise ValueError("Unexpected cumulative cap")
    if spec["budget"]["new_attempt_cap"] != MAX_CALLS:
        raise ValueError("Unexpected attempt cap")
    if spec["prompt_hash"] != wire.digest(wire.encoded(wire.QUESTION)):
        raise ValueError("Prompt changed")
    for relative, expected in spec["frozen_files"].items():
        if sha(ROOT / relative) != expected:
            raise ValueError(f"Frozen file changed: {relative}")
    cases = []
    for entry in spec["datasets"]:
        dataset = ROOT / entry["path"]
        if sha(dataset) != entry["sha256"]:
            raise ValueError("Dataset digest changed")
        part = wire.load_cases((dataset,))
        if len(part) != entry["count"]:
            raise ValueError("Dataset coverage mismatch")
        cases.extend(part)
    if len(cases) != MAX_CALLS or len({case["id"] for case in cases}) != MAX_CALLS:
        raise ValueError("Expected 500 unique cases")
    if len({case["text"] for case in cases}) != MAX_CALLS:
        raise ValueError("Duplicate request")
    groups = defaultdict(set)
    for case in cases:
        if case["human_reviewed"] or case["source_kind"] != "synthetic_challenge":
            raise ValueError("Incorrect annotation provenance")
        if case["split"] not in {"dev", "test"}:
            raise ValueError("Invalid split")
        groups[case["group_id"]].add(case["split"])
    if any(len(splits) != 1 for splits in groups.values()):
        raise ValueError("Topic leakage between dev and test")
    guard = json.loads((ROOT / spec["historical_report"]).read_text())["rows"]
    guard = [row for row in guard if row["dataset"] != "routing_semantic_probe.v1.jsonl"]
    return spec, cases, guard


def validate_report(rows: list[dict], cases: list[dict]) -> None:
    expected = {case["id"]: case for case in cases}
    if len(rows) != len(expected) or {row["id"] for row in rows} != expected.keys():
        raise ValueError("Missing, duplicated or extra report rows")
    for row in rows:
        if any(row[field] != expected[row["id"]][field] for field in INPUT_FIELDS):
            raise ValueError("Report inputs/labels do not match frozen data")
        if row.get("jev") is None or row.get("hybrid") is None:
            raise ValueError("Incomplete paired observations")


class CumulativeLedger(wire.SpendLedger):
    """New stage journal, retaining historical known and uncertain spending."""

    def __init__(self, path: Path, carry: dict) -> None:
        super().__init__(path, LIMIT)
        self.carry = carry

    def summary(self) -> dict:
        current = super().summary()
        known = Decimal(current["priced_usage_usd"]) + Decimal(self.carry["priced_usage_usd"])
        unknown = Decimal(current["unresolved_reservations_usd"]) + Decimal(
            self.carry["unresolved_reservations_usd"]
        )
        return {
            **current,
            "priced_usage_usd": str(known),
            "unresolved_reservations_usd": str(unknown),
            "accounted_usd": str(known + unknown),
            "stage_priced_usage_usd": current["priced_usage_usd"],
            "stage_unresolved_usd": current["unresolved_reservations_usd"],
            "historical_attempts": self.carry["http_attempts"],
            "request_cap": MAX_CALLS,
        }

    def reserve(self, payload_hash: str, provider: str = "typesafe") -> str:
        if provider != "openrouter":
            raise wire.ExperimentStop("unexpected_provider")
        entries = self._reservations().values()
        if any(entry["event"] == "reserve" for entry in entries):
            raise wire.ExperimentStop("unresolved_previous_attempt")
        summary = self.summary()
        if summary["http_attempts"] >= MAX_CALLS:
            raise wire.ExperimentStop("request_cap_reached")
        amount = Decimal(wire.PROVIDERS[provider]["context_ceiling"]) * wire.PRICE / 1_000_000
        if Decimal(summary["accounted_usd"]) + amount > self.budget:
            raise wire.ExperimentStop("budget_exhausted")
        identifier = str(uuid4())
        self.append(
            {
                "event": "reserve",
                "id": identifier,
                "usd": str(amount),
                "provider": provider,
                "payload_hash": payload_hash,
            }
        )
        return identifier


def hybrid(row: dict, threshold: float) -> dict:
    decision = row.get("jev")
    source = "jev" if decision and decision["confidence"] >= threshold else "rule"
    return {
        "route": row[source]["route"],
        "source": source,
        "degraded": decision is None,
    }


def metrics(rows: list[dict], arm: str) -> dict:
    confusion = {route: {actual: 0 for actual in wire.ROUTES} for route in wire.ROUTES}
    dangerous = [row for row in rows if row["expected_route"] in SLOW]
    for row in rows:
        confusion[row["expected_route"]][row[arm]["route"]] += 1
    times = [row[arm]["latency_ms"] for row in rows if "latency_ms" in row[arm]]
    return {
        "n": len(rows),
        "correct": sum(row[arm]["route"] == row["expected_route"] for row in rows),
        "accuracy": (
            sum(row[arm]["route"] == row["expected_route"] for row in rows) / len(rows)
            if rows
            else None
        ),
        "dangerous": sum(row[arm]["route"] in FAST for row in dangerous),
        "dangerous_denominator": len(dangerous),
        "unsupported_not_rejected": sum(
            row["expected_route"] == "unsupported" and row[arm]["route"] != "unsupported"
            for row in rows
        ),
        "unsupported_denominator": sum(row["expected_route"] == "unsupported" for row in rows),
        "confusion": confusion,
        "latency": wire.latency_summary(times),
    }


def select_threshold(dev: list[dict], guard: list[dict], grid: list[float]) -> dict:
    if not dev or any(row["split"] != "dev" for row in dev):
        raise ValueError("Threshold selection accepts dev rows only")
    candidates = []
    original_danger = metrics(dev, "rule")["dangerous"]
    for threshold in grid:
        predictions = [{**row, "hybrid": hybrid(row, threshold)} for row in dev]
        result = metrics(predictions, "hybrid")
        guard_regressions = sum(
            row["rule"]["route"] == row["expected_route"]
            and hybrid(row, threshold)["route"] != row["expected_route"]
            for row in guard
        )
        candidates.append(
            {
                "threshold": threshold,
                **result,
                "jev_coverage": sum(row["hybrid"]["source"] == "jev" for row in predictions)
                / len(dev),
                "historical_guard_regressions": guard_regressions,
                "eligible": guard_regressions == 0 and result["dangerous"] <= original_danger,
            }
        )
    eligible = [point for point in candidates if point["eligible"]]
    if not eligible:
        raise ValueError("No eligible threshold, including rule-only fallback")
    chosen = max(
        eligible,
        key=lambda point: (point["correct"], -point["dangerous"], point["threshold"]),
    )
    return {
        "threshold": chosen["threshold"],
        "selection_rule": "max dev correct, then min danger, then larger threshold; "
        "no individual historical guard regression and dev danger <= rule required",
        "dev_count": len(dev),
        "test_observed_at_selection": 0,
        "candidates": candidates,
    }


def exact_p(improved: int, regressed: int) -> float:
    count = improved + regressed
    if not count:
        return 1.0
    return min(
        1.0,
        2 * sum(math.comb(count, k) for k in range(min(improved, regressed) + 1)) / 2**count,
    )


def comparison(rows: list[dict], arm: str, iterations: int = 10_000) -> dict:
    improved = sum(
        row[arm]["route"] == row["expected_route"] != row["rule"]["route"] for row in rows
    )
    regressed = sum(
        row["rule"]["route"] == row["expected_route"] != row[arm]["route"] for row in rows
    )
    groups = defaultdict(list)
    for row in rows:
        groups[(row["dataset"], row["group_id"])].append(row)
    by_stratum = defaultdict(list)
    for (dataset, _), block in groups.items():
        difference = sum(
            int(row[arm]["route"] == row["expected_route"])
            - int(row["rule"]["route"] == row["expected_route"])
            for row in block
        )
        by_stratum[dataset].append((difference, len(block)))
    generator = random.Random(20261028)
    bootstrap = []
    for _ in range(iterations):
        sample = [
            generator.choice(blocks) for blocks in by_stratum.values() for _ in range(len(blocks))
        ]
        bootstrap.append(sum(d for d, _ in sample) / sum(n for _, n in sample))
    bootstrap.sort()
    differences = [difference for blocks in by_stratum.values() for difference, _ in blocks]
    observed = abs(sum(differences))
    extreme = sum(
        abs(sum(sign * d for sign, d in zip(signs, differences, strict=True))) >= observed
        for signs in itertools.product((-1, 1), repeat=len(differences))
    )
    dangerous = [row for row in rows if row["expected_route"] in SLOW]
    fixed = sum(row["rule"]["route"] in FAST and row[arm]["route"] not in FAST for row in dangerous)
    introduced = sum(
        row["rule"]["route"] not in FAST and row[arm]["route"] in FAST for row in dangerous
    )
    return {
        "improved": improved,
        "regressed": regressed,
        "delta": (improved - regressed) / len(rows),
        "cluster_count": len(groups),
        "cluster_bootstrap_ci95": [
            bootstrap[max(0, math.ceil(iterations * 0.025) - 1)],
            bootstrap[math.ceil(iterations * 0.975) - 1],
        ],
        "cluster_permutation_p": extreme / 2 ** len(differences),
        "row_mcnemar_p_descriptive_only": exact_p(improved, regressed),
        "danger_fixed": fixed,
        "danger_introduced": introduced,
        "danger_mcnemar_p_descriptive_only": exact_p(fixed, introduced),
    }


def holm(pvalues: dict[str, float]) -> dict[str, float]:
    result, previous = {}, 0.0
    ordered = sorted(pvalues, key=pvalues.get)
    for index, name in enumerate(ordered):
        previous = max(previous, min(1.0, (len(ordered) - index) * pvalues[name]))
        result[name] = previous
    return result


def analyze(rows: list[dict], guard: list[dict], selection: dict, spend: dict) -> dict:
    test = [row for row in rows if row["split"] == "test"]
    threshold = selection["threshold"]
    old = [{**row, "hybrid": hybrid(row, threshold)} for row in guard]
    summaries = {
        arm: {"metrics": metrics(test, arm), "versus_rule": comparison(test, arm)}
        for arm in ("jev", "hybrid")
    }
    adjusted = holm(
        {arm: value["versus_rule"]["cluster_permutation_p"] for arm, value in summaries.items()}
    )
    for arm in summaries:
        summaries[arm]["versus_rule"]["holm_p"] = adjusted[arm]
    groups = {
        dimension: {
            name: {
                arm: metrics([row for row in test if row[dimension] == name], arm)
                for arm in ("rule", "jev", "hybrid")
            }
            for name in sorted({row[dimension] for row in test})
        }
        for dimension in ("dataset", "family", "form", "language")
    }
    jev = summaries["jev"]["versus_rule"]
    gates = {
        "J_accuracy_gain_at_least_10pp": jev["delta"] >= 0.10,
        "J_cluster_ci_excludes_zero": jev["cluster_bootstrap_ci95"][0] > 0,
        "J_cluster_holm_p_at_most_005": jev["holm_p"] <= 0.05,
        "J_danger_not_above_rule": metrics(test, "jev")["dangerous"]
        <= metrics(test, "rule")["dangerous"],
        "J_prohibited_acceptance_not_above_rule": metrics(test, "jev")["unsupported_not_rejected"]
        <= metrics(test, "rule")["unsupported_not_rejected"],
        "H_no_individual_historical_regression": all(
            row["rule"]["route"] != row["expected_route"]
            or row["hybrid"]["route"] == row["expected_route"]
            for row in old
        ),
        "latency_p95_at_most_2s": metrics(test, "jev")["latency"]["p95_ms"] <= 2000,
        "cumulative_budget_ok": Decimal(spend["accounted_usd"]) <= LIMIT,
    }
    return {
        "status": "COMPLETED",
        "test_count": len(test),
        "test_topics": len({r["group_id"] for r in test}),
        "selected_threshold": threshold,
        "rule": metrics(test, "rule"),
        **summaries,
        "by": groups,
        "historical_exposed_reference": {
            dataset: {
                arm: metrics([row for row in old if row["dataset"] == dataset], arm)
                for arm in ("rule", "jev", "hybrid")
            }
            for dataset in sorted({row["dataset"] for row in old})
        },
        "gates": gates,
        "all_gates_pass": all(gates.values()),
        "spend": spend,
        "limitations": [
            "Single assistant authored and labelled; no independent human annotation or kappa.",
            "Synthetic, template-based challenge; no real traffic or production accuracy claim.",
            "Topic split prevents sibling leakage; wording templates are shared across topics.",
            "Cluster inference assumes independent topics; shared templates limit that assumption.",
            "Historical cases were exposed and constrain selection; they are not a held-out test.",
            "All arms require a Jev call to know its confidence; coverage does not save API calls.",
            "No downstream execution, end-to-end cost saving or production deployment measured.",
        ],
    }


async def account(client: httpx.AsyncClient) -> dict:
    response = await client.get("https://openrouter.ai/api/v1/key")
    if response.status_code != 200:
        raise wire.ExperimentStop("account_check_failed")
    data = response.json()["data"]
    remaining = data.get("limit_remaining")
    if remaining is None or Decimal(str(remaining)) < LIMIT:
        raise wire.ExperimentStop("insufficient_key_allowance")
    return {
        "at": wire.timestamp(),
        **{key: data.get(key) for key in ("limit", "limit_remaining", "usage", "is_free_tier")},
    }


async def run(live: bool) -> int:
    spec, cases, guard = verify_spec()
    if not live:
        print(f"VALIDATED_OFFLINE: {len(cases)} machine-labelled cases; no calls")
        return 0
    key = os.environ.get("OPENROUTER_API_KEY") or dotenv_values(ROOT / ".env").get(
        "OPENROUTER_API_KEY"
    )
    if not key:
        raise wire.ExperimentStop("missing_openrouter_key")
    STAGE.mkdir(parents=True, exist_ok=False)
    save(
        STAGE / "freeze.json",
        {
            "at": wire.timestamp(),
            "spec": spec,
            "spec_sha256": sha(SPEC),
            "note": "Written before new predictions and paid calls; no human review is claimed.",
        },
    )
    rows, router = [], FastPathRouter()
    for case in cases:
        started = time.perf_counter()
        decision = router.route(case["text"], request_write=case["request_write"])
        rows.append(
            {
                **case,
                "rule": {
                    **decision.model_dump(mode="json"),
                    "latency_ms": (time.perf_counter() - started) * 1000,
                },
                "jev": None,
                "hybrid": None,
                "jev_status": "not_attempted",
            }
        )
    # Hold the old journal's writer lock for the entire stage. Its contents never change.
    with (
        wire.SpendLedger(wire.EXPERIMENT / "spend.jsonl") as historical,
        CumulativeLedger(STAGE / "spend.jsonl", historical.summary()) as ledger,
    ):
        save(STAGE / "historical-spend.json", historical.summary())
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {key}"},
            timeout=20,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            save(STAGE / "account-before.json", await account(client))
            selection, served_model = None, None
            for split in ("dev", "test"):
                if split == "test":
                    dev = [row for row in rows if row["split"] == "dev"]
                    selection = select_threshold(dev, guard, spec["threshold_grid"])
                    selection.update(
                        {
                            "selected_at": wire.timestamp(),
                            "spec_sha256": sha(SPEC),
                            "dev_predictions_sha256": wire.digest(wire.encoded(dev)),
                        }
                    )
                    with (STAGE / "selection.json").open("x") as handle:
                        json.dump(selection, handle, ensure_ascii=False, indent=2)
                        handle.write("\n")
                    print(
                        f"THRESHOLD_FROZEN: {selection['threshold']}; no test predictions observed",
                        flush=True,
                    )
                subset = [row for row in rows if row["split"] == split]
                for index, row in enumerate(subset):
                    try:
                        row["jev"] = await wire.call_jev(
                            client, row, ledger, "openrouter", STAGE / "receipts"
                        )
                        if served_model is not None and served_model != row["jev"]["served_model"]:
                            raise wire.ExperimentStop("served_model_changed_within_batch")
                        served_model = row["jev"]["served_model"]
                        row["jev_status"] = "observed"
                    except wire.ExperimentStop as exc:
                        row["jev_status"] = str(exc)
                        save(
                            STAGE / "report.json",
                            {"status": f"STOPPED_{exc}", "rows": rows, "spend": ledger.summary()},
                        )
                        print(f"STOPPED_{exc}", flush=True)
                        return 2
                    if index == 0 or (index + 1) % 10 == 0 or index + 1 == len(subset):
                        save(
                            STAGE / "report.json",
                            {
                                "status": "RUNNING",
                                "split": split,
                                "rows": rows,
                                "spend": ledger.summary(),
                            },
                        )
                        print(
                            f"{split}: {index + 1}/{len(subset)}; cumulative USD {ledger.summary()['accounted_usd']}",
                            flush=True,
                        )
            for row in rows:
                row["hybrid"] = hybrid(row, selection["threshold"])
            validate_report(rows, cases)
            verify_spec()  # Reject edits to frozen sources/data while the run was in flight.
            save(
                STAGE / "report.json",
                {
                    "status": "COMPLETED",
                    "updated_at": wire.timestamp(),
                    "rows": rows,
                    "spend": ledger.summary(),
                    "served_model": served_model,
                    "selection_sha256": sha(STAGE / "selection.json"),
                },
            )
            save(STAGE / "analysis.json", analyze(rows, guard, selection, ledger.summary()))
            save(STAGE / "account-after.json", await account(client))
    print(f"COMPLETED: {STAGE}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    arguments = parser.parse_args()
    try:
        return asyncio.run(run(arguments.live))
    except wire.ExperimentStop as exc:
        print(f"STOPPED_{exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
