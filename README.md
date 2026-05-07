# cross-exchange-bot-rewrite

Python + asyncio cross-exchange funding-rate arbitrage bot. Scans funding rates across 4 exchanges, opens delta-neutral positions on the best pairs, and monitors spreads in real time.

## Supported Exchanges

| Exchange | Adapter | Auth | Funding Interval |
|----------|---------|------|-----------------|
| Lighter | `lighter_adapter.py` | REST + Lighter SDK | 8h (Binance-sourced) |
| EdgeX | `edgex_adapter.py` | REST + edgex-python-sdk | 4h |
| Hyperliquid | `hyperliquid_adapter.py` | REST via official SDK | 1h |
| GRVT | `grvt_adapter.py` | REST via grvt-pysdk | 4h / 8h (per-market) |

Add an exchange: implement `ExchangeAdapter` ABC (8 methods), add to `_build_adapters()` in `main.py` — zero changes to core logic.

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your API keys
cp bot_config.json.example bot_config.json   # adjust symbols/tiers
python -m src.main
```

## Configuration

### .env

```bash
# Required per exchange — see .env.example for all variables
ACTIVE_EXCHANGES=edgex,lighter,hyperliquid,grvt
```

Only exchanges listed in `ACTIVE_EXCHANGES` are loaded. Missing env vars for an active exchange → fail-fast on startup.

### bot_config.json

```json
{
  "symbols_to_monitor": ["BTC", "ETH", "SOL", ...],
  "min_net_apr_threshold": 10.0,
  "max_spread_pct": 0.20,
  "max_concurrent_positions": 5,
  "leverage": 3,
  "estimated_taker_fee_bps": 4.0,
  "estimated_slippage_bps": 2.0,
  "estimated_impact_bps": 1.0,
  "max_symbol_exposure_usd": 1000.0,
  "max_exchange_exposure_usd": 2000.0,
  "max_total_exposure_usd": 4000.0,
  "max_total_drawdown_usd": 300.0,

  "position_tiers": {
    "large": 500.0,
    "medium": 200.0,
    "small": 100.0
  },
  "symbol_tiers": {
    "BTC": "large",
    "ETH": "large",
    "PENDLE": "small",
    ...
  }
}
```

New runtime risk controls:

- scanner deducts configured fee/slippage/impact costs before comparing net APR to the open threshold
- opening prechecks reject orders that miss exchange min size/min notional requirements
- opening enforces symbol / exchange / total exposure caps plus portfolio drawdown guardrails
- startup performs read-only health checks for balances, open positions, market metadata, and `close_all.sh`
- recovery now escalates unhedged restart states into `ERROR` instead of silently resuming

## Runbooks

- `docs/资金费率套利实盘风控清单.md`
- `docs/异常场景处置手册.md`
- `docs/此工程机 P0_P1_P2 开发排期版计划.md`

## How It Works

### State Machine

```
IDLE → ANALYZING → OPENING → HOLDING → CLOSING → WAITING → IDLE
```

- **IDLE**: verify all exchanges are flat
- **ANALYZING**: scan all symbols × all exchange pairs, rank by net APR
- **OPENING**: open top N positions concurrently (one per symbol), rollback all on any failure
- **HOLDING**: every 60s re-check funding spreads — close positions whose net APR drops below threshold, scan for replacements to fill empty slots
- **CLOSING**: emergency/manual close all
- **WAITING**: cool-down → back to IDLE

### Position Sizing

Each symbol is assigned a tier (large/medium/small) mapping to a dollar notional. The execution engine caps position size at `tier_notional / mid_price` while respecting available balance and leverage limits.

### Close Condition

No fixed hold duration. A position is closed when the **current net APR** of its exchange pair drops below `min_net_apr_threshold` (10%). This naturally acts as both take-profit and stop-loss — the arb either persists or it doesn't.

## Architecture

```
src/
  core/
    orchestrator.py   — state machine, multi-position management
    scanner.py        — scan_all(adapters, config) → ranked opportunities
    execution.py      — open_position / close_position with rollback
    sizing.py         — Decimal-precision tick/step math
    models.py         — ExchangeLeg, Opportunity, PositionState
  exchanges/
    base.py           — ExchangeAdapter ABC (8 methods)
    lighter_adapter.py
    edgex_adapter.py
    hyperliquid_adapter.py
    grvt_adapter.py
  db/
    schema.sql        — bot_kv, events, cycles, positions, position_legs
    store.py          — aiosqlite async wrapper
  config.py           — typed Env + BotConfig loading
  main.py             — entry point, adapter wiring
  logging_.py         — logging setup
  services/           — Lighter WebSocket (M1 demo fallback)
  util/               — retry backoff, UTC time
```

## Tests

```bash
# Exchange smoke tests
python tests/test_lighter.py --public
python tests/test_edgex.py --public
python tests/test_hyperliquid.py --public
python tests/test_grvt.py --public --env prod

# Cross-exchange scanner (standalone)
python tests/test_scanner.py --min-apr 10 --max-spread 0.3

# Orchestrator dry run (adapter-based)
python tests/test_orchestrator.py
```

## Database

SQLite (`bot.sqlite3`, gitignored). Tables:

- `bot_kv` — key-value state store
- `events` — structured event log
- `cycles` — one row per open→close round
- `positions` — active positions (multiple allowed)
- `position_legs` — per-exchange leg details (size, entry, PnL)
