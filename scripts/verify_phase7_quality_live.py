"""Run the 200 quality cases against the live platform and record what happened.

This script **records**; it does not judge. Every verdict lives in
``servicemind.evaluation.quality_grader``, which is a pure function over these observation
files. The split is the same one the acceptance and security batches use, and it is what
makes the gate's replay mode meaningful: the judge can be re-run over the same observations
without a stack, and an observation cannot be quietly re-graded by re-running the platform
until it agrees.

**What is measured, exactly.** For each case: a run is submitted as the case's subject,
against the case's ticket, with the case's question as the goal and ``request_write=False``.
The run is polled to a resting status, then its persisted ``result`` is read for the
reviewer's decision and the citations. Those two readings plus the terminal status are the
whole observation. Nothing here knows what the answer *should* be -- that is in the case
list, and applying it is the grader's job.

**Concurrency is a recorded parameter, not a hidden one.** The default is four in flight.
A correctness measurement run at some concurrency is only interpretable if the concurrency
is written down, so it goes into every observation file's batch header along with the cases
digest. The per-case elapsed time is recorded too, which is what lets a reader see whether
the batch degraded as it went -- if the last fifty cases are much slower than the first
fifty, the concurrency is the first thing to suspect and the data says so.

**Why this reuses the acceptance driver's ``Stack``.** Not for the code, but because it is
the one client implementation in this repository that has been run against the real stack
and had its bugs found: the token cache there carries an expiry because a bare cached string
expired partway through a 28-case sweep and turned every later case into a 401 that read
like a platform refusal. A 200-case sweep is seven times longer, so that bug would be seven
times more certain. Copying the class here would copy the fix and the next omission with it.

Usage:

    uv run python scripts/verify_phase7_quality_live.py --tenant-id 2222... [--only QC-0007]
    uv run python scripts/verify_phase7_quality_live.py --concurrency 1 --only-kind must-refuse-access
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import selectors
import sys
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from servicemind.evaluation.quality import (
    CaseKind,
    QualityCase,
    case_set_digest,
    load_quality_cases,
    read_citations,
    read_review_signals,
    reviewer_decision,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
QUALITY = REPO_ROOT / "evaluation" / "quality"
CASES = QUALITY / "cases.v1.json"
TICKETS = QUALITY / "tickets.json"
REPLAYS = QUALITY / "replays"
BATCH = QUALITY / "replays" / "_batch.json"

ACCEPTANCE_DRIVER = REPO_ROOT / "scripts" / "verify_phase7_acceptance_live.py"

#: How often the poll re-reads a run, and the ceiling on how long a single read may take.
#: The ceiling is generous because a read is cheap and the cost of a spurious give-up --
#: a case recorded BLOCKED that would have succeeded -- is paid in the report's credibility.
POLL_SECONDS = 1.5

#: A read-only run on the acceptance set settles in tens of seconds. The budget is a
#: multiple of that rather than a tight fit, because a case that is genuinely wedged and a
#: case that is merely slow are indistinguishable at the deadline but very distinguishable
#: in the report, and only one of them should be reported as a platform defect.
DEFAULT_CASE_BUDGET_SECONDS = 240.0

DEFAULT_CONCURRENCY = 4
DEFAULT_BASE_URL = "http://127.0.0.1:18080"


def load_acceptance_driver() -> Any:
    """The acceptance driver as a module, for its ``Stack`` and its revision helper.

    Public because a third batch now uses it: ``scripts/verify_phase7_load_live.py`` loads
    *this* module to get here, rather than deriving a second copy of the live client and the
    three bugs already found in it. The name says what it returns and nothing about who
    calls it.

    Loaded by path rather than imported by name because ``scripts/`` is not a package. The
    module is guarded by ``if __name__ == "__main__"``, so importing it defines and runs
    nothing -- asserted below rather than assumed, since a driver that started a sweep at
    import time would be a spectacular way to discover the problem.
    """
    spec = importlib.util.spec_from_file_location("_acceptance_driver", ACCEPTANCE_DRIVER)
    if spec is None or spec.loader is None:  # pragma: no cover -- a broken checkout
        raise RuntimeError(f"cannot load {ACCEPTANCE_DRIVER}")
    module = importlib.util.module_from_spec(spec)
    # Registered before ``exec_module``, not after. ``@dataclass(slots=True)`` asks
    # ``dataclasses`` to re-resolve the annotations of its own class, and that lookup goes
    # through ``sys.modules[cls.__module__]``. A module built from a spec and executed
    # without being registered is invisible to that lookup, so the driver died on its first
    # ``@dataclass`` with "'NoneType' object has no attribute '__dict__'" -- an error about
    # the loader, raised from inside a decorator, a long way from the line that caused it.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[spec.name]
        raise
    for attribute in ("Stack", "SETTLED", "source_revision"):
        if not hasattr(module, attribute):
            raise RuntimeError(
                f"the acceptance driver no longer defines {attribute}; the quality driver "
                "reuses it rather than duplicating the live client, so this is a real break"
            )
    return module


def load_tickets() -> dict[str, int]:
    if not TICKETS.exists():
        raise SystemExit(
            f"{TICKETS.relative_to(REPO_ROOT)} does not exist; run "
            "scripts/seed_phase7_quality_fixtures.py first"
        )
    recorded = json.loads(TICKETS.read_text(encoding="utf-8"))
    return {str(name): int(value) for name, value in recorded["tickets"].items()}


def load_restriction_map() -> dict[str, frozenset[int]]:
    """Which groups each document is restricted to, per the corpus manifest.

    The runner needs this and the grader must not have it: whether the asker can reach a
    document is a fact about the deployment, and a judge that held deployment state would
    stop being a pure function of the observations.
    """
    manifest = json.loads((QUALITY / "manifest.json").read_text(encoding="utf-8"))
    return {
        str(entry["source_record_id"]): frozenset(int(value) for value in entry["group_ids"])
        for entry in manifest["documents"]
    }


def reachability_errors(
    case: QualityCase, restrictions: dict[str, frozenset[int]], groups: frozenset[int]
) -> list[str]:
    """Whether the asker can actually reach the document the case says they cannot.

    This is the precondition the whole must-refuse-access class rests on. If the token
    carries the group that owns the document, the case is not testing isolation -- it is
    testing nothing, and it would report a pass for a platform that leaked nothing only
    because nothing was ever out of reach.

    A module-level function rather than a method so it can be tested without a stack: it
    reads two declarations and does set arithmetic, and a rule that can only be exercised
    against a live deployment is a rule that stops being exercised.

    Returned as errors rather than raised: one mis-provisioned identity must not end a
    two-hundred-case sweep.
    """
    problems: list[str] = []
    for record_id in case.forbidden_citations:
        owner_groups = restrictions.get(record_id, frozenset())
        if not owner_groups:
            problems.append(
                f"{record_id} is declared forbidden but is not group-restricted in the "
                "manifest, so refusing it proves nothing"
            )
            continue
        reachable = owner_groups & groups
        if reachable:
            problems.append(
                f"{case.subject} actually holds group(s) {sorted(reachable)}, which own "
                f"{record_id}, so the case asserts no isolation"
            )
    return problems


class Recorder:
    """One case in flight: submit, settle, read, write the observation."""

    def __init__(
        self,
        *,
        stack: Any,
        tickets: dict[str, int],
        restrictions: dict[str, frozenset[int]],
        driver: Any,
    ) -> None:
        self.stack = stack
        self.tickets = tickets
        self.restrictions = restrictions
        self.driver = driver

    async def observe(
        self, case: QualityCase, *, cases_digest: str, revision: str | None, budget: float
    ) -> dict[str, Any]:
        started = time.monotonic()
        errors: list[str] = []
        run_id: UUID | None = None
        terminal_status: str | None = None
        decision: str | None = None
        observed_username: str | None = None
        reading = read_citations(None)
        signals = read_review_signals(None)
        payload: dict[str, Any] = {}

        try:
            observed = await self.stack.observed_subject(case.subject)
            observed_username = observed.username
            if case.kind is CaseKind.MUST_REFUSE_ACCESS:
                errors.extend(
                    reachability_errors(case, self.restrictions, frozenset(observed.group_ids))
                )
        except Exception as exc:  # noqa: BLE001 -- recorded, not swallowed
            errors.append(f"reading the caller's own claims raised {type(exc).__name__}: {exc}")

        ticket_id = self.tickets.get(case.ticket_ref)
        if ticket_id is None:
            errors.append(f"no resolved ticket id for {case.ticket_ref}")
        else:
            try:
                response = await self.stack.create_run(
                    case.subject,
                    ticket_id=ticket_id,
                    goal=case.question,
                    request_write=False,
                )
                if response.status_code not in (200, 201, 202):
                    errors.append(
                        f"POST /runs returned {response.status_code}: {response.text[:400]}"
                    )
                else:
                    # ``id``, which is ``RunView``'s field. It is not ``run_id``: that is the
                    # *webhook* response's field (``WebhookAccepted``), and taking the name
                    # from there cost every case in the first smoke run -- the recorder read
                    # a key that endpoint never sends and recorded four KeyErrors against a
                    # platform that had answered 202 four times.
                    body = response.json()
                    if "id" not in body:
                        errors.append(
                            "POST /runs returned no ``id``; the body has "
                            f"{sorted(body)}. The run was created and its id is lost, so "
                            "this case cannot be observed."
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
                    signals = read_review_signals(result)
            except Exception as exc:  # noqa: BLE001 -- recorded, not swallowed
                errors.append(f"polling raised {type(exc).__name__}: {exc}")

        return {
            "case_id": case.id,
            "kind": case.kind.value,
            "subject": case.subject,
            "ticket_ref": case.ticket_ref,
            "question": case.question,
            "run_id": str(run_id) if run_id else None,
            "terminal_status": terminal_status,
            "reviewer_decision": decision,
            "citations": list(reading.source_record_ids),
            "observed_username": observed_username,
            "unreadable_citations": reading.unreadable,
            "evidence_rows_without_citation": reading.without_citation,
            # The reviewer's own account of claims its evidence does not carry. Recorded for
            # every case, read only by the insufficient-evidence rule -- and recorded as the
            # signed counts ``ReviewSignals`` produces, where -1 means "the field was not
            # there", because a run whose response shape changed must not read as one that
            # was checked and came back clean.
            "unsupported_claims": signals.unsupported_claims,
            "missing_evidence": signals.missing_evidence,
            "proposed_actions": signals.proposed_actions,
            "review_findings": signals.findings,
            "errors": errors,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "observed_at": datetime.now(UTC).isoformat(),
            "deployed_revision": revision,
            "cases_digest": cases_digest,
        }


async def _bounded(
    items: Sequence[QualityCase],
    worker: Callable[[QualityCase], Awaitable[dict[str, Any]]],
    *,
    concurrency: int,
    on_done: Callable[[dict[str, Any]], None],
) -> list[dict[str, Any]]:
    """Run ``worker`` over ``items`` with at most ``concurrency`` in flight.

    Written out rather than taken from a library because the interesting part is the
    incremental write below: observations are persisted as each case finishes, not at the
    end, so a sweep that dies at case 180 leaves 180 observations to grade rather than an
    empty directory and a question about what happened.
    """
    queue: asyncio.Queue[QualityCase] = asyncio.Queue()
    for item in items:
        queue.put_nowait(item)
    results: list[dict[str, Any]] = []
    lock = asyncio.Lock()

    async def drain() -> None:
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                record = await worker(item)
            except Exception as exc:  # noqa: BLE001 -- one case must not end the sweep
                record = {
                    "case_id": item.id,
                    "kind": item.kind.value,
                    "subject": item.subject,
                    "question": item.question,
                    "errors": [f"the worker raised {type(exc).__name__}: {exc}"],
                    "observed_at": datetime.now(UTC).isoformat(),
                }
            async with lock:
                results.append(record)
                on_done(record)

    await asyncio.gather(*(drain() for _ in range(max(1, concurrency))))
    return results


def _select(cases: Iterable[QualityCase], only: str | None, kinds: set[str]) -> list[QualityCase]:
    chosen = list(cases)
    if only:
        wanted = {item.strip() for item in only.split(",") if item.strip()}
        chosen = [case for case in chosen if case.id in wanted]
        missing = wanted - {case.id for case in chosen}
        if missing:
            raise SystemExit(f"no such case id: {sorted(missing)}")
    if kinds:
        chosen = [case for case in chosen if case.kind.value in kinds]
    return chosen


async def run(args: argparse.Namespace) -> int:
    driver = load_acceptance_driver()
    case_set = load_quality_cases(CASES)
    digest = case_set_digest(case_set)
    tickets = load_tickets()
    revision = driver.source_revision()
    kinds = {item.strip() for item in (args.only_kind or "").split(",") if item.strip()}
    selected = _select(case_set.cases, args.only, kinds)

    if not selected:
        raise SystemExit("the filters selected no cases")

    tenant_id = UUID(args.tenant_id)
    REPLAYS.mkdir(parents=True, exist_ok=True)
    stack = driver.Stack(base_url=args.base_url, tenant_id=tenant_id, timeout_scale=1.0)
    recorder = Recorder(
        stack=stack,
        tickets=tickets,
        restrictions=load_restriction_map(),
        driver=driver,
    )

    #: Written per case as it lands, so the directory on disk is always a prefix of the
    #: sweep rather than a snapshot of its end.
    done: list[dict[str, Any]] = []

    def persist(record: dict[str, Any]) -> None:
        (REPLAYS / f"{record['case_id']}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        done.append(record)
        status = record.get("terminal_status")
        decision = record.get("reviewer_decision")
        errors = record.get("errors") or []
        marker = "ERR " if errors else "    "
        print(
            f"{marker}{record['case_id']} {record.get('kind', '')[:22]:22} "
            f"{status or '-':18} {decision or '-':16} "
            f"{len(record.get('citations') or []):>2} cited  "
            f"{record.get('elapsed_seconds', 0):>6.1f}s",
            flush=True,
        )

    print(
        f"running {len(selected)} of {len(case_set.cases)} cases, concurrency {args.concurrency}, "
        f"cases_digest {digest[:12]}, revision {revision}",
        flush=True,
    )
    try:
        await stack.await_health(seconds=args.health_timeout)
        await _bounded(
            selected,
            lambda case: recorder.observe(
                case, cases_digest=digest, revision=revision, budget=args.budget
            ),
            concurrency=args.concurrency,
            on_done=persist,
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
                "cases_digest": digest,
                "concurrency": args.concurrency,
                "case_budget_seconds": args.budget,
                "selected": [case.id for case in selected],
                "counts": case_set.counts(),
                "errored": [record["case_id"] for record in errored],
                "elapsed_seconds": round(
                    sum(float(record.get("elapsed_seconds") or 0) for record in done), 2
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        f"\n{len(done)} observations written to {REPLAYS.relative_to(REPO_ROOT)}; "
        f"{len(errored)} recorded driver errors",
        flush=True,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True, help="the tenant the quality corpus is in")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--only",
        default=None,
        help="a comma-separated list of case ids; the default is every case",
    )
    parser.add_argument(
        "--only-kind",
        default=None,
        help="a comma-separated list of case kinds, one of "
        + ", ".join(kind.value for kind in CaseKind),
    )
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--budget", type=float, default=DEFAULT_CASE_BUDGET_SECONDS)
    parser.add_argument("--health-timeout", type=float, default=120.0)
    args = parser.parse_args()
    return asyncio.run(
        run(args), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())
    )


if __name__ == "__main__":
    sys.exit(main())
