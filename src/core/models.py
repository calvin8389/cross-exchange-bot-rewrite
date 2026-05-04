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
class Opportunity:
    symbol: str
    edgex_rate: Optional[float] = None
    lighter_rate: Optional[float] = None
    edgex_apr: float = 0.0
    lighter_apr: float = 0.0
    net_apr: float = 0.0
    volume: float = 0.0
    spread: float = 0.0
    direction: str = ""          # "long_edgex_short_lighter" | "short_edgex_long_lighter"
    edgex_bid: float = 0.0
    edgex_ask: float = 0.0
    lighter_bid: float = 0.0
    lighter_ask: float = 0.0


@dataclass
class PositionState:
    symbol: str
    cycle_id: int
    edgex_size: float = 0.0
    lighter_size: float = 0.0
    edgex_entry: float = 0.0
    lighter_entry: float = 0.0
    opened_at: str = ""


@dataclass
class CycleRecord:
    cycle_id: int
    symbol: str
    state: BotState = BotState.IDLE
    opened_at: Optional[str] = None
    closed_at: Optional[str] = None
    edgex_pnl: float = 0.0
    lighter_pnl: float = 0.0
    net_pnl: float = 0.0
