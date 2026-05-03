import asyncio
import random
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
