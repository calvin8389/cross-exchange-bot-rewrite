from __future__ import annotations

import logging
from typing import Optional

import aiohttp

from src.exchanges.base import Balance, BestBidAsk, ExchangeAdapter, FundingRate, PositionInfo

logger = logging.getLogger(__name__)


class EdgeXAdapter(ExchangeAdapter):
    """Minimal EdgeX REST adapter via aiohttp.

    In later milestones this will be backed by ``edgex-python-sdk``.
    Current implementation uses EdgeX REST endpoints directly.
    """

    def __init__(self, base_url: str, account_id: int, private_key: str):
        self.base_url = base_url.rstrip("/")
        self.account_id = account_id       # MUST be int (SDK requirement)
        self.private_key = private_key
        self._session: Optional[aiohttp.ClientSession] = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Balance
    # ------------------------------------------------------------------

    async def get_balance(self) -> Balance:
        session = await self._ensure_session()
        url = f"{self.base_url}/api/v1/private/account/asset"
        try:
            async with session.get(
                url,
                params={"accountId": self.account_id},
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"EdgeX balance HTTP {resp.status}")
                data = await resp.json()
                assets = data.get("collateralAssetModelList", [])
                total = 0.0
                avail = 0.0
                for a in assets:
                    total += float(a.get("totalEquity", 0) or 0)
                    avail += float(a.get("availableAmount", 0) or 0)
                return Balance(total_equity=total, available=avail)
        except Exception as e:
            logger.warning("EdgeX balance fetch failed: %s", e)
            raise

    # ------------------------------------------------------------------
    # Best bid/ask
    # ------------------------------------------------------------------

    async def get_best_bid_ask(self, market_id: int) -> BestBidAsk:
        """Fetch best bid/ask from EdgeX order book via REST.

        Note: ``market_id`` is the EdgeX **contract ID string** (e.g. "BTCUSD"),
        not a numeric ID.
        """
        session = await self._ensure_session()
        contract_id = str(market_id)  # EdgeX contract_id must be str
        url = f"{self.base_url}/api/v1/public/orderbook/{contract_id}"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"EdgeX order book HTTP {resp.status}")
                data = await resp.json()
                bids = data.get("bids", [])
                asks = data.get("asks", [])
                if not bids or not asks:
                    # fallback: use last price to synthesise bid/ask
                    last = float(data.get("lastPrice", 0) or 0)
                    if last > 0:
                        return BestBidAsk(
                            bid=last * 0.9995,
                            ask=last * 1.0005,
                        )
                    raise RuntimeError("EdgeX order book empty, no last price")
                return BestBidAsk(
                    bid=float(bids[0][0]),
                    ask=float(asks[0][0]),
                )
        except Exception as e:
            logger.warning("EdgeX order book fetch failed: %s", e)
            raise

    # ------------------------------------------------------------------
    # Funding rate
    # ------------------------------------------------------------------

    async def get_funding_rate(self, market_id: int) -> Optional[FundingRate]:
        """Fetch EdgeX funding rate from quote API.

        ``market_id`` is the contract name string (e.g. "BTCUSD").
        """
        session = await self._ensure_session()
        contract = str(market_id)
        url = f"{self.base_url}/api/v1/public/quote/{contract}"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning("EdgeX quote HTTP %s", resp.status)
                    return None
                data = await resp.json()
                rate = float(data.get("fundingRate", 0) or 0)
                return FundingRate(rate=rate, apr=rate * 365 * 24)
        except Exception as e:
            logger.warning("EdgeX funding rate fetch failed: %s", e)
            return None

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    async def get_open_positions(self) -> list[PositionInfo]:
        session = await self._ensure_session()
        url = f"{self.base_url}/api/v1/private/account/positions"
        try:
            async with session.get(
                url,
                params={"accountId": self.account_id},
            ) as resp:
                if resp.status != 200:
                    logger.warning("EdgeX positions HTTP %s", resp.status)
                    return []
                data = await resp.json()
                positions = []
                for p in data.get("positions", []):
                    size = float(p.get("size", 0) or 0)
                    if abs(size) > 1e-8:
                        positions.append(PositionInfo(
                            symbol=p.get("contractId", "UNKNOWN"),
                            size=size,
                            entry_price=float(p.get("entryPrice", 0) or 0),
                            unrealized_pnl=float(p.get("unrealizedPnl", 0) or 0),
                        ))
                return positions
        except Exception as e:
            logger.warning("EdgeX positions fetch failed: %s", e)
            return []
