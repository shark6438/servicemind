from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any


class SemanticModelCache:
    """Tenant-keyed process cache; PostgreSQL and model outputs remain authoritative."""

    def __init__(self, *, ttl_seconds: int = 300, max_entries: int = 1024) -> None:
        self.ttl = timedelta(seconds=ttl_seconds)
        self.max_entries = max_entries
        self._values: dict[str, tuple[datetime, Any]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            item = self._values.get(key)
            if item is None:
                return None
            created, value = item
            if datetime.now(UTC) - created >= self.ttl:
                self._values.pop(key, None)
                return None
            return value

    async def put(self, key: str, value: Any) -> None:
        async with self._lock:
            if len(self._values) >= self.max_entries:
                oldest = min(self._values, key=lambda item: self._values[item][0])
                self._values.pop(oldest, None)
            self._values[key] = (datetime.now(UTC), value)
