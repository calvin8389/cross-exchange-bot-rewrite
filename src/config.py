"""Typed configuration loading from environment and bot_config.json."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Env:
    edgex_base_url: str
    edgex_account_id: int
    edgex_stark_private_key: str
    lighter_base_url: str
    lighter_ws_url: str
    lighter_private_key: str
    account_index: int
    api_key_index: int


@dataclass
class BotConfig:
    symbols_to_monitor: list[str] = field(default_factory=list)
    quote: str = "USD"
    leverage: int = 3
    notional_per_position: float = 100.0
    hold_duration_hours: float = 8.0
    wait_between_cycles_minutes: float = 5.0
    check_interval_seconds: int = 60
    min_net_apr_threshold: float = 5.0
    min_volume_usd: float = 250_000_000
    max_spread_pct: float = 0.15
    cross_pct: float = 3.0
    enable_stop_loss: bool = True


def load_env() -> Env:
    """Load and validate environment variables.  Fail-fast on missing required keys."""
    required = [
        "EDGEX_BASE_URL",
        "EDGEX_ACCOUNT_ID",
        "EDGEX_STARK_PRIVATE_KEY",
        "LIGHTER_BASE_URL",
        "LIGHTER_WS_URL",
        "LIGHTER_PRIVATE_KEY",
    ]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")

    return Env(
        edgex_base_url=os.environ["EDGEX_BASE_URL"],
        edgex_account_id=int(os.environ["EDGEX_ACCOUNT_ID"]),
        edgex_stark_private_key=os.environ["EDGEX_STARK_PRIVATE_KEY"],
        lighter_base_url=os.environ["LIGHTER_BASE_URL"],
        lighter_ws_url=os.environ["LIGHTER_WS_URL"],
        lighter_private_key=os.environ["LIGHTER_PRIVATE_KEY"],
        account_index=int(os.environ.get("LIGHTER_ACCOUNT_INDEX", os.environ.get("ACCOUNT_INDEX", "0"))),
        api_key_index=int(os.environ.get("LIGHTER_API_KEY_INDEX", os.environ.get("API_KEY_INDEX", "0"))),
    )


def load_bot_config(path: str = "bot_config.json") -> BotConfig:
    """Load trading parameters from JSON file. Uses defaults for missing keys."""
    if not Path(path).exists():
        return BotConfig()
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return BotConfig(
        symbols_to_monitor=raw.get("symbols_to_monitor", []),
        quote=raw.get("quote", "USD"),
        leverage=raw.get("leverage", 3),
        notional_per_position=raw.get("notional_per_position", 100.0),
        hold_duration_hours=raw.get("hold_duration_hours", 8.0),
        wait_between_cycles_minutes=raw.get("wait_between_cycles_minutes", 5.0),
        check_interval_seconds=raw.get("check_interval_seconds", 60),
        min_net_apr_threshold=raw.get("min_net_apr_threshold", 5.0),
        min_volume_usd=raw.get("min_volume_usd", 250_000_000),
        max_spread_pct=raw.get("max_spread_pct", 0.15),
        cross_pct=raw.get("cross_pct", 3.0),
        enable_stop_loss=raw.get("enable_stop_loss", True),
    )
