from __future__ import annotations

import logging
from typing import Optional

import aiohttp

from src.exchanges.base import Balance, BestBidAsk, ExchangeAdapter, FundingRate, MarketDetails, PositionInfo

logger = logging.getLogger(__name__)


class EdgeXAdapter(ExchangeAdapter):
    """EdgeX REST adapter via aiohttp.

    Public endpoints use direct REST calls.  Private endpoints (balance,
    positions, order placement) use the edgex-python-sdk when available.
    """

    def __init__(self, base_url: str, account_id: int, private_key: str):
        self.base_url = base_url.rstrip("/")
        self.account_id = account_id       # MUST be int (SDK requirement)
        self.private_key = private_key
        self._session: Optional[aiohttp.ClientSession] = None
        self._contracts_by_name: dict[str, str] = {}  # "BTC" → "10000001"

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def _load_metadata(self) -> dict[str, str]:
        """Lazy-load EdgeX contract metadata. Returns {name: contract_id}."""
        if self._contracts_by_name:
            return self._contracts_by_name

        session = await self._ensure_session()
        url = f"{self.base_url}/api/v1/public/meta/getMetaData"
        async with session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"EdgeX metadata HTTP {resp.status}")
            data = await resp.json()

        for c in data.get("data", {}).get("contractList", []):
            name = c.get("contractName", "")
            cid = c.get("contractId", "")
            if name and cid:
                # Store both full name ("BTCUSD") and base symbol ("BTC")
                self._contracts_by_name[name.upper()] = cid
                if name.endswith("USD") and len(name) > 3:
                    base = name[:-3].upper()
                    if base not in self._contracts_by_name:
                        self._contracts_by_name[base] = cid
        return self._contracts_by_name

    async def resolve_contract_id(self, symbol: str) -> str:
        """Resolve a symbol (e.g. 'BTC' or 'BTCUSD') to a numeric contract ID."""
        meta = await self._load_metadata()
        key = symbol.upper()
        if key in meta:
            return meta[key]
        # Try with USD suffix
        if not key.endswith("USD"):
            key_usd = f"{key}USD"
            if key_usd in meta:
                return meta[key_usd]
        raise ValueError(f"Symbol {symbol} not found in EdgeX metadata")

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
        """Fetch best bid/ask from EdgeX order book depth.

        Note: ``market_id`` is the EdgeX **contract ID string** (e.g. "10000001"),
        not a numeric ID.
        """
        session = await self._ensure_session()
        contract_id = str(market_id)
        url = f"{self.base_url}/api/v1/public/quote/getDepth"
        params = {"contractId": contract_id, "level": "15"}
        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"EdgeX order book HTTP {resp.status}")
                data = await resp.json()
                ob_list = data.get("data", [{}])
                if not ob_list:
                    raise RuntimeError("EdgeX order book empty, no last price")
                ob = ob_list[0]
                bids = ob.get("bids", [])
                asks = ob.get("asks", [])
                if not bids or not asks:
                    raise RuntimeError("EdgeX order book empty, no last price")
                return BestBidAsk(
                    bid=float(bids[0].get("price", 0)),
                    ask=float(asks[0].get("price", 0)),
                )
        except Exception as e:
            logger.warning("EdgeX order book fetch failed: %s", e)
            raise

    # ------------------------------------------------------------------
    # Funding rate
    # ------------------------------------------------------------------

    async def get_funding_rate(self, market_id: int) -> Optional[FundingRate]:
        """Fetch EdgeX funding rate from ticker API.

        ``market_id`` is the contract ID string (e.g. "10000001").
        """
        session = await self._ensure_session()
        contract = str(market_id)
        url = f"{self.base_url}/api/v1/public/quote/getTicker"
        params = {"contractId": contract}
        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.warning("EdgeX quote HTTP %s", resp.status)
                    return None
                data = await resp.json()
                ticker_list = data.get("data", [])
                if ticker_list:
                    ticker = ticker_list[0]
                else:
                    ticker = {}
                rate = float(ticker.get("fundingRate", 0) or 0)
                return FundingRate(rate=rate, apr=rate * 365 * 24 * 100)
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

    # ------------------------------------------------------------------
    # Market details
    # ------------------------------------------------------------------

    async def get_market_details(self, symbol: str) -> MarketDetails:
        """Resolve symbol to contract metadata via EdgeX metadata API."""
        meta = await self._load_metadata()
        contract_id = await self.resolve_contract_id(symbol)

        # Fetch the full contract info from the cached metadata
        session = await self._ensure_session()
        url = f"{self.base_url}/api/v1/public/meta/getMetaData"
        async with session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"EdgeX metadata HTTP {resp.status}")
            data = await resp.json()

        for c in data.get("data", {}).get("contractList", []):
            if c.get("contractId") == contract_id:
                price_tick = float(c.get("tickSize", 0.01) or 0.01)
                size_step = float(c.get("stepSize", 0.001) or 0.001)
                return MarketDetails(market_id=contract_id, price_tick=price_tick, size_step=size_step)

        # Fallback
        return MarketDetails(market_id=contract_id, price_tick=0.01, size_step=0.001)

    # ------------------------------------------------------------------
    # Order placement (requires edgex-python-sdk)
    # ------------------------------------------------------------------

    async def place_order(
        self, symbol: str, side: str, size_base: float,
        price: float, market_id: int | str | None = None,
    ) -> Optional[str]:
        try:
            from edgex_sdk import Client as EdgeXClient, CreateOrderParams, OrderSide, OrderType, TimeInForce

            client = EdgeXClient(
                base_url=self.base_url,
                account_id=self.account_id,
                stark_private_key=self.private_key,
            )
            contract_id = str(market_id) if market_id else f"{symbol.upper()}USD"
            side_enum = OrderSide.BUY if side == "buy" else OrderSide.SELL
            params = CreateOrderParams(
                contract_id=str(contract_id),
                side=side_enum,
                order_type=OrderType.LIMIT,
                price=price,
                size=size_base,
                time_in_force=TimeInForce.GOOD_TILL_TIME,
            )
            result = await client.create_order(params)
            await client.close()
            return result.get("orderId") if result else None
        except ImportError:
            logger.warning("edgex-python-sdk not installed — order placement unavailable")
            return None

    async def close_position(
        self, symbol: str, side: str, size_base: float,
        price: float, market_id: int | str | None = None,
    ) -> bool:
        try:
            from edgex_sdk import Client as EdgeXClient, CreateOrderParams, OrderSide, OrderType, TimeInForce

            client = EdgeXClient(
                base_url=self.base_url,
                account_id=self.account_id,
                stark_private_key=self.private_key,
            )
            contract_id = str(market_id) if market_id else f"{symbol.upper()}USD"
            side_enum = OrderSide.BUY if side == "buy" else OrderSide.SELL
            params = CreateOrderParams(
                contract_id=str(contract_id),
                side=side_enum,
                order_type=OrderType.LIMIT,
                price=price,
                size=size_base,
                time_in_force=TimeInForce.GOOD_TILL_TIME,
                reduce_only=True,
            )
            result = await client.create_order(params)
            await client.close()
            return result is not None
        except ImportError:
            logger.warning("edgex-python-sdk not installed — close unavailable")
            return False
