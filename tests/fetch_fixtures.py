#!/usr/bin/env python3
"""Phase 0: Dump exchange metadata to JSON fixtures.

Usage:
  python tests/fetch_fixtures.py                 # all exchanges
  python tests/fetch_fixtures.py --exchange lighter  # single exchange

Output: tests/fixtures/{exchange}_markets.json

Each fixture contains the raw API response for market metadata, plus
a derived "contracts" dict keyed by symbol with computed precision fields.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


# ---------------------------------------------------------------------------
# Lighter
# ---------------------------------------------------------------------------

async def fetch_lighter() -> dict:
    import aiohttp

    base_url = os.environ.get("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai")
    url = f"{base_url}/api/v1/orderBooks"

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Lighter orderBooks HTTP {resp.status}")
            raw = await resp.json()

    contracts = {}
    for ob in raw.get("order_books", []):
        symbol = ob.get("symbol", "").upper()
        if not symbol:
            continue
        price_decimals = ob.get("supported_price_decimals", 2)
        size_decimals = ob.get("supported_size_decimals", 2)
        contracts[symbol] = {
            "market_id": ob["market_id"],
            "symbol": symbol,
            "price_tick": 10 ** -price_decimals,
            "price_precision": price_decimals,
            "size_step": 10 ** -size_decimals,
            "size_precision": size_decimals,
            "min_base_amount": float(ob.get("min_base_amount", 0) or 0),
            "min_quote_amount": float(ob.get("min_quote_amount", 0) or 0),
            "order_quote_limit": float(ob.get("order_quote_limit", 0) or 0),
            "max_leverage": None,  # not in Lighter metadata
            "contract_type": ob.get("market_type", "perp"),
            "base_currency": symbol,
            "quote_currency": "USD",
            "taker_fee": float(ob.get("taker_fee", 0) or 0),
            "maker_fee": float(ob.get("maker_fee", 0) or 0),
            "status": ob.get("status", "active"),
        }

    return {
        "exchange": "lighter",
        "source": "/api/v1/orderBooks",
        "symbol_count": len(contracts),
        "contracts": contracts,
    }


# ---------------------------------------------------------------------------
# EdgeX
# ---------------------------------------------------------------------------

async def fetch_edgex() -> dict:
    import aiohttp

    base_url = os.environ.get("EDGEX_BASE_URL", "https://pro.edgex.exchange")
    url = f"{base_url}/api/v1/public/meta/getMetaData"

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"EdgeX metadata HTTP {resp.status}")
            raw = await resp.json()

    contracts = {}
    for c in raw.get("data", {}).get("contractList", []):
        cid = c.get("contractId", "")
        name = c.get("contractName", "")
        if not name:
            continue
        tick_size = float(c.get("tickSize", 0.01) or 0.01)
        step_size = float(c.get("stepSize", 0.001) or 0.001)
        entry = {
            "market_id": cid,
            "symbol": name.upper(),
            "price_tick": tick_size,
            "price_precision": _decimal_places(tick_size),
            "size_step": step_size,
            "size_precision": _decimal_places(step_size),
            "min_order_size": float(c.get("minOrderSize", step_size) or step_size),
            "min_quote_amount": None,
            "max_leverage": int(c.get("defaultLeverage", 0) or 0) or None,
            "contract_type": "perp",
            "base_currency": name.upper(),
            "quote_currency": "USD",
            "taker_fee": float(c.get("defaultTakerFeeRate", 0) or 0),
            "maker_fee": float(c.get("defaultMakerFeeRate", 0) or 0),
            "status": "active" if c.get("enableTrade") else "inactive",
        }
        contracts[name.upper()] = entry
        # Also register base symbol (BTCUSD → BTC) for adapter compatibility
        if name.upper().endswith("USD") and len(name) > 3:
            base = name[:-3].upper()
            if base not in contracts:
                contracts[base] = entry

    return {
        "exchange": "edgex",
        "source": "/api/v1/public/meta/getMetaData",
        "symbol_count": len(contracts),
        "contracts": contracts,
    }


# ---------------------------------------------------------------------------
# Hyperliquid
# ---------------------------------------------------------------------------

async def fetch_hyperliquid() -> dict:
    import asyncio as _asyncio

    def _sync():
        from hyperliquid.info import Info

        base_url = os.environ.get("HYPERLIQUID_BASE_URL", "https://api.hyperliquid.xyz")
        info = Info(base_url, skip_ws=True)
        meta = info.meta()
        asset_ctxs, = info.meta_and_asset_ctxs()[1:] if False else (None,)  # not used directly
        # Get meta + asset contexts for full data
        full_meta, full_ctxs = info.meta_and_asset_ctxs()
        return meta, full_ctxs

    meta, asset_ctxs = await asyncio.to_thread(_sync)

    contracts = {}
    universe = meta.get("universe", [])
    for i, entry in enumerate(universe):
        name = entry.get("name", "").upper()
        if not name:
            continue
        sz_decimals = entry.get("szDecimals", 2)
        # Derive price tick from order book (most reliable)
        price_tick = _hl_tick_from_l2(name)

        ctx = asset_ctxs[i] if i < len(asset_ctxs) else {}
        contracts[name] = {
            "market_id": name,
            "symbol": name,
            "price_tick": price_tick,
            "price_precision": _decimal_places(price_tick),
            "size_step": float(10 ** -sz_decimals),
            "size_precision": sz_decimals,
            "min_order_size": float(10 ** -sz_decimals),  # one step
            "min_quote_amount": None,  # not in HL metadata
            "max_leverage": entry.get("maxLeverage"),
            "contract_type": "perp",
            "base_currency": name,
            "quote_currency": "USD",
            "taker_fee": None,
            "maker_fee": None,
            "status": "active",
            "_sz_decimals": sz_decimals,
            "_mark_px": float(ctx.get("markPx", 0)) if ctx else None,
        }

    return {
        "exchange": "hyperliquid",
        "source": "POST /info metaAndAssetCtxs + L2 snapshot",
        "symbol_count": len(contracts),
        "contracts": contracts,
    }


def _hl_tick_from_l2(coin: str) -> float:
    """Derive HL price tick from L2 order book gaps."""
    try:
        from hyperliquid.info import Info

        base_url = os.environ.get("HYPERLIQUID_BASE_URL", "https://api.hyperliquid.xyz")
        info = Info(base_url, skip_ws=True)
        snap = info.l2_snapshot(coin)
        levels = snap.get("levels", [])
        if levels and len(levels) >= 2:
            bids = levels[0][:10]
            px_values = [float(b["px"]) for b in bids]
            min_diff = None
            for j in range(1, len(px_values)):
                diff = abs(px_values[j] - px_values[j - 1])
                if diff > 0 and (min_diff is None or diff < min_diff):
                    min_diff = diff
            if min_diff is not None:
                # Round to significant digits to eliminate float artifacts
                rounded = float(f"{min_diff:.10g}")
                return max(rounded, 1e-8)  # clamp to reasonable minimum
    except Exception:
        pass
    return 0.01  # fallback


# ---------------------------------------------------------------------------
# GRVT
# ---------------------------------------------------------------------------

async def fetch_grvt() -> dict:
    def _sync():
        from pysdk.grvt_ccxt import GrvtCcxt
        from pysdk.grvt_ccxt_env import GrvtEnv

        env_str = os.environ.get("GRVT_ENV", os.environ.get("GRVT_ENVIRONMENT", "prod"))
        env_map = {"prod": GrvtEnv.PROD, "testnet": GrvtEnv.TESTNET,
                   "staging": GrvtEnv.STAGING, "dev": GrvtEnv.DEV}
        grvt_env = env_map.get(env_str.lower(), GrvtEnv.PROD)

        # Public-only client (no auth needed for fetch_markets)
        client = GrvtCcxt(env=grvt_env)
        markets = client.fetch_markets()
        return markets

    markets = await asyncio.to_thread(_sync)

    contracts = {}
    for m in markets:
        instrument = m.get("instrument", "")
        base = m.get("base", "").upper()
        if not instrument or not base:
            continue
        tick = float(m.get("tick_size", 0.01) or 0.01)
        min_sz = float(m.get("min_size", 0.001) or 0.001)
        contracts[base] = {
            "market_id": instrument,
            "symbol": base,
            "price_tick": tick,
            "price_precision": _decimal_places(tick),
            "size_step": min_sz,  # min_size often == step on GRVT
            "size_precision": _decimal_places(min_sz),
            "min_order_size": min_sz,
            "min_quote_amount": float(m.get("min_notional", 100) or 100),
            "max_leverage": None,  # via separate endpoint
            "contract_type": "perp" if "PERP" in str(m.get("kind", "")).upper() else "future",
            "base_currency": base,
            "quote_currency": m.get("quote", "USDT"),
            "taker_fee": None,
            "maker_fee": None,
            "status": "active",
            "_kind": str(m.get("kind", "")),
            "_funding_interval_hours": int(m.get("funding_interval_hours", 8) or 8),
        }

    return {
        "exchange": "grvt",
        "source": "GrvtCcxt.fetch_markets()",
        "symbol_count": len(contracts),
        "contracts": contracts,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decimal_places(value: float) -> int:
    """Count decimal places in a float value."""
    s = f"{value:.10f}".rstrip("0")
    if "." in s:
        return len(s.split(".")[1])
    return 0


def _save_fixture(data: dict, filename: str) -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURES_DIR / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  ✓ {filename} — {data['symbol_count']} symbols")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _main() -> None:
    parser = argparse.ArgumentParser(description="Fetch exchange metadata fixtures")
    parser.add_argument("--exchange", choices=["lighter", "edgex", "hyperliquid", "grvt"],
                        help="Fetch a single exchange (default: all)")
    args = parser.parse_args()

    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Writing fixtures to {FIXTURES_DIR}/\n")

    fetchers = {
        "lighter": (fetch_lighter, "lighter_markets.json"),
        "edgex": (fetch_edgex, "edgex_contracts.json"),
        "hyperliquid": (fetch_hyperliquid, "hl_markets.json"),
        "grvt": (fetch_grvt, "grvt_markets.json"),
    }

    targets = {args.exchange: fetchers[args.exchange]} if args.exchange else fetchers

    for name, (fetcher, filename) in targets.items():
        print(f"[{name}] Fetching...")
        try:
            data = await fetcher()
            _save_fixture(data, filename)
        except Exception as e:
            print(f"  ✗ {name} FAILED: {e}")

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(_main())
