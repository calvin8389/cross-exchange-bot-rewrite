"""Typed configuration loading from environment and bot_config.json."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Env:
    # EdgeX
    edgex_base_url: str = ""
    edgex_account_id: int = 0
    edgex_stark_private_key: str = ""
    # Lighter
    lighter_base_url: str = ""
    lighter_ws_url: str = ""
    lighter_private_key: str = ""
    account_index: int = 0
    api_key_index: int = 0
    # Hyperliquid
    hyperliquid_base_url: str = "https://api.hyperliquid.xyz"
    hyperliquid_private_key: str = ""
    hyperliquid_account_address: str = ""
    # GRVT
    grvt_trading_account_id: str = ""
    grvt_private_key: str = ""
    grvt_api_key: str = ""
    grvt_env: str = "testnet"
    # Exchange selection
    active_exchanges: list[str] = field(default_factory=lambda: ["edgex", "lighter"])


@dataclass
class BotConfig:
    symbols_to_monitor: list[str] = field(default_factory=list)
    quote: str = "USD"
    leverage: int = 3
    notional_per_position: float = 100.0
    wait_between_cycles_minutes: float = 5.0
    check_interval_seconds: int = 60
    min_net_apr_threshold: float = 5.0   # entry: scanner only opens when net_apr >= this
    exit_net_apr_threshold: float = 5.0  # exit: holding closes when net_apr drops below this
    min_volume_usd: float = 250_000_000
    max_spread_pct: float = 0.15
    cross_pct: float = 3.0
    enable_stop_loss: bool = True
    # Multi-position support
    max_concurrent_positions: int = 5
    position_tiers: dict[str, float] = field(default_factory=lambda: {"large": 500.0, "medium": 200.0, "small": 100.0})
    symbol_tiers: dict[str, str] = field(default_factory=dict)
    # Net APR cost model (per order leg, in bps)
    estimated_taker_fee_bps: float = 4.0
    estimated_slippage_bps: float = 2.0
    estimated_impact_bps: float = 1.0
    # Portfolio risk limits (0 = disabled)
    max_symbol_exposure_usd: float = 0.0
    max_exchange_exposure_usd: float = 0.0
    max_total_exposure_usd: float = 0.0
    max_total_drawdown_usd: float = 0.0
    # Runtime alert thresholds
    runtime_failure_alert_threshold: int = 3
    # Per-symbol exchange exclusions: {"LDO": ["edgex"]} skips LDO on EdgeX in scanner
    symbol_exchange_excludes: dict[str, list[str]] = field(default_factory=dict)


def load_env() -> Env:
    """Load and validate environment variables. Fail-fast on missing required keys
    for whichever exchanges are active."""
    active_raw = os.environ.get("ACTIVE_EXCHANGES", "edgex,lighter")
    active_exchanges = [e.strip() for e in active_raw.split(",") if e.strip()]

    required_by_exchange: dict[str, list[str]] = {
        "edgex": ["EDGEX_BASE_URL", "EDGEX_ACCOUNT_ID", "EDGEX_STARK_PRIVATE_KEY"],
        "lighter": ["LIGHTER_BASE_URL", "LIGHTER_WS_URL", "LIGHTER_PRIVATE_KEY"],
        "hyperliquid": ["HYPERLIQUID_PRIVATE_KEY", "HYPERLIQUID_ACCOUNT_ADDRESS"],
        "grvt": ["GRVT_TRADING_ACCOUNT_ID", "GRVT_PRIVATE_KEY", "GRVT_API_KEY"],
    }

    for ex_id in active_exchanges:
        keys = required_by_exchange.get(ex_id, [])
        missing = [k for k in keys if not os.environ.get(k)]
        if missing:
            raise RuntimeError(
                f"Exchange '{ex_id}' is active but missing required env vars: {', '.join(missing)}"
            )

    return Env(
        edgex_base_url=os.environ.get("EDGEX_BASE_URL", ""),
        edgex_account_id=int(os.environ.get("EDGEX_ACCOUNT_ID", "0")),
        edgex_stark_private_key=os.environ.get("EDGEX_STARK_PRIVATE_KEY", ""),
        lighter_base_url=os.environ.get("LIGHTER_BASE_URL", ""),
        lighter_ws_url=os.environ.get("LIGHTER_WS_URL", ""),
        lighter_private_key=os.environ.get("LIGHTER_PRIVATE_KEY", ""),
        account_index=int(os.environ.get("LIGHTER_ACCOUNT_INDEX", os.environ.get("ACCOUNT_INDEX", "0"))),
        api_key_index=int(os.environ.get("LIGHTER_API_KEY_INDEX", os.environ.get("API_KEY_INDEX", "0"))),
        hyperliquid_base_url=os.environ.get("HYPERLIQUID_BASE_URL", "https://api.hyperliquid.xyz"),
        hyperliquid_private_key=os.environ.get("HYPERLIQUID_PRIVATE_KEY", ""),
        hyperliquid_account_address=os.environ.get("HYPERLIQUID_ACCOUNT_ADDRESS", ""),
        grvt_trading_account_id=os.environ.get("GRVT_TRADING_ACCOUNT_ID", ""),
        grvt_private_key=os.environ.get("GRVT_PRIVATE_KEY", ""),
        grvt_api_key=os.environ.get("GRVT_API_KEY", ""),
        grvt_env=os.environ.get("GRVT_ENVIRONMENT", os.environ.get("GRVT_ENV", "prod")),
        active_exchanges=active_exchanges,
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
        wait_between_cycles_minutes=raw.get("wait_between_cycles_minutes", 5.0),
        check_interval_seconds=raw.get("check_interval_seconds", 60),
        min_net_apr_threshold=raw.get("min_net_apr_threshold", 5.0),
        exit_net_apr_threshold=raw.get("exit_net_apr_threshold", raw.get("min_net_apr_threshold", 5.0)),
        min_volume_usd=raw.get("min_volume_usd", 250_000_000),
        max_spread_pct=raw.get("max_spread_pct", 0.15),
        cross_pct=raw.get("cross_pct", 3.0),
        enable_stop_loss=raw.get("enable_stop_loss", True),
        max_concurrent_positions=raw.get("max_concurrent_positions", 5),
        position_tiers=raw.get("position_tiers", {"large": 500.0, "medium": 200.0, "small": 100.0}),
        symbol_tiers=raw.get("symbol_tiers", {}),
        estimated_taker_fee_bps=raw.get("estimated_taker_fee_bps", 4.0),
        estimated_slippage_bps=raw.get("estimated_slippage_bps", 2.0),
        estimated_impact_bps=raw.get("estimated_impact_bps", 1.0),
        max_symbol_exposure_usd=raw.get("max_symbol_exposure_usd", 0.0),
        max_exchange_exposure_usd=raw.get("max_exchange_exposure_usd", 0.0),
        max_total_exposure_usd=raw.get("max_total_exposure_usd", 0.0),
        max_total_drawdown_usd=raw.get("max_total_drawdown_usd", 0.0),
        runtime_failure_alert_threshold=raw.get("runtime_failure_alert_threshold", 3),
        symbol_exchange_excludes=raw.get("symbol_exchange_excludes", {}),
    )
