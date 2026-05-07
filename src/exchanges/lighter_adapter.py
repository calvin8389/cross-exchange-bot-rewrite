from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

import aiohttp

from src.exchanges.base import Balance, BestBidAsk, ExchangeAdapter, FundingRate, MarketDetails, OrderResult, PositionInfo

logger = logging.getLogger(__name__)
_CLIENT_ORDER_ID_MAX = 2_147_483_647
_PID_MIX_SHIFT_BITS = 16


class LighterAdapter(ExchangeAdapter):
    """Lighter REST adapter backed by aiohttp.

    All public + account endpoints use REST.  Order placement uses the
    Lighter SDK SignerClient (imported lazily to keep the adapter usable
    without the SDK).
    """

    @property
    def exchange_id(self) -> str:
        return "lighter"

    def __init__(self, ws_url: str, rest_url: str, account_index: int):
        self.ws_url = ws_url   # kept for compatibility; no longer used internally
        self.rest_url = rest_url.rstrip("/")
        self.account_index = account_index
        self._session: Optional[aiohttp.ClientSession] = None
        from src.util.retry import RateLimiter
        self._rate_limiter = RateLimiter(max_per_minute=35)  # Lighter: 40/min, keep margin
        self._funding_cache: Optional[tuple[float, dict[int, tuple[float, float]]]] = None
        self._market_cache: Optional[tuple[float, dict[str, MarketDetails]]] = None
        self._order_id_lock = asyncio.Lock()
        # Mix coarse process identity into the monotonic seed so restarts in
        # nearby timestamps are less likely to reuse the same initial sequence.
        seed = time.time_ns() ^ (os.getpid() << _PID_MIX_SHIFT_BITS)
        self._client_order_seq = int(seed % _CLIENT_ORDER_ID_MAX) or 1

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None
        self._funding_cache = None
        self._market_cache = None

    async def next_client_order_id(self) -> int:
        """Generate a process-local, monotonic client order ID."""
        async with self._order_id_lock:
            self._client_order_seq += 1
            if self._client_order_seq > _CLIENT_ORDER_ID_MAX:
                self._client_order_seq = 1
            return self._client_order_seq

    # ------------------------------------------------------------------
    # Balance (REST)
    # ------------------------------------------------------------------

    async def get_balance(self) -> Balance:
        session = await self._ensure_session()
        url = f"{self.rest_url}/api/v1/account"
        params = {"by": "index", "value": str(self.account_index)}
        try:
            await self._rate_limiter.acquire()
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Lighter account HTTP {resp.status}")
                data = await resp.json()
                accounts = data.get("accounts", [])
                if not accounts:
                    raise RuntimeError("Lighter balance: no account returned")
                acc = accounts[0]
                avail = float(acc.get("available_balance", 0))
                total = float(acc.get("total_asset_value", acc.get("collateral", 0)))
                return Balance(total_equity=total, available=avail)
        except Exception as e:
            logger.warning("Lighter balance fetch failed: %s", e)
            raise

    # ------------------------------------------------------------------
    # Best bid/ask (REST order book snapshot)
    # ------------------------------------------------------------------

    async def get_best_bid_ask(self, market_id: int) -> BestBidAsk:
        session = await self._ensure_session()
        url = f"{self.rest_url}/api/v1/orderBookOrders"
        params = {"market_id": str(market_id), "limit": "1"}
        try:
            await self._rate_limiter.acquire()
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Lighter order book HTTP {resp.status}")
                data = await resp.json()
                bids = data.get("bids", [])
                asks = data.get("asks", [])
                if not bids or not asks:
                    raise RuntimeError(f"Lighter order book empty for market {market_id}")
                return BestBidAsk(
                    bid=float(bids[0]["price"]),
                    ask=float(asks[0]["price"]),
                )
        except Exception as e:
            logger.warning("Lighter order book fetch failed: %s", e)
            raise

    # ------------------------------------------------------------------
    # Funding rate (REST)
    # ------------------------------------------------------------------

    async def get_funding_rate(self, market_id: int) -> Optional[FundingRate]:
        import time as _time

        # Return from cache if fresh (< 30s old)
        if self._funding_cache and _time.monotonic() - self._funding_cache[0] < 30:
            cached = self._funding_cache[1].get(int(market_id))
            if cached:
                return FundingRate(rate=cached[0], apr=cached[1])

        session = await self._ensure_session()
        url = f"{self.rest_url}/api/v1/funding-rates"
        try:
            await self._rate_limiter.acquire()
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning("Lighter funding rates HTTP %s", resp.status)
                    return None
                data = await resp.json()
                rates = data.get("funding_rates") or []
                cache: dict[int, tuple[float, float]] = {}
                for r in rates:
                    mid = int(r.get("market_id", -1))
                    rate = float(r.get("rate", 0))
                    # Lighter funding rates are sourced from Binance (8-hour interval)
                    # → 3 periods per day
                    cache[mid] = (rate, rate * 365 * 3 * 100)

                self._funding_cache = (_time.monotonic(), cache)

                if int(market_id) in cache:
                    r, apr = cache[int(market_id)]
                    return FundingRate(rate=r, apr=apr)
                return None
        except Exception as e:
            logger.warning("Lighter funding rate fetch failed: %s", e)
            return None

    # ------------------------------------------------------------------
    # Positions (REST)
    # ------------------------------------------------------------------

    async def get_open_positions(self) -> list[PositionInfo]:
        session = await self._ensure_session()
        url = f"{self.rest_url}/api/v1/account"
        params = {"by": "index", "value": str(self.account_index)}
        try:
            await self._rate_limiter.acquire()
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.warning("Lighter account HTTP %s", resp.status)
                    return []
                data = await resp.json()
                accounts = data.get("accounts", [])
                if not accounts:
                    return []
                positions = []
                for pos in accounts[0].get("positions", []):
                    raw_size = float(pos.get("position", 0) or 0)
                    sign = int(pos.get("sign", 0))
                    signed_size = raw_size * sign
                    if abs(signed_size) > 1e-8:
                        positions.append(PositionInfo(
                            symbol=pos.get("symbol", "UNKNOWN"),
                            size=signed_size,
                            entry_price=float(pos.get("avg_entry_price", 0) or 0),
                            unrealized_pnl=float(pos.get("unrealized_pnl", 0) or 0),
                        ))
                return positions
        except Exception as e:
            logger.warning("Lighter positions fetch failed: %s", e)
            return []

    # ------------------------------------------------------------------
    # Market details (REST)
    # ------------------------------------------------------------------

    async def get_market_details(self, symbol: str) -> MarketDetails:
        """Return Lighter market_id, price_tick, size_step for a symbol."""
        import time as _time

        # Return from cache if fresh (< 300s)
        if self._market_cache and _time.monotonic() - self._market_cache[0] < 300:
            cached = self._market_cache[1].get(symbol.upper())
            if cached:
                return cached

        session = await self._ensure_session()
        url = f"{self.rest_url}/api/v1/orderBooks"
        try:
            await self._rate_limiter.acquire()
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Lighter orderBooks HTTP {resp.status}")
                data = await resp.json()
                cache: dict[str, MarketDetails] = {}
                for ob in data.get("order_books", []):
                    sym = ob.get("symbol", "").upper()
                    market_id = ob["market_id"]
                    price_tick = 10 ** -ob.get("supported_price_decimals", 2)
                    size_step = 10 ** -ob.get("supported_size_decimals", 2)
                    cache[sym] = MarketDetails(market_id=market_id, price_tick=price_tick, size_step=size_step)

                self._market_cache = (_time.monotonic(), cache)

                if symbol.upper() in cache:
                    return cache[symbol.upper()]
            raise ValueError(f"Symbol {symbol} not found on Lighter")
        except Exception as e:
            logger.warning("Lighter market details fetch failed: %s", e)
            raise

    # ------------------------------------------------------------------
    # Order placement (Lighter SDK)
    # ------------------------------------------------------------------

    async def place_order(
        self, symbol: str, side: str, size_base: float,
        price: float, market_id: int | str | None = None,
    ) -> Optional[OrderResult]:
        import lighter

        base_url = self.rest_url
        private_key = _lighter_private_key()
        api_key_index = int(os.environ.get("LIGHTER_API_KEY_INDEX", os.environ.get("API_KEY_INDEX", "0")))
        mid = int(market_id) if market_id else 0

        md = await self.get_market_details(symbol)
        base_scaled = int(round(size_base / md.size_step))
        price_scaled = int(price / md.price_tick)
        client_order_id = await self.next_client_order_id()

        signer = lighter.SignerClient(
            url=base_url,
            account_index=self.account_index,
            api_private_keys={api_key_index: private_key},
        )
        try:
            _tx, tx_hash, err = await signer.create_order(
                market_index=mid,
                client_order_index=client_order_id,
                base_amount=base_scaled,
                price=price_scaled,
                is_ask=(side == "sell"),
                order_type=signer.ORDER_TYPE_LIMIT,
                time_in_force=signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                reduce_only=0,
                trigger_price=0,
                api_key_index=api_key_index,
            )
            if err:
                logger.error("Lighter order error: %s", err)
                return None
            logger.info("Lighter order placed: %s %s %s @ %s", symbol, side, size_base, price)
            return OrderResult(order_id=str(client_order_id))
        finally:
            await signer.close()

    # ------------------------------------------------------------------
    # Funding history (authenticated)
    # ------------------------------------------------------------------

    async def get_funding_history(
        self, symbol: str, market_id: int | str | None = None,
        since_ts: str | None = None, until_ts: str | None = None,
    ) -> list:
        from datetime import datetime, timezone
        from src.exchanges.base import FundingPayment

        mid = int(market_id) if market_id else 0
        if mid == 0:
            md = await self.get_market_details(symbol)
            mid = md.market_id

        # Build auth token
        import lighter
        private_key = _lighter_private_key()
        if not private_key:
            return []

        api_key_index = int(os.environ.get("LIGHTER_API_KEY_INDEX", os.environ.get("API_KEY_INDEX", "0")))
        signer = lighter.SignerClient(
            url=self.rest_url,
            account_index=self.account_index,
            api_private_keys={api_key_index: private_key},
        )
        try:
            auth_token, err = signer.create_auth_token_with_expiry(api_key_index=api_key_index)
            if err:
                logger.warning("Lighter auth failed for funding history: %s", err)
                return []
        finally:
            await signer.close()

        session = await self._ensure_session()
        params: dict = {
            "account_index": self.account_index,
            "market_id": mid,
            "limit": 100,
        }
        headers = {"authorization": auth_token}

        try:
            async with session.get(
                f"{self.rest_url}/api/v1/positionFunding",
                params=params, headers=headers,
            ) as resp:
                if resp.status != 200:
                    logger.warning("Lighter positionFunding HTTP %s", resp.status)
                    return []
                data = await resp.json()
        except Exception as e:
            logger.warning("Lighter positionFunding fetch failed: %s", e)
            return []

        payments = []
        entries = data if isinstance(data, list) else data.get("fundings", data.get("data", []))
        for entry in entries:
            ts_val = entry.get("time") or entry.get("created_at") or entry.get("timestamp", "")
            rate = float(entry.get("funding_rate", entry.get("rate", 0)) or 0)
            amount = float(entry.get("amount", entry.get("funding", 0)) or 0)
            payments.append(FundingPayment(
                ts=str(ts_val),
                amount=amount,
                rate=rate,
            ))
        return payments

    async def close_position(
        self, symbol: str, side: str, size_base: float,
        price: float, market_id: int | str | None = None,
    ) -> Optional[OrderResult]:
        import lighter

        base_url = self.rest_url
        private_key = _lighter_private_key()
        api_key_index = int(os.environ.get("LIGHTER_API_KEY_INDEX", os.environ.get("API_KEY_INDEX", "0")))
        mid = int(market_id) if market_id else 0

        md = await self.get_market_details(symbol)
        base_scaled = int(round(size_base / md.size_step))
        price_scaled = int(price / md.price_tick)
        client_order_id = await self.next_client_order_id()

        signer = lighter.SignerClient(
            url=base_url,
            account_index=self.account_index,
            api_private_keys={api_key_index: private_key},
        )
        try:
            _tx, tx_hash, err = await signer.create_order(
                market_index=mid,
                client_order_index=client_order_id,
                base_amount=base_scaled,
                price=price_scaled,
                is_ask=(side == "sell"),
                order_type=signer.ORDER_TYPE_LIMIT,
                time_in_force=signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                reduce_only=1,
                trigger_price=0,
                api_key_index=api_key_index,
            )
            if err:
                logger.error("Lighter close error: %s", err)
                return None
            logger.info("Lighter close order placed: %s %s %s @ %s", symbol, side, size_base, price)
            return OrderResult(order_id=str(client_order_id))
        finally:
            await signer.close()


def _lighter_private_key() -> str:
    return os.environ.get("LIGHTER_PRIVATE_KEY") or os.environ.get("API_KEY_PRIVATE_KEY") or ""
