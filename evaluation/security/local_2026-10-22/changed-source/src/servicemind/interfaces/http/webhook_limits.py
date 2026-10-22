import time
from collections import deque
from collections.abc import AsyncIterator

from fastapi import HTTPException


class WebhookAdmission:
    def __init__(self, *, per_minute: int = 120, concurrent: int = 8):
        self.per_minute = per_minute
        self.concurrent = concurrent
        self._arrivals: deque[float] = deque()
        self._active = 0

    async def __call__(self) -> AsyncIterator[None]:
        now = time.monotonic()
        while self._arrivals and self._arrivals[0] <= now - 60:
            self._arrivals.popleft()
        if len(self._arrivals) >= self.per_minute or self._active >= self.concurrent:
            raise HTTPException(
                status_code=429, detail="Webhook capacity exceeded", headers={"Retry-After": "60"}
            )
        self._arrivals.append(now)
        self._active += 1
        try:
            yield
        finally:
            self._active -= 1


webhook_admission = WebhookAdmission()
