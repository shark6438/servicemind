"""Cost-control semantics for the optional Jev router.

The ledger is the only thing standing between a request storm and an unbounded bill,
so these tests pin the conservative direction: reserve before the call, settle what the
provider says, and never forget spend that cannot be proven free.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from servicemind.orchestration.jev_ledger import (
    JevBudgetExhausted,
    JevJournalCorrupt,
    JevLedgerUnavailable,
    JevRateLimited,
    JevSpendLedger,
)

DAY = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self, moment: datetime = DAY) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def set(self, moment: datetime) -> None:
        self.moment = moment


def make_ledger(
    directory: Path,
    *,
    clock: Clock | None = None,
    budget: str = "10",
    per_minute: int = 100,
    lock_timeout: float = 5.0,
) -> JevSpendLedger:
    return JevSpendLedger(
        directory,
        daily_budget_usd=Decimal(budget),
        price_usd_per_million_input_tokens=Decimal("0.042"),
        per_minute_limit=per_minute,
        request_model="typesafe/jev-1.14",
        clock=clock or Clock(),
        lock_timeout_seconds=lock_timeout,
    )


def events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_reserve_is_written_before_the_call_and_settle_records_provider_cost(
    tmp_path: Path,
) -> None:
    ledger = make_ledger(tmp_path)
    reservation = ledger.reserve(payload_bytes=3000, payload_sha256="a" * 64, run_id="run-1")

    written = events(reservation.path)
    assert written[0] == {"event": "ledger", "version": 1}
    assert [event["event"] for event in written[1:]] == ["reserve"]
    assert written[1]["payload_sha256"] == "a" * 64

    pending = ledger.snapshot()
    assert pending.pending_usd == reservation.amount_usd
    assert pending.accounted_usd == reservation.amount_usd
    assert pending.settled_usd == 0

    ledger.settle(
        reservation,
        cost_usd=Decimal("0.0000042"),
        input_tokens=100,
        output_tokens=5,
        status=200,
    )

    settled = ledger.snapshot()
    assert settled.attempts == 1
    assert settled.pending_usd == 0
    assert settled.settled_usd == Decimal("0.0000042")
    assert settled.accounted_usd == Decimal("0.0000042")


def test_daily_budget_refuses_before_any_request_is_made(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path, budget="0.00015")
    first = ledger.reserve(payload_bytes=3000, payload_sha256="b" * 64, run_id=None)

    with pytest.raises(JevBudgetExhausted):
        ledger.reserve(payload_bytes=3000, payload_sha256="c" * 64, run_id=None)

    reserves = [event for event in events(first.path) if event["event"] == "reserve"]
    assert len(reserves) == 1


def test_per_minute_cap_refuses_before_any_request_is_made(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path, per_minute=2)
    first = ledger.reserve(payload_bytes=100, payload_sha256="d" * 64, run_id=None)
    ledger.reserve(payload_bytes=100, payload_sha256="e" * 64, run_id=None)

    with pytest.raises(JevRateLimited):
        ledger.reserve(payload_bytes=100, payload_sha256="f" * 64, run_id=None)

    reserves = [event for event in events(first.path) if event["event"] == "reserve"]
    assert len(reserves) == 2


def test_unknown_usage_keeps_the_reservation_and_never_refunds_it(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path)
    reservation = ledger.reserve(payload_bytes=3000, payload_sha256="1" * 64, run_id=None)

    ledger.mark_unknown(reservation, error_code="timeout", http_status=None)

    snapshot = ledger.snapshot()
    assert snapshot.pending_usd == 0
    assert snapshot.unknown_usd == reservation.amount_usd
    assert snapshot.accounted_usd == reservation.amount_usd

    with pytest.raises(JevJournalCorrupt):
        ledger.mark_unknown(reservation, error_code="timeout", http_status=None)


def test_a_crashed_reservation_still_counts_against_the_budget(tmp_path: Path) -> None:
    crashed = make_ledger(tmp_path)
    reservation = crashed.reserve(payload_bytes=3000, payload_sha256="2" * 64, run_id="run-crashed")

    survivor = make_ledger(tmp_path)
    snapshot = survivor.snapshot()
    assert snapshot.pending_usd == reservation.amount_usd
    assert snapshot.accounted_usd == reservation.amount_usd


def test_corrupt_journal_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "spend-2026-10-28.jsonl"
    path.write_text("this is not json\n", encoding="utf-8")

    with pytest.raises(JevJournalCorrupt):
        make_ledger(tmp_path).reserve(payload_bytes=100, payload_sha256="3" * 64, run_id=None)


def test_unwritable_location_fails_closed(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    with pytest.raises(JevLedgerUnavailable):
        make_ledger(blocker).reserve(payload_bytes=100, payload_sha256="4" * 64, run_id=None)


def test_settle_after_midnight_lands_in_the_reservation_day_file(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 10, 28, 23, 59, tzinfo=UTC))
    ledger = make_ledger(tmp_path, clock=clock)
    reservation = ledger.reserve(payload_bytes=3000, payload_sha256="5" * 64, run_id=None)

    clock.set(datetime(2026, 10, 29, 0, 0, 30, tzinfo=UTC))
    ledger.settle(
        reservation,
        cost_usd=Decimal("0.0000042"),
        input_tokens=100,
        output_tokens=5,
        status=200,
    )

    day_one = events(tmp_path / "spend-2026-10-28.jsonl")
    assert [event["event"] for event in day_one] == ["ledger", "reserve", "settle"]
    assert ledger.snapshot().attempts == 0  # the new day starts empty


def test_concurrent_reservations_are_not_lost(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path, budget="100", lock_timeout=30.0)

    def reserve(index: int) -> str:
        return ledger.reserve(
            payload_bytes=100,
            payload_sha256=f"{index:064d}",
            run_id=f"run-{index}",
        ).attempt_id

    with ThreadPoolExecutor(max_workers=4) as pool:
        identifiers = list(pool.map(reserve, range(20)))

    assert len(set(identifiers)) == 20
    written = events(tmp_path / "spend-2026-10-28.jsonl")
    assert [event["event"] for event in written].count("reserve") == 20
    assert ledger.snapshot().attempts == 20


def test_journal_holds_no_request_text_or_secret(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path)
    reservation = ledger.reserve(payload_bytes=3000, payload_sha256="6" * 64, run_id="run-1")
    ledger.settle(
        reservation,
        cost_usd=Decimal("0.0000042"),
        input_tokens=100,
        output_tokens=5,
        status=200,
    )

    text = reservation.path.read_text(encoding="utf-8")
    assert "delete ticket" not in text
    assert "Bearer" not in text
    allowed = {
        "event",
        "version",
        "id",
        "usd",
        "at",
        "payload_sha256",
        "run_id",
        "model",
        "input_tokens",
        "output_tokens",
        "status",
    }
    for event in events(reservation.path):
        assert set(event) <= allowed


def test_reservation_amount_scales_with_payload_and_has_a_floor(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path)
    small = ledger.reservation_amount(1)
    large = ledger.reservation_amount(30_000)
    assert small == Decimal("0.000001")
    assert large > small
