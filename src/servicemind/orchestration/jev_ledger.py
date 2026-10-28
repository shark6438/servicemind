"""Durable spend journal for the optional Jev routing advisor.

The journal is the cost authority for the router: a call is made only after a
reservation has been durably written and the daily / per-minute caps admit it. The
implementation is deliberately synchronous so callers can run it in a worker thread
(``asyncio.to_thread``) and a slow disk can never block the event loop.

The journal is an append-only JSONL file per UTC day. A reservation is *never*
refunded: a provider error, timeout or process crash leaves spend that cannot be
proven unbilled counted against the cap. That is the conservative direction -- the
historical v3.1 run learned that an HTTP status alone does not prove a request was
free.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import IO, Any

try:  # pragma: no cover - the fallback exists only for non-POSIX developer shells.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


class JevLedgerError(RuntimeError):
    """Base class for refusals that must fall back to the rule router."""


class JevLedgerUnavailable(JevLedgerError):
    """The journal cannot be read, written or locked; do not spend."""


class JevBudgetExhausted(JevLedgerError):
    """The daily USD cap would be exceeded by this reservation."""


class JevRateLimited(JevLedgerError):
    """The per-minute reservation cap has been reached."""


class JevJournalCorrupt(JevLedgerUnavailable):
    """The journal cannot be interpreted; never guess at spend."""


JOURNAL_VERSION = 1
LOCK_POLL_SECONDS = 0.005
MIN_RESERVATION_USD = Decimal("0.000001")
RESERVATION_SAFETY_FACTOR = Decimal("1.25")
#: The journal stores whole dollars at nanodollar precision; more digits are noise.
USD_QUANTUM = Decimal("0.000000001")


def _usd(value: Decimal) -> str:
    return format(value, "f")


def _epoch(value: Any, *, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise JevJournalCorrupt(f"invalid {field}") from exc
    if not math.isfinite(parsed):
        raise JevJournalCorrupt(f"invalid {field}")
    return parsed


def _decimal(value: Any, *, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise JevJournalCorrupt(f"invalid {field}") from exc
    if not parsed.is_finite() or parsed < 0:
        raise JevJournalCorrupt(f"invalid {field}")
    return parsed


@dataclass(frozen=True)
class JevReservation:
    """One reserved attempt. ``path`` is the day file the reservation lives in."""

    attempt_id: str
    path: Path
    amount_usd: Decimal


@dataclass(frozen=True)
class JevLedgerSnapshot:
    day: str
    budget_usd: Decimal
    accounted_usd: Decimal
    settled_usd: Decimal
    unknown_usd: Decimal
    pending_usd: Decimal
    attempts: int
    reservations_last_minute: int


@dataclass
class _Attempt:
    reserved_usd: Decimal
    reserved_at: float
    terminal_usd: Decimal | None = None
    terminal_kind: str | None = None


class _Journal:
    """Append handle exposed while the exclusive lock is held."""

    def __init__(self, handle: IO[str]) -> None:
        self._handle = handle

    def append(self, event: dict[str, Any]) -> None:
        self._handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


class JevSpendLedger:
    """Reservation journal with a daily USD cap and a per-minute request cap."""

    def __init__(
        self,
        directory: Path,
        *,
        daily_budget_usd: Decimal,
        price_usd_per_million_input_tokens: Decimal,
        per_minute_limit: int,
        request_model: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        lock_timeout_seconds: float = 1.0,
    ) -> None:
        if daily_budget_usd <= 0:
            raise ValueError("daily budget must be positive")
        if price_usd_per_million_input_tokens <= 0:
            raise ValueError("price must be positive")
        if per_minute_limit < 1:
            raise ValueError("per-minute limit must be positive")
        if lock_timeout_seconds <= 0:
            raise ValueError("lock timeout must be positive")
        self.directory = directory
        self.daily_budget_usd = daily_budget_usd
        self.price_usd_per_million_input_tokens = price_usd_per_million_input_tokens
        self.per_minute_limit = per_minute_limit
        self.request_model = request_model
        self._clock = clock
        self._lock_timeout_seconds = lock_timeout_seconds
        self._thread_lock = threading.Lock()

    # -- public API ---------------------------------------------------------

    def reserve(
        self,
        *,
        payload_bytes: int,
        payload_sha256: str,
        run_id: str | None,
    ) -> JevReservation:
        """Durably reserve spend before the HTTP call; raises instead of overspending."""
        if payload_bytes < 1:
            raise ValueError("payload must not be empty")
        now = self._clock()
        day = now.date().isoformat()
        path = self._path_for(day)
        amount = self.reservation_amount(payload_bytes)
        attempt_id = uuid.uuid4().hex
        with self._locked(path, now=now) as (attempts, journal):
            snapshot = self._snapshot(attempts, now)
            if snapshot.accounted_usd + amount > self.daily_budget_usd:
                raise JevBudgetExhausted("daily budget exhausted")
            if snapshot.reservations_last_minute >= self.per_minute_limit:
                raise JevRateLimited("per-minute reservation cap reached")
            journal.append(
                {
                    "event": "reserve",
                    "id": attempt_id,
                    "usd": _usd(amount),
                    "at": now.timestamp(),
                    "payload_sha256": payload_sha256,
                    "run_id": run_id,
                    "model": self.request_model,
                }
            )
        return JevReservation(attempt_id=attempt_id, path=path, amount_usd=amount)

    def settle(
        self,
        reservation: JevReservation,
        *,
        cost_usd: Decimal,
        input_tokens: int,
        output_tokens: int,
        status: int,
    ) -> None:
        """Record the provider-reported cost; called before the decision is validated.

        A provider can bill a response whose decision we end up rejecting, so this
        must not depend on response validity.
        """
        if cost_usd < 0 or not cost_usd.is_finite():
            raise ValueError("cost must be finite and non-negative")
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("token counts must be non-negative")
        self._append_terminal(
            reservation,
            {
                "event": "settle",
                "id": reservation.attempt_id,
                "usd": _usd(cost_usd),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "status": status,
            },
        )

    def mark_unknown(
        self,
        reservation: JevReservation,
        *,
        error_code: str,
        http_status: int | None,
    ) -> None:
        """Retain the reservation when usage cannot be proven; never refund it."""
        self._append_terminal(
            reservation,
            {
                "event": "unknown",
                "id": reservation.attempt_id,
                "usd": _usd(reservation.amount_usd),
                "error_code": error_code,
                "status": http_status,
            },
        )

    def snapshot(self) -> JevLedgerSnapshot:
        now = self._clock()
        path = self._path_for(now.date().isoformat())
        with self._locked(path, now=now) as (attempts, _journal):
            return self._snapshot(attempts, now)

    def reservation_amount(self, payload_bytes: int) -> Decimal:
        """Conservative per-request ceiling used only for cap admission."""
        estimated_tokens = max(1, (payload_bytes * 2) // 3)
        amount = (
            Decimal(estimated_tokens) * self.price_usd_per_million_input_tokens / Decimal(1_000_000)
        ) * RESERVATION_SAFETY_FACTOR
        return max(amount.quantize(USD_QUANTUM), MIN_RESERVATION_USD)

    # -- journal mechanics --------------------------------------------------

    def _path_for(self, day: str) -> Path:
        return self.directory / f"spend-{day}.jsonl"

    @contextmanager
    def _locked(
        self, path: Path, *, now: datetime
    ) -> Iterator[tuple[dict[str, _Attempt], _Journal]]:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            handle = path.open("a+", encoding="utf-8")
        except OSError as exc:
            raise JevLedgerUnavailable("ledger directory is not writable") from exc
        locked = False
        try:
            self._acquire(handle)
            locked = True
            handle.seek(0)
            raw = handle.read()
            if raw.strip():
                attempts = self._parse(raw)
            else:
                attempts = {}
                handle.seek(0)
                handle.write(
                    json.dumps(
                        {"event": "ledger", "version": JOURNAL_VERSION},
                        sort_keys=True,
                    )
                    + "\n"
                )
            yield attempts, _Journal(handle)
            handle.flush()
            os.fsync(handle.fileno())
        except OSError as exc:
            raise JevLedgerUnavailable("ledger write failed") from exc
        finally:
            if locked:
                self._release(handle)
            handle.close()

    def _acquire(self, handle: IO[str]) -> None:
        if fcntl is None:  # pragma: no cover - non-POSIX developer shell only.
            if not self._thread_lock.acquire(timeout=self._lock_timeout_seconds):
                raise JevLedgerUnavailable("ledger lock timeout")
            return
        deadline = time.monotonic() + self._lock_timeout_seconds
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise JevLedgerUnavailable("ledger lock timeout") from None
                time.sleep(LOCK_POLL_SECONDS)

    def _release(self, handle: IO[str]) -> None:
        if fcntl is None:  # pragma: no cover
            self._thread_lock.release()
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass

    def _parse(self, raw: str) -> dict[str, _Attempt]:
        lines = [line for line in raw.splitlines() if line.strip()]
        attempts: dict[str, _Attempt] = {}
        for number, line in enumerate(lines, 1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise JevJournalCorrupt(f"corrupt ledger line {number}") from exc
            if not isinstance(event, dict):
                raise JevJournalCorrupt(f"non-object ledger line {number}")
            kind = event.get("event")
            if number == 1:
                if kind != "ledger" or event.get("version") != JOURNAL_VERSION:
                    raise JevJournalCorrupt("missing or incompatible ledger header")
                continue
            if kind == "reserve":
                identifier = event.get("id")
                if not isinstance(identifier, str) or not identifier:
                    raise JevJournalCorrupt("reserve without id")
                if identifier in attempts:
                    raise JevJournalCorrupt("duplicate reservation id")
                attempts[identifier] = _Attempt(
                    reserved_usd=_decimal(event.get("usd"), field="reserve usd"),
                    reserved_at=_epoch(event.get("at"), field="reserve at"),
                )
            elif kind in {"settle", "unknown"}:
                identifier = event.get("id")
                attempt = attempts.get(identifier) if isinstance(identifier, str) else None
                if attempt is None:
                    raise JevJournalCorrupt("terminal event without reservation")
                if attempt.terminal_usd is not None:
                    raise JevJournalCorrupt("duplicate terminal event")
                terminal_usd = _decimal(event.get("usd"), field=f"{kind} usd")
                if kind == "unknown" and terminal_usd != attempt.reserved_usd:
                    # An unknown attempt retains exactly its reservation; any other
                    # amount means the journal was edited or truncated and cannot be
                    # trusted to answer "how much may we still spend".
                    raise JevJournalCorrupt("unknown amount does not match reservation")
                attempt.terminal_usd = terminal_usd
                attempt.terminal_kind = kind
            else:
                raise JevJournalCorrupt(f"unknown ledger event {kind!r}")
        return attempts

    def _append_terminal(self, reservation: JevReservation, event: dict[str, Any]) -> None:
        now = self._clock()
        with self._locked(reservation.path, now=now) as (attempts, journal):
            attempt = attempts.get(reservation.attempt_id)
            if attempt is None:
                raise JevJournalCorrupt("reservation is not in its day file")
            if attempt.terminal_usd is not None:
                raise JevJournalCorrupt("attempt already settled")
            journal.append(event)

    def _snapshot(self, attempts: dict[str, _Attempt], now: datetime) -> JevLedgerSnapshot:
        settled = Decimal("0")
        unknown = Decimal("0")
        pending = Decimal("0")
        for attempt in attempts.values():
            if attempt.terminal_usd is None:
                pending += attempt.reserved_usd
            elif attempt.terminal_kind == "unknown":
                unknown += attempt.terminal_usd
            else:
                settled += attempt.terminal_usd
        return JevLedgerSnapshot(
            day=now.date().isoformat(),
            budget_usd=self.daily_budget_usd,
            accounted_usd=settled + unknown + pending,
            settled_usd=settled,
            unknown_usd=unknown,
            pending_usd=pending,
            attempts=len(attempts),
            reservations_last_minute=sum(
                1
                for attempt in attempts.values()
                # A clock that jumped backwards must not make recent reservations
                # disappear from the window; anything not provably older than a
                # minute counts.
                if now.timestamp() - attempt.reserved_at < 60
            ),
        )
