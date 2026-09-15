from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID


class RateLimitExceeded(RuntimeError):
    pass


class CircuitOpen(RuntimeError):
    pass


class TenantRateLimiter(Protocol):
    async def acquire(self, tenant_id: UUID, tool_name: str) -> None: ...


class SlidingWindowRateLimiter:
    def __init__(self, limit: int = 60, window_seconds: float = 60) -> None:
        if limit < 1 or window_seconds <= 0:
            raise ValueError("rate limit and window must be positive")
        self.limit, self.window_seconds = limit, window_seconds
        self._events: dict[tuple[UUID, str], deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def acquire(self, tenant_id: UUID, tool_name: str) -> None:
        now = time.monotonic()
        async with self._lock:
            events = self._events[(tenant_id, tool_name)]
            while events and events[0] <= now - self.window_seconds:
                events.popleft()
            if len(events) >= self.limit:
                raise RateLimitExceeded("tenant tool rate limit exceeded")
            events.append(now)


class RedisTenantRateLimiter:
    """Atomic distributed fixed-window limiter; keys contain tenant + tool only."""

    _SCRIPT = """
    local current = redis.call('INCR', KEYS[1])
    if current == 1 then redis.call('PEXPIRE', KEYS[1], ARGV[1]) end
    return current
    """

    def __init__(self, client: Any, *, limit: int = 60, window_seconds: int = 60) -> None:
        self.client, self.limit, self.window_ms = client, limit, window_seconds * 1000

    async def acquire(self, tenant_id: UUID, tool_name: str) -> None:
        safe_tool = tool_name.replace(":", "_")
        key = f"servicemind:tool-rate:{tenant_id}:{safe_tool}"
        current = int(await self.client.eval(self._SCRIPT, 1, key, self.window_ms))
        if current > self.limit:
            raise RateLimitExceeded("tenant tool rate limit exceeded")


@dataclass
class _CircuitState:
    failures: int = 0
    opened_at: float | None = None


class CircuitBreaker:
    def __init__(self, *, failure_threshold: int = 5, reset_seconds: float = 30) -> None:
        self.failure_threshold, self.reset_seconds = failure_threshold, reset_seconds
        self._states: dict[str, _CircuitState] = defaultdict(_CircuitState)
        self._lock = asyncio.Lock()

    async def before(self, provider: str) -> None:
        async with self._lock:
            state = self._states[provider]
            if state.opened_at is not None:
                if time.monotonic() - state.opened_at < self.reset_seconds:
                    raise CircuitOpen(f"tool provider circuit is open: {provider}")
                state.opened_at = None
                state.failures = 0

    async def success(self, provider: str) -> None:
        async with self._lock:
            self._states[provider] = _CircuitState()

    async def failure(self, provider: str) -> None:
        async with self._lock:
            state = self._states[provider]
            state.failures += 1
            if state.failures >= self.failure_threshold:
                state.opened_at = time.monotonic()


class Bulkheads:
    def __init__(self, per_tool_limit: int = 8) -> None:
        if per_tool_limit < 1:
            raise ValueError("bulkhead limit must be positive")
        self.limit = per_tool_limit
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    def for_tool(self, name: str) -> asyncio.Semaphore:
        return self._semaphores.setdefault(name, asyncio.Semaphore(self.limit))


class IdempotencyLedger:
    def __init__(self, *, max_entries: int = 10_000, ttl_seconds: float = 600) -> None:
        if max_entries < 1 or ttl_seconds <= 0:
            raise ValueError("idempotency capacity and TTL must be positive")
        self.max_entries, self.ttl_seconds = max_entries, ttl_seconds
        self._locks: dict[tuple[UUID, str], asyncio.Lock] = {}
        self._hashes: dict[tuple[UUID, str], str] = {}
        self._results: dict[tuple[UUID, str], Any] = {}
        self._completed: OrderedDict[tuple[UUID, str], float] = OrderedDict()
        self._guard = asyncio.Lock()

    def _remove(self, identity: tuple[UUID, str]) -> None:
        self._locks.pop(identity, None)
        self._hashes.pop(identity, None)
        self._results.pop(identity, None)
        self._completed.pop(identity, None)

    def _prune(self, now: float) -> None:
        while self._completed:
            identity, completed_at = next(iter(self._completed.items()))
            if completed_at > now - self.ttl_seconds and len(self._hashes) < self.max_entries:
                break
            self._remove(identity)

    async def lock(self, tenant_id: UUID, key: str, argument_hash: str) -> asyncio.Lock:
        identity = (tenant_id, key)
        async with self._guard:
            previous = self._hashes.get(identity)
            if previous is not None and previous != argument_hash:
                raise ValueError("idempotency key was reused with different arguments")
            if previous is None:
                self._prune(time.monotonic())
                if len(self._hashes) >= self.max_entries:
                    raise RuntimeError("idempotency ledger capacity exhausted")
            self._hashes[identity] = argument_hash
            return self._locks.setdefault(identity, asyncio.Lock())

    def get(self, tenant_id: UUID, key: str) -> Any | None:
        return self._results.get((tenant_id, key))

    def complete(self, tenant_id: UUID, key: str, result: Any) -> None:
        identity = (tenant_id, key)
        self._results[identity] = result
        self._completed[identity] = time.monotonic()
        self._completed.move_to_end(identity)

    async def abort(self, tenant_id: UUID, key: str) -> None:
        async with self._guard:
            identity = (tenant_id, key)
            # Keep the argument hash as a bounded tombstone so a failed request ID
            # cannot be replayed with different content.
            self._results.pop(identity, None)
            self._completed[identity] = time.monotonic()
            self._completed.move_to_end(identity)
