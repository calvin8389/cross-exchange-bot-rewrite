from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Optional

import aiohttp
import websockets

from src.exchanges.base import Balance, BestBidAsk, ExchangeAdapter, FundingRate, MarketDetails, PositionInfo

logger = logging.getLogger(__name__)


class LighterAdapter(ExchangeAdapter):
    """Minimal Lighter REST/WS adapter.

    Uses ephemeral WebSocket connections for balance / order-book snapshots
    (no SDK dependency).  In later milestones the persistent WS services in
    ``src/services/`` can be injected to avoid per-call connection overhead.
    """

    def __init__(self, ws_url: str, rest_url: str, account_index: int):
        self.ws_url = ws_url
        self.rest_url = rest_url
        self.account_index = account_index

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        pass  # ephemeral sessions, nothing to persist

    # ------------------------------------------------------------------
    # Balance (ephemeral WS)
    # ------------------------------------------------------------------

    async def get_balance(self) -> Balance:
        sub = {"type": "subscribe", "channel": f"user_stats/{self.account_index}"}
        timeout = 15.0
        try:
            async with websockets.connect(self.ws_url) as ws:
                await ws.send(json.dumps(sub))
                deadline = asyncio.get_running_loop().time() + timeout
                while asyncio.get_running_loop().time() < deadline:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    msg = json.loads(raw)
                    t = msg.get("type")
                    if t in ("update/user_stats", "subscribed/user_stats"):
                        stats = msg.get("stats") or {}
                        avail = float(stats.get("available_balance", 0) or 0)
                        port = float(stats.get("portfolio_value", 0) or 0)
                        return Balance(total_equity=port, available=avail)
        except asyncio.TimeoutError:
            pass
        raise RuntimeError("Lighter balance: timeout waiting for user_stats")

    # ------------------------------------------------------------------
    # Best bid/ask (ephemeral WS order-book snapshot)
    # ------------------------------------------------------------------

    async def get_best_bid_ask(self, market_id: int) -> BestBidAsk:
        sub = {"type": "subscribe", "channel": f"order_book/{market_id}"}
        timeout = 15.0
        try:
            async with websockets.connect(self.ws_url) as ws:
                await ws.send(json.dumps(sub))
                deadline = asyncio.get_running_loop().time() + timeout
                while asyncio.get_running_loop().time() < deadline:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    msg = json.loads(raw)
                    t = msg.get("type")
                    if t in ("update/order_book", "subscribed/order_book"):
                        ob = msg.get("order_book") or {}
                        bids = ob.get("bids", [])
                        asks = ob.get("asks", [])
                        if bids and asks:
                            return BestBidAsk(
                                bid=float(bids[0]["price"]),
                                ask=float(asks[0]["price"]),
                            )
        except asyncio.TimeoutError:
            pass
        raise RuntimeError(f"Lighter order book: timeout for market {market_id}")

    # ------------------------------------------------------------------
    # Funding rate (REST)
    # ------------------------------------------------------------------

    async def get_funding_rate(self, market_id: int) -> Optional[FundingRate]:
        try:
            url = f"{self.rest_url}/api/v1/funding-rates"
            async with aiohttp.ClientSession() as session, session.get(url) as resp:
                if resp.status != 200:
                    logger.warning("Lighter funding rates HTTP %s", resp.status)
                    return None
                data = await resp.json()
                rates = data.get("funding_rates") or []
                for r in rates:
                    if int(r.get("market_id", -1)) == market_id:
                        rate = float(r.get("rate", 0))
                        return FundingRate(rate=rate, apr=rate * 365 * 24)
                return None
        except Exception as e:
            logger.warning("Lighter funding rate fetch failed: %s", e)
            return None

    # ------------------------------------------------------------------
    # Positions (REST)
    # ------------------------------------------------------------------

    async def get_open_positions(self) -> list[PositionInfo]:
        try:
            url = f"{self.rest_url}/api/v1/account?by=index&value={self.account_index}"
            async with aiohttp.ClientSession() as session, session.get(url) as resp:
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
    # Market details (REST or ephemeral WS)
    # ------------------------------------------------------------------

    async def get_market_details(self, symbol: str) -> MarketDetails:
        """Return Lighter market_id, price_tick, size_step for a symbol."""
        # Use REST order-books list to resolve symbol → market metadata
        url = f"{self.rest_url}/api/v1/orderBooks"
        async with aiohttp.ClientSession() as session, session.get(url) as resp:
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
