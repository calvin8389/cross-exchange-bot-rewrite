import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import websockets

from src.util.retry import Backoff, sleep_backoff

logger = logging.getLogger(__name__)


class StaleDataError(RuntimeError):
    pass


@dataclass
class UserStatsSnapshot:
    available: float
    portfolio: float
    ts_epoch: float


class LighterUserStatsService:
    def __init__(self, ws_url: str, account_index: int):
        self.ws_url = ws_url
        self.account_index = account_index

        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()

        self._snap: Optional[UserStatsSnapshot] = None
        self._ready = asyncio.Event()

    async def start(self) -> None:
        if self._task:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="lighter-userstats")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def wait_ready(self, timeout: float = 30.0) -> None:
        await asyncio.wait_for(self._ready.wait(), timeout=timeout)

    async def get_balance(self, max_age_seconds: float = 10.0) -> Tuple[float, float]:
        now = time.time()
        async with self._lock:
            if not self._snap:
                raise StaleDataError("no user_stats")
            age = now - self._snap.ts_epoch
            if age > max_age_seconds:
                raise StaleDataError(f"user_stats stale age={age:.2f}s")
            return self._snap.available, self._snap.portfolio

    async def _run(self) -> None:
        backoff = Backoff()
        attempt = 0
        sub_msg = {"type": "subscribe", "channel": f"user_stats/{self.account_index}"}

        while not self._stop.is_set():
            try:
                async with websockets.connect(self.ws_url, ping_interval=None) as ws:
                    attempt = 0
                    await ws.send(json.dumps(sub_msg))
                    while not self._stop.is_set():
                        raw = await ws.recv()
                        msg = json.loads(raw)
                        t = msg.get("type")
                        if t == "ping":
                            await ws.send(json.dumps({"type": "pong"}))
                            continue
                        if t not in ("update/user_stats", "subscribed/user_stats"):
                            continue
                        stats = msg.get("stats") or {}
                        avail = float(stats.get("available_balance", 0) or 0)
                        port = float(stats.get("portfolio_value", 0) or 0)
                        async with self._lock:
                            self._snap = UserStatsSnapshot(available=avail, portfolio=port, ts_epoch=time.time())
                        self._ready.set()

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("UserStats WS error: %s", e)
                await sleep_backoff(backoff, attempt)
                attempt += 1
