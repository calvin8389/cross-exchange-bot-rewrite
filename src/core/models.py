from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Optional


class BotState(StrEnum):
    IDLE = "IDLE"
    ANALYZING = "ANALYZING"
    OPENING = "OPENING"
    HOLDING = "HOLDING"
    CLOSING = "CLOSING"
    WAITING = "WAITING"
    ERROR = "ERROR"
    SHUTDOWN = "SHUTDOWN"


@dataclass
class ExchangeLeg:
    """One side of a delta-neutral trade on a specific exchange."""
    exchange_id: str           # "edgex", "lighter", "hyperliquid"
    side: str = ""             # "long" or "short"
    rate: float = 0.0          # raw funding rate
    apr: float = 0.0           # annualised funding APR
    bid: float = 0.0
    ask: float = 0.0


@dataclass
class Opportunity:
    """A cross-exchange funding-rate arbitrage opportunity.

    ``long_leg`` is the exchange where funding is higher (get paid).
    ``short_leg`` is the exchange where funding is lower (pay less).
    """
    symbol: str
    long_leg: ExchangeLeg = field(default_factory=ExchangeLeg)
    short_leg: ExchangeLeg = field(default_factory=ExchangeLeg)
    net_apr: float = 0.0
    spread_pct: float = 0.0
    volume: float = 0.0
    gross_apr: float = 0.0
    estimated_cost_apr: float = 0.0

    @property
    def direction(self) -> str:
        return f"long_{self.long_leg.exchange_id}_short_{self.short_leg.exchange_id}"


@dataclass
class PositionState:
    """Track the state of an open delta-neutral position."""
    symbol: str
    cycle_id: int
    legs: dict[str, dict] = field(default_factory=dict)
    # legs[exchange_id] = {"side": "buy"|"sell", "size": float, "entry": float}
    opened_at: str = ""


@dataclass
class CycleRecord:
    cycle_id: int
    symbol: str
    state: BotState = BotState.IDLE
    exchange_long: str = ""
    exchange_short: str = ""
    opened_at: Optional[str] = None
    closed_at: Optional[str] = None
    long_pnl: float = 0.0
    short_pnl: float = 0.0
    net_pnl: float = 0.0
