import asyncio
import random
import time
from dataclasses import dataclass


@dataclass
class Backoff:
    initial: float = 1.0
    factor: float = 2.0
    maximum: float = 30.0
    jitter: float = 0.25

    def delay(self, attempt: int) -> float:
        base = min(self.initial * (self.factor ** attempt), self.maximum)
        j = base * self.jitter
        return base + random.uniform(-j, j)


async def sleep_backoff(backoff: Backoff, attempt: int) -> None:
    await asyncio.sleep(max(0.0, backoff.delay(attempt)))


class RateLimiter:
    """Token-bucket rate limiter for async API calls."""

    def __init__(self, max_per_minute: int):
        self._interval = 60.0 / max_per_minute
        self._next_ok = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next_ok - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_ok = time.monotonic() + self._interval
