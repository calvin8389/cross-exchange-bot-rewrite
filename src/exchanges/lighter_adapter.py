from __future__ import annotations

import logging
import os
import time
from typing import Optional

import aiohttp

from src.exchanges.base import Balance, BestBidAsk, ExchangeAdapter, FundingRate, MarketDetails, PositionInfo

logger = logging.getLogger(__name__)


class LighterAdapter(ExchangeAdapter):
    """Lighter REST adapter backed by aiohttp.

    All public + account endpoints use REST.  Order placement uses the
    Lighter SDK SignerClient (imported lazily to keep the adapter usable
    without the SDK).
    """

    def __init__(self, ws_url: str, rest_url: str, account_index: int):
        self.ws_url = ws_url   # kept for compatibility; no longer used internally
        self.rest_url = rest_url.rstrip("/")
        self.account_index = account_index
        self._session: Optional[aiohttp.ClientSession] = None

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

    # ------------------------------------------------------------------
    # Balance (REST)
    # ------------------------------------------------------------------

    async def get_balance(self) -> Balance:
        session = await self._ensure_session()
        url = f"{self.rest_url}/api/v1/account"
        params = {"by": "index", "value": str(self.account_index)}
        try:
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
        session = await self._ensure_session()
        url = f"{self.rest_url}/api/v1/funding-rates"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning("Lighter funding rates HTTP %s", resp.status)
                    return None
                data = await resp.json()
                rates = data.get("funding_rates") or []
                for r in rates:
                    if int(r.get("market_id", -1)) == market_id:
                        rate = float(r.get("rate", 0))
                        return FundingRate(rate=rate, apr=rate * 365 * 24 * 100)
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
        session = await self._ensure_session()
        url = f"{self.rest_url}/api/v1/orderBooks"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Lighter orderBooks HTTP {resp.status}")
                data = await resp.json()
                for ob in data.get("order_books", []):
                    if ob.get("symbol", "").upper() == symbol.upper():
                        market_id = ob["market_id"]
                        price_tick = 10 ** -ob.get("supported_price_decimals", 2)
                        size_step = 10 ** -ob.get("supported_size_decimals", 2)
                        return MarketDetails(market_id=market_id, price_tick=price_tick, size_step=size_step)
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
    ) -> Optional[str]:
        import lighter

        base_url = self.rest_url
        private_key = _lighter_private_key()
        api_key_index = 0  # default
        mid = int(market_id) if market_id else 0

        md = await self.get_market_details(symbol)
        base_scaled = int(round(size_base / md.size_step))
        price_scaled = int(price / md.price_tick)
        client_order_id = int(time.time() * 1_000_000) % 1_000_000

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
            )
            if err:
                logger.error("Lighter order error: %s", err)
                return None
            logger.info("Lighter order placed: %s %s %s @ %s", symbol, side, size_base, price)
            return str(client_order_id)
        finally:
            await signer.close()

    async def close_position(
        self, symbol: str, side: str, size_base: float,
        price: float, market_id: int | str | None = None,
    ) -> bool:
        import lighter

        base_url = self.rest_url
        private_key = _lighter_private_key()
        api_key_index = 0
        mid = int(market_id) if market_id else 0

        md = await self.get_market_details(symbol)
        base_scaled = int(round(size_base / md.size_step))
        price_scaled = int(price / md.price_tick)
        client_order_id = int(time.time() * 1_000_000) % 1_000_000

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
            )
            if err:
                logger.error("Lighter close error: %s", err)
                return False
            logger.info("Lighter close order placed: %s %s %s @ %s", symbol, side, size_base, price)
            return True
        finally:
            await signer.close()


def _lighter_private_key() -> str:
    return os.environ.get("LIGHTER_PRIVATE_KEY") or os.environ.get("API_KEY_PRIVATE_KEY") or ""
