#!/usr/bin/env python3
"""Phase 7: Single-leg micro trades. Open → confirm → close → verify flat.

Usage:
  python tests/test_micro_trade.py              # all exchanges
  python tests/test_micro_trade.py --exchange lighter --symbol DOGE --notional 5
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv

load_dotenv()


def _ok(label, val):
    print(f"  \033[32m✓\033[0m {label}: {val}")


def _fail(label, reason):
    print(f"  \033[31m✗\033[0m {label}: {reason}")


def _header(title):
    print(f"\n{'='*60}\n  {title}\n{'='*60}")


# ---------------------------------------------------------------------------
# Lighter
# ---------------------------------------------------------------------------

async def test_lighter(symbol: str, notional: float):
    from src.exchanges.lighter_adapter import LighterAdapter

    a = LighterAdapter(
        ws_url=os.environ.get("LIGHTER_WS_URL", ""),
        rest_url=os.environ.get("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai"),
        account_index=int(os.environ.get("LIGHTER_ACCOUNT_INDEX", "0")),
    )
    try:
        md = await a.get_market_details(symbol)
        bba = await a.get_best_bid_ask(md.market_id)
        mid = (bba.bid + bba.ask) / 2
        size = round(notional / mid / md.size_step) * md.size_step
        actual = size * mid
        _ok("market", f"{symbol} tick={md.price_tick} step={md.size_step} mid={mid:.4f} size={size:.4f} (~${actual:.2f})")

        # 1. Place buy order
        oid = await a.place_order(symbol, "buy", size, bba.ask, md.market_id)
        if not oid:
            _fail("open", "order returned None")
            return
        _ok("open", f"BUY {size:.4f} @ {bba.ask:.4f} order_id={oid.order_id}")

        # 2. Confirm position
        await asyncio.sleep(3)
        for _tick in range(5):
            positions = await a.get_open_positions()
            match = [p for p in positions if p.symbol.upper() == symbol.upper() and abs(p.size) > 1e-8]
            if match:
                p = match[0]
                _ok("confirm", f"{p.symbol} {('LONG' if p.size>0 else 'SHORT')} size={p.size:.4f} entry={p.entry_price:.4f}")
                break
            await asyncio.sleep(2)
        else:
            _fail("confirm", "position not found after 5 attempts")
            return

        # 3. Close position
        close_side = "sell" if match[0].size > 0 else "buy"
        close_size = abs(match[0].size)
        bba2 = await a.get_best_bid_ask(md.market_id)
        ok = await a.close_position(symbol, close_side, close_size, bba2.bid, md.market_id)
        if ok:
            _ok("close", f"{close_side.upper()} {close_size:.4f} @ {bba2.bid:.4f}")
        else:
            _fail("close", "failed")

        # 4. Verify flat
        await asyncio.sleep(3)
        positions2 = await a.get_open_positions()
        still_open = [p for p in positions2 if p.symbol.upper() == symbol.upper() and abs(p.size) > 1e-8]
        if still_open:
            _fail("verify flat", f"still have {still_open[0].size:.4f}")
        else:
            _ok("verify flat", "position closed")
    finally:
        await a.close()


# ---------------------------------------------------------------------------
# Hyperliquid
# ---------------------------------------------------------------------------

async def test_hl(symbol: str, notional: float):
    from src.exchanges.hyperliquid_adapter import HyperliquidAdapter

    a = HyperliquidAdapter(
        base_url=os.environ.get("HYPERLIQUID_BASE_URL", "https://api.hyperliquid.xyz"),
        private_key_hex=os.environ.get("HYPERLIQUID_PRIVATE_KEY", ""),
        account_address=os.environ.get("HYPERLIQUID_ACCOUNT_ADDRESS", ""),
    )
    try:
        md = await a.get_market_details(symbol)
        bba = await a.get_best_bid_ask(md.market_id)
        mid = (bba.bid + bba.ask) / 2
        import math
        precision = max(0, int(round(-math.log10(md.price_tick))))
        size = round(notional / mid / md.size_step) * md.size_step
        actual = size * mid
        _ok("market", f"{symbol} tick={md.price_tick} step={md.size_step} mid={mid:.4f} size={size:.6f} (~${actual:.2f})")

        # 1. Place buy
        buy_px = round(bba.ask, precision)
        oid = await a.place_order(symbol, "buy", size, buy_px, md.market_id)
        if not oid:
            _fail("open", "order returned None")
            return
        _ok("open", f"BUY {size:.6f} @ {buy_px} order_id={oid}")

        # 2. Confirm
        await asyncio.sleep(5)
        for _tick in range(5):
            positions = await a.get_open_positions()
            match = [p for p in positions if p.symbol.upper() == symbol.upper() and abs(p.size) > 1e-8]
            if match:
                p = match[0]
                _ok("confirm", f"{p.symbol} {('LONG' if p.size>0 else 'SHORT')} size={p.size:.6f} entry={p.entry_price:.4f}")
                break
            await asyncio.sleep(2)
        else:
            _fail("confirm", "position not found after 5 attempts")
            return

        # 3. Close
        close_side = "sell" if match[0].size > 0 else "buy"
        close_size = abs(match[0].size)
        bba2 = await a.get_best_bid_ask(md.market_id)
        sell_px = round(bba2.bid, precision)
        ok = await a.close_position(symbol, close_side, close_size, sell_px, md.market_id)
        _ok("close" if ok else "close", f"{close_side.upper()} {close_size:.6f} @ {sell_px} -- {'OK' if ok else 'FAIL'}")

        # 4. Verify flat
        await asyncio.sleep(3)
        positions2 = await a.get_open_positions()
        still_open = [p for p in positions2 if p.symbol.upper() == symbol.upper() and abs(p.size) > 1e-8]
        if still_open:
            # Try GTC close if IOC failed
            _fail("verify flat", f"still open ({still_open[0].size:.6f}), trying GTC...")
            bba3 = await a.get_best_bid_ask(md.market_id)
            oid2 = await a.place_order(symbol, close_side, abs(still_open[0].size), bba3.bid, md.market_id)
            _ok("gtc close", f"order_id={oid2}")
            await asyncio.sleep(3)
            positions3 = await a.get_open_positions()
            still_open2 = [p for p in positions3 if p.symbol.upper() == symbol.upper() and abs(p.size) > 1e-8]
            if still_open2:
                _fail("verify flat (2)", f"STILL open ({still_open2[0].size:.6f})")
            else:
                _ok("verify flat", "closed via GTC")
        else:
            _ok("verify flat", "position closed")
    finally:
        await a.close()


# ---------------------------------------------------------------------------
# EdgeX
# ---------------------------------------------------------------------------

async def test_edgex(symbol: str, notional: float):
    from src.exchanges.edgex_adapter import EdgeXAdapter
    from src.core.sizing import round_size_to_step

    a = EdgeXAdapter(
        base_url=os.environ.get("EDGEX_BASE_URL", ""),
        account_id=int(os.environ.get("EDGEX_ACCOUNT_ID", "0")),
        private_key=os.environ.get("EDGEX_STARK_PRIVATE_KEY", ""),
    )
    try:
        md = await a.get_market_details(symbol)
        bba = await a.get_best_bid_ask(md.market_id)
        mid = (bba.bid + bba.ask) / 2
        # EdgeX has min_order_size in fixture
        import json
        with open(Path(__file__).resolve().parent / "fixtures" / "edgex_contracts.json") as f:
            edgex_fixture = json.load(f)["contracts"]
        f_info = edgex_fixture.get(symbol) or edgex_fixture.get(f"{symbol}USD") or {}
        min_sz = f_info.get("min_order_size", md.size_step)
        size = round_size_to_step(notional / mid, md.size_step)
        while size < min_sz:
            size += md.size_step
        actual = size * mid
        _ok("market", f"{symbol} tick={md.price_tick} step={md.size_step} min_sz={min_sz} mid={mid:.4f} size={size:.4f} (~${actual:.2f})")

        # 1. Place buy
        oid = await a.place_order(symbol, "buy", size, bba.ask, md.market_id)
        if not oid:
            _fail("open", "order returned None")
            return
        _ok("open", f"BUY {size:.4f} @ {bba.ask:.4f} order_id={oid.order_id}")

        # 2. Confirm
        await asyncio.sleep(5)
        for _tick in range(6):
            positions = await a.get_open_positions()
            match = [p for p in positions if symbol.upper() in str(p.symbol).upper() and abs(p.size) > 1e-8]
            if match:
                p = match[0]
                _ok("confirm", f"{p.symbol} {('LONG' if p.size>0 else 'SHORT')} size={p.size:.4f} entry={p.entry_price:.4f}")
                break
            await asyncio.sleep(2)
        else:
            _fail("confirm", "position not found after 6 attempts")
            return

        # 3. Close
        close_side = "sell" if match[0].size > 0 else "buy"
        close_size = abs(match[0].size)
        bba2 = await a.get_best_bid_ask(md.market_id)
        ok = await a.close_position(symbol, close_side, close_size, bba2.bid, md.market_id)
        if ok:
            _ok("close", f"{close_side.upper()} {close_size:.4f} @ {bba2.bid:.4f}")
        else:
            _fail("close", "failed")

        # 4. Verify flat
        await asyncio.sleep(3)
        positions2 = await a.get_open_positions()
        still_open = [p for p in positions2 if symbol.upper() in str(p.symbol).upper() and abs(p.size) > 1e-8]
        if still_open:
            _fail("verify flat", f"still have {still_open[0].size:.4f}")
        else:
            _ok("verify flat", "position closed")
    finally:
        await a.close()


# ---------------------------------------------------------------------------
# GRVT
# ---------------------------------------------------------------------------

async def test_grvt(symbol: str, notional: float):
    from src.exchanges.grvt_adapter import GrvtAdapter

    a = GrvtAdapter(
        trading_account_id=os.environ.get("GRVT_TRADING_ACCOUNT_ID", ""),
        private_key=os.environ.get("GRVT_PRIVATE_KEY", ""),
        api_key=os.environ.get("GRVT_API_KEY", ""),
        env=os.environ.get("GRVT_ENV", "prod"),
    )
    try:
        md = await a.get_market_details(symbol)
        bba = await a.get_best_bid_ask(md.market_id)
        mid = (bba.bid + bba.ask) / 2
        size = round(notional / mid / md.size_step) * md.size_step
        while size * mid < 5.0:  # GRVT min_notional ~$5
            size += md.size_step
        actual = size * mid
        _ok("market", f"{symbol} tick={md.price_tick} step={md.size_step} mid={mid:.4f} size={size:.4f} (~${actual:.2f})")

        # 1. Place buy
        oid = await a.place_order(symbol, "buy", size, bba.ask, md.market_id)
        if not oid:
            _fail("open", "order returned None")
            return
        _ok("open", f"BUY {size:.4f} @ {bba.ask:.4f} order_id={oid.order_id}")

        # 2. Confirm
        await asyncio.sleep(3)
        for _tick in range(5):
            positions = await a.get_open_positions()
            match = [p for p in positions if p.symbol.upper() == symbol.upper() and abs(p.size) > 1e-8]
            if match:
                p = match[0]
                _ok("confirm", f"{p.symbol} {('LONG' if p.size>0 else 'SHORT')} size={p.size:.4f} entry={p.entry_price:.4f}")
                break
            await asyncio.sleep(2)
        else:
            _fail("confirm", "position not found after 5 attempts")
            return

        # 3. Close
        close_side = "sell" if match[0].size > 0 else "buy"
        close_size = abs(match[0].size)
        bba2 = await a.get_best_bid_ask(md.market_id)
        ok = await a.close_position(symbol, close_side, close_size, bba2.bid, md.market_id)
        _ok("close" if ok else "close", f"{close_side.upper()} {close_size:.4f} @ {bba2.bid:.4f} -- {'OK' if ok else 'FAIL'} order_id={ok.order_id if ok else 'N/A'}")

        # 4. Verify flat
        await asyncio.sleep(3)
        positions2 = await a.get_open_positions()
        still_open = [p for p in positions2 if p.symbol.upper() == symbol.upper() and abs(p.size) > 1e-8]
        if still_open:
            _fail("verify flat", f"still have {still_open[0].size:.4f}")
        else:
            _ok("verify flat", "position closed")
    finally:
        await a.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _main():
    parser = argparse.ArgumentParser(description="Phase 7: Single-leg micro trades")
    parser.add_argument("--exchange", choices=["lighter", "hl", "grvt", "edgex", "all"], default="all")
    parser.add_argument("--symbol", default="DOGE")
    parser.add_argument("--notional", type=float, default=5.0)
    args = parser.parse_args()

    symbol = args.symbol.upper()
    notional = args.notional

    if args.exchange in ("all", "lighter"):
        _header(f"Lighter Micro Trade — {symbol} ${notional}")
        await test_lighter(symbol, notional)

    if args.exchange in ("all", "hl"):
        _header(f"Hyperliquid Micro Trade — {symbol} ${notional}")
        await test_hl(symbol, notional)

    if args.exchange in ("all", "grvt"):
        _header(f"GRVT Micro Trade — {symbol} ${notional}")
        await test_grvt(symbol, notional)

    if args.exchange in ("all", "edgex"):
        _header(f"EdgeX Micro Trade — {symbol} ${notional}")
        await test_edgex(symbol, notional)


if __name__ == "__main__":
    asyncio.run(_main())
