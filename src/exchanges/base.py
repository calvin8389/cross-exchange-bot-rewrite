from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class Balance:
    total_equity: float
    available: float


@dataclass
class BestBidAsk:
    bid: float
    ask: float


@dataclass
class FundingRate:
    rate: float  # e.g. 0.01 = 0.01% per funding interval
    apr: float   # annualised, e.g. 8.76 = 8.76%


@dataclass
class PositionInfo:
    symbol: str
    size: float  # signed: +long, -short
    entry_price: float
    unrealized_pnl: float


@dataclass
class MarketDetails:
    market_id: int | str   # int for Lighter, str for EdgeX
    price_tick: float
    size_step: float


class ExchangeAdapter(ABC):
    @abstractmethod
    async def get_balance(self) -> Balance: ...

    @abstractmethod
    async def get_best_bid_ask(self, market_id: int) -> BestBidAsk: ...

    @abstractmethod
    async def get_funding_rate(self, market_id: int) -> Optional[FundingRate]: ...

    @abstractmethod
    async def get_open_positions(self) -> list[PositionInfo]: ...

    @abstractmethod
    async def get_market_details(self, symbol: str) -> MarketDetails: ...

    @abstractmethod
    async def place_order(
        self, symbol: str, side: str, size_base: float,
        price: float, market_id: int | str | None = None,
    ) -> Optional[str]: ...

    @abstractmethod
    async def close_position(
        self, symbol: str, side: str, size_base: float,
        price: float, market_id: int | str | None = None,
    ) -> bool: ...

    @abstractmethod
    async def close(self) -> None: ...
