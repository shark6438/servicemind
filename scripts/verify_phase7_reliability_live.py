"""Repeat a fixed set of cases N times at concurrency 1 and record whether the outcome moves.

**The gap this fills.** The load batch found that the same question rests at ``succeeded``
on an idle machine and at ``failed`` under ten-way concurrency, and reported it as a defect
of the platform under load. That finding cannot, by itself, say whether the platform is
deterministic when *not* loaded -- a batch that only ever ran at concurrency 1 would have
missed it, and a batch that only ever ran at concurrency 10 cannot tell a race from a coin
flip. This batch holds the load fixed at one in flight and varies only the repeat index, so
what it measures is the platform's own run-to-run stability, with contention removed as far
as a shared machine allows.

**What is compared.** For each case, every repeat records the same fields the quality batch
records: terminal status, the reviewer's decision, and the citation set. Two repeats of one
case are the same outcome when all three agree. Terminal status alone is too coarse (a run
can succeed twice with different evidence, and an evidence change is what a reviewer would
have to act on); citations alone are too coarse the other way (a run can cite the same
documents and still fail to settle). The three together are the smallest set that matches
what a caller actually sees.

**What this is not.** It is not a mutation experiment and it does not judge. It records
outcomes and renders the variance it found; whether a given variance is acceptable is a
threshold question that belongs to whoever reads it. Repeats are recorded as separate
observation files rather than overwriting one, because the point of the batch is the set of
outcomes, and a set of one is not a measurement.

Usage:

    uv run python scripts/verify_phase7_reliability_live.py --tenant-id 2222... \
        [--repeats 5] [--only Q-001,Q-121]
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from servicemind.evaluation.deployment import refuse_stale_deployment

REPO_ROOT = Path(__file__).resolve().parents[1]
QUALITY_DRIVER = REPO_ROOT / "scripts" / "verify_phase7_quality_live.py"
REPLAYS = REPO_ROOT / "evaluation" / "reliability" / "replays"
BATCH = REPLAYS / "_batch.json"

DEFAULT_BASE_URL = "http://127.0.0.1:18080"
DEFAULT_CASE_BUDGET_SECONDS = 240.0
DEFAULT_REPEATS = 5

#: A stratified default, two per case kind, so the sample covers a run that must answer, a
#: run that must find its evidence thin, a run that must prefer a current version, and a
#: run that must refuse access. Chosen by position in the loaded case list rather than by an
#: id hard-coded here: ids are the case list's business, and a hard-coded id that stopped
#: existing would silently shrink the sample rather than fail.
PER_KIND = 2


def load_quality_driver() -> Any:
    """The quality driver as a module, for its ``Recorder``, ``Stack`` and loaders.

    Loaded by path because ``scripts/`` is not a package, and registered in ``sys.modules``
    before ``exec_module`` for the same dataclass-resolution reason the quality driver
    documents in its own loader: a module built from a spec and executed unregistered is
    invisible to ``sys.modules[cls.__module__]``, which ``@dataclass(slots=True)`` reads.
    """
    spec = importlib.util.spec_from_file_location("_quality_driver", QUALITY_DRIVER)
    if spec is None or spec.loader is None:  # pragma: no cover -- a broken checkout
        raise RuntimeError(f"cannot load {QUALITY_DRIVER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def stratified(cases: list[Any], only: str | None) -> list[Any]:
    """The sample to repeat: an explicit list if given, else the per-kind default."""
    if only:
        wanted = {item.strip() for item in only.split(",") if item.strip()}
        chosen = [case for case in cases if case.id in wanted]
        missing = wanted - {case.id for case in chosen}
        if missing:
            raise SystemExit(f"no such case id: {sorted(missing)}")
        return chosen

    seen: dict[str, int] = {}
    chosen = []
    for case in cases:
        kind = case.kind.value
        if seen.get(kind, 0) < PER_KIND:
            seen[kind] = seen.get(kind, 0) + 1
            chosen.append(case)
    return chosen


def outcome_of(record: dict[str, Any]) -> dict[str, Any]:
    """The three fields two repeats must agree on to count as the same outcome.

    Sorted citations, so the comparison is over the evidence set rather than the order the
    platform happened to render it in -- citation order is presentation, and a batch that
    flagged reordering as instability would drown the signal in noise it does not care about.
    ``errors`` is folded in because a repeat that raised is not the same outcome as one that
    did not, whatever the status field says.
    """
    return {
        "terminal_status": record.get("terminal_status"),
        "reviewer_decision": record.get("reviewer_decision"),
        "citations": sorted(record.get("citations") or []),
        "errors": record.get("errors") or [],
    }


def summarise(records: list[dict[str, Any]]) -> dict[str, Any]:
    """The variance the batch found, per case and overall."""
    by_case: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_case.setdefault(record["case_id"], []).append(record)

    cases: list[dict[str, Any]] = []
    for case_id, rows in sorted(by_case.items()):
        variants: dict[str, int] = {}
        for row in rows:
            key = json.dumps(outcome_of(row), ensure_ascii=False, sort_keys=True)
            variants[key] = variants.get(key, 0) + 1
        cases.append(
            {
                "case_id": case_id,
                "kind": rows[0].get("kind"),
                "repeats": len(rows),
                "stable": len(variants) == 1,
                "distinct_outcomes": len(variants),
                "variants": [
                    {"outcome": json.loads(key), "count": count}
                    for key, count in sorted(variants.items(), key=lambda item: -item[1])
                ],
            }
        )

    flaky = [case for case in cases if not case["stable"]]
    return {
        "cases": cases,
        "case_count": len(cases),
        "run_count": len(records),
        "stable_cases": len(cases) - len(flaky),
        "flaky_cases": len(flaky),
        "flake_rate": round(len(flaky) / len(cases), 4) if cases else None,
        "reading": (
            "Measured at concurrency 1, so any variance here is the platform's own and not "
            "contention. A flake_rate of 0 means every repeat of every sampled case agreed "
            "on status, decision and citations; it does not mean the platform is "
            "deterministic under load -- the load batch measures that separately."
        ),
    }


async def run(args: argparse.Namespace) -> int:
    # Refused before anything is observed, because the failure does not announce itself:
    # a process running code older than the tree answers every request competently, so the
    # batch completes and every record carries the revision of code the platform never ran.
    refusal = refuse_stale_deployment(REPO_ROOT, allow=args.allow_stale_deployment, argv=sys.argv)
    if refusal:
        return refusal

    driver = load_quality_driver()
    acceptance = driver.load_acceptance_driver()

    case_set = driver.load_quality_cases(driver.CASES)
    digest = driver.case_set_digest(case_set)
    tickets = driver.load_tickets()
    revision = acceptance.source_revision()

    selected = stratified(list(case_set.cases), args.only)
    if not selected:
        raise SystemExit("the filters selected no cases")

    REPLAYS.mkdir(parents=True, exist_ok=True)
    stack = acceptance.Stack(
        base_url=args.base_url, tenant_id=UUID(args.tenant_id), timeout_scale=1.0
    )
    recorder = driver.Recorder(
        stack=stack,
        tickets=tickets,
        restrictions=driver.load_restriction_map(),
        driver=acceptance,
    )

    print(
        f"repeating {len(selected)} cases x {args.repeats} at concurrency 1, "
        f"cases_digest {digest[:12]}, revision {revision}",
        flush=True,
    )

    records: list[dict[str, Any]] = []
    try:
        await stack.await_health(seconds=args.health_timeout)
        for index in range(args.repeats):
            for case in selected:
                record = await recorder.observe(
                    case, cases_digest=digest, revision=revision, budget=args.budget
                )
                record["repeat_index"] = index
                path = REPLAYS / f"{case.id}__rep{index}.json"
                path.write_text(
                    json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                records.append(record)
                marker = "ERR " if record.get("errors") else "    "
                print(
                    f"{marker}{case.id} rep{index} {record.get('kind', '')[:20]:20} "
                    f"{record.get('terminal_status') or '-':18} "
                    f"{record.get('reviewer_decision') or '-':16} "
                    f"{len(record.get('citations') or []):>2} cited  "
                    f"{record.get('elapsed_seconds', 0):>6.1f}s",
                    flush=True,
                )
    finally:
        await stack.aclose()

    summary = summarise(records)
    summary.update(
        {
            "mode": "live",
            "base_url": args.base_url,
            "tenant_id": args.tenant_id,
            "recorded_at": datetime.now(UTC).isoformat(),
            "deployed_revision": revision,
            "cases_digest": digest,
            "concurrency": 1,
            "repeats": args.repeats,
            "selected": [case.id for case in selected],
            "case_budget_seconds": args.budget,
        }
    )
    BATCH.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps({k: v for k, v in summary.items() if k != "cases"}, ensure_ascii=False, indent=1)
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True, help="the tenant the cases are in")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--only", default=None, help="a comma-separated list of case ids")
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument("--budget", type=float, default=DEFAULT_CASE_BUDGET_SECONDS)
    parser.add_argument("--health-timeout", type=float, default=120.0)
    parser.add_argument(
        "--allow-stale-deployment",
        action="store_true",
        help=(
            "observe even though the serving process predates the tree, recording the gap "
            "as a note instead of refusing; the evidence will describe the older code"
        ),
    )
    args = parser.parse_args()
    if args.repeats < 2:
        print("--repeats below 2 measures nothing: a set of one has no variance", file=sys.stderr)
        return 3
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
