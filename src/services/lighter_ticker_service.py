import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import websockets

from src.util.retry import Backoff, sleep_backoff

logger = logging.getLogger(__name__)


class StaleDataError(RuntimeError):
    pass


def _parse_mid(channel: str) -> Optional[int]:
    if not isinstance(channel, str) or not channel.startswith("ticker:"):
        return None
    try:
        return int(channel.split(":", 1)[1])
    except Exception:
        return None


@dataclass
class TickerTop:
    bid: Optional[float] = None
    ask: Optional[float] = None
    ts_epoch: float = 0.0


class LighterTickerService:
    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._ws = None
        self._lock = asyncio.Lock()
        self._subscribed: set[int] = set()
        self._tops: Dict[int, TickerTop] = {}

    async def start(self) -> None:
        if self._task:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="lighter-ticker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def subscribe(self, market_id: int) -> None:
        mid = int(market_id)
        async with self._lock:
            self._subscribed.add(mid)
            self._tops.setdefault(mid, TickerTop())
        if self._ws:
            await self._ws.send(json.dumps({"type": "subscribe", "channel": f"ticker/{mid}"}))

    async def get_best_bid_ask(self, market_id: int, max_age_seconds: float = 3.0) -> Tuple[float, float]:
        mid = int(market_id)
        now = time.time()
        async with self._lock:
            top = self._tops.get(mid)
            if not top or top.bid is None or top.ask is None:
                raise StaleDataError("ticker not ready")
            age = now - top.ts_epoch
            if age > max_age_seconds:
                raise StaleDataError(f"ticker stale age={age:.2f}s")
            return float(top.bid), float(top.ask)

    async def _run(self) -> None:
        backoff = Backoff()
        attempt = 0
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.ws_url, ping_interval=None) as ws:
                    self._ws = ws
                    attempt = 0
                    async with self._lock:
                        mids = sorted(self._subscribed)
                    for mid in mids:
                        await ws.send(json.dumps({"type": "subscribe", "channel": f"ticker/{mid}"}))

                    while not self._stop.is_set():
                        raw = await ws.recv()
                        msg = json.loads(raw)
                        t = msg.get("type")
                        if t == "ping":
                            await ws.send(json.dumps({"type": "pong"}))
                            continue
                        if t != "update/ticker":
                            continue
                        mid = _parse_mid(msg.get("channel", ""))
                        if mid is None:
                            continue
                        tick = msg.get("ticker") or {}
                        a = tick.get("a") or {}
                        b = tick.get("b") or {}
                        try:
                            ask = float(a.get("price"))
                            bid = float(b.get("price"))
                        except Exception:
                            continue
                        async with self._lock:
                            top = self._tops.setdefault(mid, TickerTop())
                            top.ask = ask
                            top.bid = bid
                            top.ts_epoch = time.time()

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Ticker WS error: %s", e)
                self._ws = None
                await sleep_backoff(backoff, attempt)
                attempt += 1
