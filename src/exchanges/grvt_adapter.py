from __future__ import annotations

import asyncio
import logging
from typing import Optional

from src.exchanges.base import Balance, BestBidAsk, ExchangeAdapter, FundingRate, MarketDetails, OrderResult, PositionInfo

logger = logging.getLogger(__name__)


class GrvtAdapter(ExchangeAdapter):
    """GRVT exchange adapter via the grvt-pysdk.

    Uses the synchronous ``GrvtCcxt`` REST client wrapped in
    ``asyncio.to_thread()`` to avoid blocking the event loop.

    GRVT's SDK returns raw market data (not CCXT-wrapped). Markets use
    ``instrument`` IDs like ``BTC_USDT_Perp``. Funding interval is 8 hours.

    Authentication requires three env vars:
      - GRVT_TRADING_ACCOUNT_ID
      - GRVT_PRIVATE_KEY
      - GRVT_API_KEY
    """

    @property
    def exchange_id(self) -> str:
        return "grvt"

    def __init__(
        self,
        trading_account_id: str,
        private_key: str,
        api_key: str,
        env: str = "prod",
    ):
        self._trading_account_id = trading_account_id
        self._private_key = private_key
        self._api_key = api_key
        self._env = env
        self._client = None          # pysdk.grvt_ccxt.GrvtCcxt (lazy)
        self._markets_cache = None   # cached raw market list

    # ------------------------------------------------------------------
    # Lazy initialisers (called inside to_thread, so synchronous)
    # ------------------------------------------------------------------

    def _get_client(self):
        if self._client is None:
            from pysdk.grvt_ccxt import GrvtCcxt
            from pysdk.grvt_ccxt_env import GrvtEnv

            env_map = {
                "prod": GrvtEnv.PROD,
                "testnet": GrvtEnv.TESTNET,
                "staging": GrvtEnv.STAGING,
                "dev": GrvtEnv.DEV,
            }
            grvt_env = env_map.get(self._env.lower(), GrvtEnv.PROD)

            self._client = GrvtCcxt(
                env=grvt_env,
                parameters={
                    "trading_account_id": self._trading_account_id,
                    "private_key": self._private_key,
                    "api_key": self._api_key,
                },
            )
        return self._client

    def _load_markets(self) -> list[dict]:
        """Load and cache raw GRVT market list."""
        if self._markets_cache is None:
            client = self._get_client()
            self._markets_cache = client.fetch_markets()
        return self._markets_cache

    def _find_market(self, symbol: str) -> dict:
        """Find a perpetual market by base symbol (e.g. 'BTC' -> {...instrument: 'BTC_USDT_Perp'})."""
        markets = self._load_markets()
        sym = symbol.upper()
        # First: exact base match on perpetuals
        for m in markets:
            if m.get("base", "").upper() == sym and m.get("kind") == "PERPETUAL":
                return m
        # Fallback: any base match
        for m in markets:
            if m.get("base", "").upper() == sym:
                return m
        raise ValueError(f"Symbol {symbol} not found on GRVT")

    def _market_by_instrument(self, instrument: str) -> dict:
        """Find a market by instrument ID (e.g. 'BTC_USDT_Perp')."""
        markets = self._load_markets()
        for m in markets:
            if m.get("instrument") == instrument:
                return m
        raise ValueError(f"Instrument {instrument} not found on GRVT")

    # ------------------------------------------------------------------
    # Balance
    # ------------------------------------------------------------------

    async def get_balance(self) -> Balance:
        def _sync():
            client = self._get_client()
            bal = client.fetch_balance()
            free = bal.get("free", {})
            total = bal.get("total", {})
            available = 0.0
            total_equity = 0.0
            for asset in free:
                available += float(free.get(asset, 0) or 0)
            for asset in total:
                total_equity += float(total.get(asset, 0) or 0)
            return Balance(total_equity=total_equity, available=available)
        return await asyncio.to_thread(_sync)

    # ------------------------------------------------------------------
    # Best bid/ask
    # ------------------------------------------------------------------

    async def get_best_bid_ask(self, market_id: int | str) -> BestBidAsk:
        """Get BBA from ticker (has best_bid_price / best_ask_price directly)."""
        instrument = str(market_id)

        def _sync():
            client = self._get_client()
            ticker = client.fetch_ticker(instrument)
            bid = float(ticker.get("best_bid_price", 0) or 0)
            ask = float(ticker.get("best_ask_price", 0) or 0)
            if bid <= 0 or ask <= 0:
                raise RuntimeError(f"GRVT ticker empty for {instrument}")
            return BestBidAsk(bid=bid, ask=ask)
        return await asyncio.to_thread(_sync)

    # ------------------------------------------------------------------
    # Funding rate (8-hour interval on GRVT, rate in ticker is percentage)
    # ------------------------------------------------------------------

    async def get_funding_rate(self, market_id: int | str) -> Optional[FundingRate]:
        instrument = str(market_id)

        def _sync():
            client = self._get_client()
            ticker = client.fetch_ticker(instrument)
            rate = float(ticker.get("funding_rate", 0) or 0)
            # GRVT funding_rate in ticker is expressed as percentage
            # (e.g. -0.0274 = -0.0274%). Interval varies per market.
            m = self._market_by_instrument(instrument)
            interval_hours = int(m.get("funding_interval_hours", 8) or 8)
            periods_per_day = 24 / interval_hours
            return FundingRate(rate=rate, apr=rate * periods_per_day * 365)
        return await asyncio.to_thread(_sync)

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    async def get_open_positions(self) -> list[PositionInfo]:
        def _sync():
            client = self._get_client()
            try:
                positions = client.fetch_positions()
            except Exception:
                return []
            result = []
            for p in positions:
                # GRVT raw format: {instrument, size, entry_price, unrealized_pnl, side, ...}
                instrument = p.get("instrument", "")
                size_str = p.get("size", "0")
                size = float(size_str) if size_str else 0.0
                if abs(size) <= 1e-8:
                    continue
                # side: 'long' or 'short' — signed size
                side = p.get("side", "")
                if side == "short":
                    size = -size
                # Extract base from instrument (e.g. 'BTC_USDT_Perp' -> 'BTC')
                base = instrument.split("_")[0] if "_" in instrument else instrument
                result.append(PositionInfo(
                    symbol=base,
                    size=size,
                    entry_price=float(p.get("entry_price", 0) or 0),
                    unrealized_pnl=float(p.get("unrealized_pnl", 0) or 0),
                ))
            return result
        return await asyncio.to_thread(_sync)

    # ------------------------------------------------------------------
    # Market details
    # ------------------------------------------------------------------

    async def get_market_details(self, symbol: str) -> MarketDetails:
        def _sync():
            m = self._find_market(symbol)
            instrument = m.get("instrument", "")
            tick_size = float(m.get("tick_size", 0.01) or 0.01)
            min_size = float(m.get("min_size", 0.001) or 0.001)
            return MarketDetails(
                market_id=instrument,
                price_tick=tick_size,
                size_step=min_size,
            )
        return await asyncio.to_thread(_sync)

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    async def place_order(
        self, symbol: str, side: str, size_base: float,
        price: float, market_id: int | str | None = None,
    ) -> Optional[OrderResult]:
        contract = str(market_id) if market_id else symbol.upper()

        def _sync():
            client = self._get_client()
            result = client.create_limit_order(
                symbol=contract,
                side=side,
                amount=size_base,
                price=price,
                params={
                    "post_only": True,
                    "order_duration_secs": 30 * 86400 - 1,
                },
            )
            if not result:
                logger.error("GRVT order returned None")
                return None
            oid = result.get("id") or result.get("order_id", "")
            logger.info("GRVT order placed: %s %s %s @ %s", contract, side, size_base, price)
            return OrderResult(order_id=str(oid) if oid else None)

        return await asyncio.to_thread(_sync)

    async def close_position(
        self, symbol: str, side: str, size_base: float,
        price: float, market_id: int | str | None = None,
    ) -> Optional[OrderResult]:
        contract = str(market_id) if market_id else symbol.upper()

        def _sync():
            client = self._get_client()
            result = client.create_order(
                symbol=contract,
                type="market",
                side=side,
                amount=size_base,
                params={"reduce_only": True},
            )
            if not result:
                logger.error("GRVT close order returned None")
                return None
            oid = result.get("id") or result.get("order_id", "")
            logger.info("GRVT close order placed: %s %s %s", contract, side, size_base)
            return OrderResult(order_id=str(oid) if oid else None)

        return await asyncio.to_thread(_sync)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Funding history
    # ------------------------------------------------------------------

    async def get_funding_history(
        self, symbol: str, market_id: int | str | None = None,
        since_ts: str | None = None, until_ts: str | None = None,
    ) -> list:
        from datetime import datetime, timezone
        from src.exchanges.base import FundingPayment

        instrument = str(market_id) if market_id else symbol.upper()

        def _sync():
            client = self._get_client()

            if until_ts is None:
                until_dt = datetime.now(timezone.utc)
            else:
                until_dt = datetime.fromisoformat(until_ts.replace("Z", "+00:00"))
            if since_ts is None:
                since_dt = datetime.fromtimestamp(until_dt.timestamp() - 86400, tz=timezone.utc)
            else:
                since_dt = datetime.fromisoformat(since_ts.replace("Z", "+00:00"))

            start_ms = int(since_dt.timestamp() * 1000)
            end_ms = int(until_dt.timestamp() * 1000)

            try:
                history = client.fetch_funding_rate_history(instrument, start_ms, end_ms)
            except Exception:
                return []

            payments = []
            for entry in history if isinstance(history, list) else []:
                ts_val = entry.get("time") or entry.get("timestamp") or entry.get("fundingTime", 0)
                ts_int = int(ts_val)
                dt = datetime.fromtimestamp(ts_int / 1000 if ts_int > 1e12 else ts_int, tz=timezone.utc)
                amount = float(entry.get("funding", 0) or 0)
                rate = float(entry.get("funding_rate", entry.get("fundingRate", 0)) or 0)
                payments.append(FundingPayment(
                    ts=dt.isoformat().replace("+00:00", "Z"),
                    amount=amount,
                    rate=rate,
                ))
            return payments

        return await asyncio.to_thread(_sync)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        self._client = None
        self._markets_cache = None
