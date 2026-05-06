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
class FundingPayment:
    """A single funding payment recorded by the exchange."""
    ts: str           # ISO-8601 timestamp
    amount: float     # signed USD amount (+ received, - paid)
    rate: float       # funding rate for this interval
    position_size: float = 0.0


@dataclass
class MarketDetails:
    market_id: int | str   # int for Lighter, str for EdgeX
    price_tick: float
    size_step: float


@dataclass
class OrderResult:
    """Result returned by place_order / close_position.

    ``order_id`` is the exchange-assigned identifier (may be None if the
    exchange did not return one).  ``fill_price`` and ``fee`` are populated
    when the exchange returns them immediately; otherwise they remain None
    and should be filled in after position confirmation.
    """
    order_id: Optional[str]
    fill_price: Optional[float] = None
    fee: Optional[float] = None


class ExchangeAdapter(ABC):
    @property
    @abstractmethod
    def exchange_id(self) -> str: ...

    @abstractmethod
    async def get_balance(self) -> Balance: ...

    @abstractmethod
    async def get_best_bid_ask(self, market_id: int | str) -> BestBidAsk: ...

    @abstractmethod
    async def get_funding_rate(self, market_id: int | str) -> Optional[FundingRate]: ...

    @abstractmethod
    async def get_open_positions(self) -> list[PositionInfo]: ...

    @abstractmethod
    async def get_market_details(self, symbol: str) -> MarketDetails: ...

    @abstractmethod
    async def place_order(
        self, symbol: str, side: str, size_base: float,
        price: float, market_id: int | str | None = None,
    ) -> Optional[OrderResult]: ...

    @abstractmethod
    async def close_position(
        self, symbol: str, side: str, size_base: float,
        price: float, market_id: int | str | None = None,
    ) -> Optional[OrderResult]: ...

    @abstractmethod
    async def close(self) -> None: ...

    async def get_funding_history(
        self, symbol: str, market_id: int | str | None = None,
        since_ts: str | None = None, until_ts: str | None = None,
    ) -> list[FundingPayment]:
        """Return actual funding payments for a position since a given time.

        Default returns empty — override per exchange if funding history
        is available.
        """
        return []
