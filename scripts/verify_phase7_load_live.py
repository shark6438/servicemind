"""Run the declared load tiers against the live platform and record what happened.

This script **records**; it does not judge. Every verdict lives in
``servicemind.evaluation.load_grader``, which is a pure function over these observation
files. The split is the same one the acceptance, security and quality batches use, and it is
what makes the gate's replay mode meaningful: the judge can be re-run over the same
observations without a stack, and an observation cannot be quietly re-graded by re-running
the platform until it agrees.

**Tiers run one after another, never at the same time.** This is the one structural rule the
driver enforces, and it is easy to get wrong by making the whole batch one queue. A tier is
only interpretable if the machine was doing that tier and nothing else: ten-way concurrency
measured while a five-way tier is also in flight is a measurement of fifteen, and the number
would be reported under the wrong heading.

**Concurrency is per tier, not per batch.** Within a tier, at most ``concurrency`` runs are in
flight, drawn from one queue holding every (question, pass) the tier declares. Runs are taken
in declared order and finished in whatever order they finish, which is why the observation
files are named by slot rather than numbered: case 7 finishing before case 3 is the normal
case under concurrency, not an anomaly to be recorded.

**What is recorded per run.** The question, the tier, the pass number, the wall time from
submit to rest, the terminal status, the reviewer's decision, and the citations the run
published. Nothing here knows what the answer should be -- that is in the quality case list,
and applying it is the grader's job.

**Why the deadline is a settle budget rather than a sleep.** ``settle_budget_seconds`` comes
from the plan and bounds the poll, not the run. A run that has not rested inside it is
recorded with no terminal status, which the grader reads as a wedged run rather than as a
slow one; the report then says so in those words instead of leaving a latency percentile to
stand in for an answer that never came.

Usage:

    uv run python scripts/verify_phase7_load_live.py --tenant-id 2222...
    uv run python scripts/verify_phase7_load_live.py --only-tier tier-5
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import selectors
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from servicemind.evaluation.load import (
    LoadPlan,
    LoadTier,
    Workload,
    WorkloadCase,
    load_plan,
    plan_digest,
    resolve_workload,
    workload_digest,
)
from servicemind.evaluation.quality import read_citations, reviewer_decision

REPO_ROOT = Path(__file__).resolve().parents[1]
LOAD = REPO_ROOT / "evaluation" / "load"
PLAN = LOAD / "plan.v1.json"
REPLAYS = LOAD / "replays"
BATCH = REPLAYS / "_batch.json"
QUALITY = REPO_ROOT / "evaluation" / "quality"
CASES = QUALITY / "cases.v1.json"
TICKETS = QUALITY / "tickets.json"

QUALITY_DRIVER = REPO_ROOT / "scripts" / "verify_phase7_quality_live.py"

#: How often the poll re-reads a run. The ceiling on a single read is the plan's settle
#: budget, not a constant here: the budget is a property of the measurement, and a driver
#: that carried its own would let the two disagree about when a run counts as wedged.
POLL_SECONDS = 1.5

DEFAULT_BASE_URL = "http://127.0.0.1:18080"


@dataclass(frozen=True, slots=True)
class Slot:
    """One declared run: a question, on a pass, at a tier."""

    tier: LoadTier
    repeat_index: int
    case: WorkloadCase

    def name(self) -> str:
        return f"{self.tier.name}__r{self.repeat_index}__{self.case.id}"

    def label(self) -> str:
        return f"{self.case.id}#{self.repeat_index}"


def _load_quality_driver() -> Any:
    """The quality driver as a module, for its loader of the acceptance driver.

    The load batch needs the same live client the other batches use, and the quality driver
    already contains the loader that produces it -- including the ``sys.modules``
    registration that ``@dataclass(slots=True)`` requires, whose absence cost a smoke run an
    ``AttributeError`` from inside a decorator. Re-deriving that here would be re-deriving the
    bug, which is the same argument the quality driver makes for reusing the acceptance one.

    Loaded by path because ``scripts/`` is not a package. Both modules are guarded by
    ``if __name__ == "__main__"``, so this defines and runs nothing -- asserted below rather
    than assumed, since a driver that started a sweep at import time would be a spectacular
    way to discover the problem.
    """
    spec = importlib.util.spec_from_file_location("_quality_driver", QUALITY_DRIVER)
    if spec is None or spec.loader is None:  # pragma: no cover -- a broken checkout
        raise RuntimeError(f"cannot load {QUALITY_DRIVER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[spec.name]
        raise
    if not hasattr(module, "load_acceptance_driver"):
        raise RuntimeError(
            "the quality driver no longer exposes ``load_acceptance_driver``; the load driver "
            "reuses it rather than duplicating the live client, so this is a real break"
        )
    return module


def load_tickets(workload: Workload) -> dict[str, int]:
    """The GLPI ticket ids the workload's cases refer to.

    Read from the quality fixtures rather than resolved here: the ticket a question is asked
    about is part of the fixture, and a load batch that resolved its own tickets would be
    running the same questions against different context.
    """
    if not TICKETS.exists():
        raise SystemExit(
            f"{TICKETS.relative_to(REPO_ROOT)} does not exist; run "
            "scripts/seed_phase7_quality_fixtures.py first"
        )
    recorded = json.loads(TICKETS.read_text(encoding="utf-8"))
    resolved = {str(name): int(value) for name, value in recorded["tickets"].items()}
    wanted = {case.ticket_ref for case in workload.cases}
    absent = sorted(wanted - set(resolved))
    if absent:
        raise SystemExit(
            f"the workload names ticket(s) {absent} which {TICKETS.name} does not hold; the "
            "load plan and the quality fixtures have drifted apart"
        )
    return resolved


class Recorder:
    """One run in flight: submit, settle, read, hand the observation back."""

    def __init__(self, *, stack: Any, tickets: dict[str, int]) -> None:
        self.stack = stack
        self.tickets = tickets

    async def observe(
        self,
        slot: Slot,
        *,
        plan_digest_value: str,
        workload_digest_value: str,
        revision: str | None,
        budget: float,
    ) -> dict[str, Any]:
        started = time.monotonic()
        errors: list[str] = []
        run_id: UUID | None = None
        terminal_status: str | None = None
        decision: str | None = None
        reading = read_citations(None)
        case = slot.case

        try:
            response = await self.stack.create_run(
                case.subject,
                ticket_id=self.tickets[case.ticket_ref],
                goal=case.question,
                request_write=False,
            )
            if response.status_code not in (200, 201, 202):
                errors.append(f"POST /runs returned {response.status_code}: {response.text[:400]}")
            else:
                # ``id``, which is ``RunView``'s field, not ``run_id``, which belongs to the
                # webhook response. Named here because the quality driver paid for the mistake
                # once already: a recorder reading a key the endpoint never sends records a
                # KeyError against a platform that answered 202.
                body = response.json()
                if "id" not in body:
                    errors.append(
                        "POST /runs returned no ``id``; the body has "
                        f"{sorted(body)}. The run was created and its id is lost, so this "
                        "run cannot be observed."
                    )
                else:
                    run_id = UUID(str(body["id"]))
        except Exception as exc:  # noqa: BLE001 -- recorded, not swallowed
            errors.append(f"POST /runs raised {type(exc).__name__}: {exc}")

        if run_id is not None and not errors:
            try:
                response = await self.stack.settle(case.subject, run_id, time.monotonic() + budget)
                if response.status_code != 200:
                    errors.append(
                        f"GET /runs returned {response.status_code}: {response.text[:400]}"
                    )
                else:
                    payload = response.json()
                    terminal_status = payload.get("status")
                    result = payload.get("result")
                    decision = reviewer_decision(result)
                    reading = read_citations(result)
                    if terminal_status is None:
                        errors.append(
                            "the run settled without a status field, so whether it rested is "
                            "unknown rather than unrecorded"
                        )
            except Exception as exc:  # noqa: BLE001 -- recorded, not swallowed
                errors.append(f"polling raised {type(exc).__name__}: {exc}")

        return {
            "tier": slot.tier.name,
            "concurrency": slot.tier.concurrency,
            "repeat_index": slot.repeat_index,
            "case_id": case.id,
            "subject": case.subject,
            "run_id": str(run_id) if run_id else None,
            "terminal_status": terminal_status,
            "reviewer_decision": decision,
            "citations": list(reading.source_record_ids),
            "expected_citations": list(case.expected_citations),
            "unreadable_citations": reading.unreadable,
            "latency_seconds": round(time.monotonic() - started, 3),
            "errors": errors,
            "observed_at": datetime.now(UTC).isoformat(),
            "deployed_revision": revision,
            "plan_digest": plan_digest_value,
            "workload_digest": workload_digest_value,
        }


async def _bounded(
    slots: Sequence[Slot],
    worker: Callable[[Slot], Awaitable[dict[str, Any]]],
    *,
    concurrency: int,
    on_done: Callable[[dict[str, Any]], None],
) -> list[dict[str, Any]]:
    """Run ``worker`` over ``slots`` with at most ``concurrency`` in flight.

    Written out rather than taken from a library because the interesting part is the
    incremental write below: observations are persisted as each run finishes, not at the end,
    so a sweep that dies at run 90 leaves 90 observations to grade rather than an empty
    directory and a question about what happened.
    """
    queue: asyncio.Queue[Slot] = asyncio.Queue()
    for slot in slots:
        queue.put_nowait(slot)
    results: list[dict[str, Any]] = []
    lock = asyncio.Lock()

    async def drain() -> None:
        while True:
            try:
                slot = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                record = await worker(slot)
            except Exception as exc:  # noqa: BLE001 -- one run must not end the tier
                record = {
                    "tier": slot.tier.name,
                    "concurrency": slot.tier.concurrency,
                    "repeat_index": slot.repeat_index,
                    "case_id": slot.case.id,
                    "subject": slot.case.subject,
                    "errors": [f"the worker raised {type(exc).__name__}: {exc}"],
                    "observed_at": datetime.now(UTC).isoformat(),
                }
            async with lock:
                results.append(record)
                on_done(record)

    await asyncio.gather(*(drain() for _ in range(max(1, concurrency))))
    return results


def _slots(plan: LoadPlan, workload: Workload, only: str | None) -> list[Slot]:
    tiers = [tier for tier in plan.tiers if only is None or tier.name == only]
    if only is not None and not tiers:
        raise SystemExit(
            f"no such tier: {only!r}; the plan declares {[t.name for t in plan.tiers]}"
        )
    return [
        Slot(tier=tier, repeat_index=repeat, case=case)
        for tier in tiers
        for repeat in range(tier.repeats)
        for case in workload.cases
    ]


async def run(args: argparse.Namespace) -> int:
    driver = _load_quality_driver().load_acceptance_driver()
    plan = load_plan(PLAN)
    workload = resolve_workload(plan, CASES)
    if plan.tenant_id != UUID(args.tenant_id):
        # A batch run against a different tenant than the plan declares would be recorded
        # under the plan's digest while describing somewhere else, which is the kind of
        # mismatch a reader has no way to see afterwards.
        raise SystemExit(
            f"the plan declares tenant {plan.tenant_id} and --tenant-id is {args.tenant_id}"
        )
    tickets = load_tickets(workload)
    revision = driver.source_revision()
    plan_digest_value = plan_digest(plan)
    workload_digest_value = workload_digest(workload)
    slots = _slots(plan, workload, args.only_tier)

    REPLAYS.mkdir(parents=True, exist_ok=True)
    stack = driver.Stack(
        base_url=args.base_url,
        tenant_id=plan.tenant_id,
        timeout_scale=1.0,
    )
    recorder = Recorder(stack=stack, tickets=tickets)

    done: list[dict[str, Any]] = []

    def persist(record: dict[str, Any]) -> None:
        (
            REPLAYS / f"{record['tier']}__r{record['repeat_index']}__{record['case_id']}.json"
        ).write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        done.append(record)
        marker = "ERR " if record.get("errors") else "    "
        print(
            f"{marker}{record['tier']:8} {record['case_id']} #{record['repeat_index']} "
            f"{record.get('terminal_status') or '-':18} "
            f"{record.get('reviewer_decision') or '-':16} "
            f"{record.get('latency_seconds', 0):>7.1f}s",
            flush=True,
        )

    print(
        f"running {len(slots)} run(s) over {len(workload.cases)} question(s) in "
        f"{len({slot.tier.name for slot in slots})} tier(s), revision {revision}",
        flush=True,
    )

    timings: list[dict[str, str]] = []
    try:
        await stack.await_health(seconds=args.health_timeout)
        for tier in plan.tiers:
            tier_slots = [slot for slot in slots if slot.tier.name == tier.name]
            if not tier_slots:
                continue
            print(
                f"\n--- {tier.name}: concurrency {tier.concurrency}, "
                f"{len(tier_slots)} run(s), repeats {tier.repeats}",
                flush=True,
            )
            started = datetime.now(UTC)
            await _bounded(
                tier_slots,
                lambda slot: recorder.observe(
                    slot,
                    plan_digest_value=plan_digest_value,
                    workload_digest_value=workload_digest_value,
                    revision=revision,
                    budget=plan.settle_budget_seconds,
                ),
                concurrency=tier.concurrency,
                on_done=persist,
            )
            finished = datetime.now(UTC)
            timings.append(
                {
                    "name": tier.name,
                    "started_at": started.isoformat(),
                    "finished_at": finished.isoformat(),
                }
            )
            print(
                f"--- {tier.name} done in {(finished - started).total_seconds():.1f}s",
                flush=True,
            )
    finally:
        await stack.aclose()

    errored = [record for record in done if record.get("errors")]
    BATCH.write_text(
        json.dumps(
            {
                "mode": "live",
                "base_url": args.base_url,
                "tenant_id": args.tenant_id,
                "recorded_at": datetime.now(UTC).isoformat(),
                "deployed_revision": revision,
                "plan_digest": plan_digest_value,
                "workload_digest": workload_digest_value,
                "workload_size": len(workload.cases),
                "tiers": timings,
                "errored": [f"{record['tier']}__{record['case_id']}" for record in errored],
                "elapsed_seconds": round(
                    sum(float(record.get("latency_seconds") or 0) for record in done), 3
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        f"\n{len(done)} observation(s) written to {REPLAYS.relative_to(REPO_ROOT)}; "
        f"{len(errored)} recorded driver errors",
        flush=True,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True, help="the tenant the workload is in")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--only-tier",
        default=None,
        help="a single tier name from the plan; the default is every declared tier",
    )
    parser.add_argument("--health-timeout", type=float, default=120.0)
    args = parser.parse_args()
    return asyncio.run(
        run(args), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())
    )


if __name__ == "__main__":
    sys.exit(main())
