from __future__ import annotations

import asyncio
import logging
from typing import Optional

from src.exchanges.base import Balance, BestBidAsk, ExchangeAdapter, FundingRate, MarketDetails, PositionInfo

logger = logging.getLogger(__name__)


class HyperliquidAdapter(ExchangeAdapter):
    """Hyperliquid REST adapter via the official Python SDK.

    Public data uses ``hyperliquid.info.Info``.  Order placement uses
    ``hyperliquid.exchange.Exchange`` with an Ethereum ECDSA private key.
    All SDK calls are wrapped in ``asyncio.to_thread()`` because the SDK
    is synchronous internally.
    """

    @property
    def exchange_id(self) -> str:
        return "hyperliquid"

    def __init__(self, base_url: str, private_key_hex: str, account_address: str):
        self.base_url = base_url.rstrip("/")
        self._private_key_hex = private_key_hex
        self._account_address = account_address  # main trading account (holds funds)
        self._wallet_address: Optional[str] = None  # API wallet (signs only)
        self._info = None          # hyperliquid.info.Info (lazy)
        self._exchange = None      # hyperliquid.exchange.Exchange (lazy)
        self._meta_cache: Optional[tuple] = None  # (universe_list, asset_ctxs_list)

    # ------------------------------------------------------------------
    # Lazy initialisers (called from asyncio.to_thread, so synchronous)
    # ------------------------------------------------------------------

    def _get_info(self):
        if self._info is None:
            from hyperliquid.info import Info
            self._info = Info(self.base_url, skip_ws=True)
        return self._info

    def _get_exchange(self):
        if self._exchange is None:
            from eth_account import Account
            from hyperliquid.exchange import Exchange
            acct = Account.from_key(self._private_key_hex)
            self._wallet_address = acct.address
            self._exchange = Exchange(
                acct, self.base_url,
                account_address=self._account_address,
            )
        return self._exchange

    def _get_wallet_address(self) -> str:
        if self._wallet_address is None:
            self._get_exchange()
        assert self._wallet_address is not None
        return self._wallet_address

    # ------------------------------------------------------------------
    # Metadata helpers
    # ------------------------------------------------------------------

    def _load_universe(self) -> list[dict]:
        """Return the raw perp universe list (synchronous, called inside to_thread)."""
        info = self._get_info()
        meta = info.meta()
        return meta["universe"]

    def _find_asset_index(self, coin: str, universe: list[dict]) -> int:
        """Find the asset index for a coin name in the universe."""
        for i, entry in enumerate(universe):
            if entry["name"].upper() == coin.upper():
                return i
        raise ValueError(f"Coin {coin} not found in Hyperliquid universe")

    # ------------------------------------------------------------------
    # Balance
    # ------------------------------------------------------------------

    async def get_balance(self) -> Balance:
        def _sync():
            info = self._get_info()
            state = info.user_state(self._account_address)
            ms = state.get("marginSummary", {})
            total = float(ms.get("accountValue", 0) or 0)
            avail = float(state.get("withdrawable", 0) or 0)
            return Balance(total_equity=total, available=avail)
        return await asyncio.to_thread(_sync)

    # ------------------------------------------------------------------
    # Best bid/ask
    # ------------------------------------------------------------------

    async def get_best_bid_ask(self, market_id: int | str) -> BestBidAsk:
        coin = str(market_id)
        def _sync():
            info = self._get_info()
            snapshot = info.l2_snapshot(coin)
            levels = snapshot.get("levels", [])
            if not levels or len(levels) < 2:
                raise RuntimeError(f"Hyperliquid order book empty for {coin}")
            bids = levels[0]  # [[px, sz, n], ...]
            asks = levels[1]
            if not bids or not asks:
                raise RuntimeError(f"Hyperliquid order book empty for {coin}")
            return BestBidAsk(
                bid=float(bids[0]["px"]),
                ask=float(asks[0]["px"]),
            )
        return await asyncio.to_thread(_sync)

    # ------------------------------------------------------------------
    # Funding rate
    # ------------------------------------------------------------------

    async def get_funding_rate(self, market_id: int | str) -> Optional[FundingRate]:
        import time as _time
        coin = str(market_id)
        def _sync():
            info = self._get_info()
            # Cache meta+ctx to avoid repeated heavy calls (30s TTL)
            if self._meta_cache and _time.monotonic() - self._meta_cache[0] < 30:
                meta, asset_ctxs = self._meta_cache[1]
            else:
                meta, asset_ctxs = info.meta_and_asset_ctxs()
                self._meta_cache = (_time.monotonic(), (meta, asset_ctxs))
            universe = meta["universe"]
            for i, entry in enumerate(universe):
                if entry["name"].upper() == coin.upper() and i < len(asset_ctxs):
                    ctx = asset_ctxs[i]
                    rate = float(ctx.get("funding", 0) or 0)
                    return FundingRate(rate=rate, apr=rate * 365 * 24 * 100)
            return None
        return await asyncio.to_thread(_sync)

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    async def get_open_positions(self) -> list[PositionInfo]:
        def _sync():
            info = self._get_info()
            state = info.user_state(self._account_address)
            positions = []
            for pos in state.get("assetPositions", []):
                inner = pos.get("position", {})
                szi = float(inner.get("szi", 0) or 0)
                if abs(szi) <= 1e-8:
                    continue
                coin = inner.get("coin", "UNKNOWN")
                positions.append(PositionInfo(
                    symbol=coin,
                    size=szi,
                    entry_price=float(inner.get("entryPx", 0) or 0),
                    unrealized_pnl=float(inner.get("unrealizedPnl", 0) or 0),
                ))
            return positions
        return await asyncio.to_thread(_sync)

    # ------------------------------------------------------------------
    # Market details
    # ------------------------------------------------------------------

    async def get_market_details(self, symbol: str) -> MarketDetails:
        coin = symbol.upper()
        def _sync():
            info = self._get_info()
            meta, asset_ctxs = info.meta_and_asset_ctxs()
            universe = meta["universe"]
            for i, entry in enumerate(universe):
                if entry["name"].upper() == coin:
                    sz_decimals = entry.get("szDecimals", 2)
                    size_step = float(10 ** -sz_decimals)
                    # Derive price tick from impact price difference
                    price_tick = 0.01
                    if i < len(asset_ctxs):
                        ctx = asset_ctxs[i]
                        impact_pxs = ctx.get("impactPxs")
                        if impact_pxs and len(impact_pxs) == 2:
                            diff = abs(float(impact_pxs[1]) - float(impact_pxs[0]))
                            if diff > 0:
                                # Tick is the smallest price increment
                                decimals = 0
                                diff_str = f"{diff:.8f}".rstrip("0")
                                if "." in diff_str:
                                    decimals = len(diff_str.split(".")[1])
                                    price_tick = float(10 ** -decimals)
                                else:
                                    price_tick = 1.0
                    return MarketDetails(
                        market_id=coin,
                        price_tick=price_tick,
                        size_step=size_step,
                    )
            raise ValueError(f"Symbol {symbol} not found on Hyperliquid")
        return await asyncio.to_thread(_sync)

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    async def place_order(
        self, symbol: str, side: str, size_base: float,
        price: float, market_id: int | str | None = None,
    ) -> Optional[str]:
        from hyperliquid.utils.signing import OrderType

        coin = str(market_id) if market_id else symbol.upper()
        is_buy = side == "buy"

        def _sync():
            exchange = self._get_exchange()
            result = exchange.order(
                name=coin,
                is_buy=is_buy,
                sz=size_base,
                limit_px=price,
                order_type=OrderType(limit={"tif": "Gtc"}),
                reduce_only=False,
            )
            if isinstance(result, dict) and result.get("status") == "err":
                logger.error("Hyperliquid order error: %s", result)
                return None
            if not isinstance(result, dict):
                return str(result) if result else None
            # Top-level oid (rare but possible)
            for key in ("oid", "orderId"):
                if key in result:
                    return str(result[key])
            # Standard response: {"response": {"data": {"statuses": [{"resting"/"filled": {"oid": ...}}]}}}
            statuses = result.get("response", {}).get("data", {}).get("statuses", [])
            if statuses:
                for event in ("filled", "resting"):
                    oid = statuses[0].get(event, {}).get("oid")
                    if oid:
                        return str(oid)
            logger.warning("Hyperliquid order response missing oid: %s", result)
            return None

        return await asyncio.to_thread(_sync)

    async def close_position(
        self, symbol: str, side: str, size_base: float,
        price: float, market_id: int | str | None = None,
    ) -> bool:
        from hyperliquid.utils.signing import OrderType

        coin = str(market_id) if market_id else symbol.upper()
        is_buy = side == "buy"

        def _sync():
            exchange = self._get_exchange()
            result = exchange.order(
                name=coin,
                is_buy=is_buy,
                sz=size_base,
                limit_px=price,
                order_type=OrderType(limit={"tif": "Ioc"}),
                reduce_only=True,
            )
            if isinstance(result, dict) and result.get("status") == "err":
                logger.error("Hyperliquid close error: %s", result)
                return False
            return True

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
        from src.exchanges.base import FundingPayment

        coin = str(market_id) if market_id else symbol.upper()

        def _sync():
            from datetime import datetime, timezone
            info = self._get_info()

            # Default: last 24h
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
                history = info.funding_history(coin, start_ms, end_ms)
            except Exception:
                return []

            payments = []
            for entry in history if isinstance(history, list) else []:
                dt = datetime.fromtimestamp(int(entry.get("time", 0)) / 1000, tz=timezone.utc)
                payments.append(FundingPayment(
                    ts=dt.isoformat().replace("+00:00", "Z"),
                    amount=float(entry.get("funding", 0) or 0),
                    rate=float(entry.get("fundingRate", 0) or 0),
                ))
            return payments

        return await asyncio.to_thread(_sync)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        self._info = None
        self._exchange = None
        self._wallet_address = None
        self._meta_cache = None
