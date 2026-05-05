#!/usr/bin/env python3
"""Orchestrator dry-run test — runs the scanner + sizing pipeline with real data,
prints what a full cycle would look like.  Optionally executes a Lighter-only
open→close cycle.

Usage:
  python tests/test_orchestrator.py                    # dry run (no orders)
  python tests/test_orchestrator.py --live             # place & close on Lighter
  python tests/test_orchestrator.py --symbol DOGE --notional 20  # small test
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from src.core.execution import ExecConfig, close_position, open_position
from src.core.scanner import ScanConfig, scan_all
from src.core.sizing import (
    calculate_position_size,
    cross_price,
    round_size_to_step,
    unify_size_step,
)
from src.db.store import Store
from src.exchanges.edgex_adapter import EdgeXAdapter
from src.exchanges.lighter_adapter import LighterAdapter


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _header(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def _ok(label: str, value: object) -> None:
    print(f"  \033[32m✓\033[0m {label}: {value}")


def _fail(label: str, reason: str) -> None:
    print(f"  \033[31m✗\033[0m {label}: {reason}")


# ---------------------------------------------------------------------------
# dry run — scanner + sizing
# ---------------------------------------------------------------------------

async def test_dry_run(symbol: str | None = None) -> None:
    """Run the scanner and show what a full cycle would look like."""
    _header("Orchestrator Dry Run — Scan → Size → Preview")

    lighter = LighterAdapter(
        ws_url=os.environ.get("LIGHTER_WS_URL", ""),
        rest_url=os.environ.get("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai"),
        account_index=int(os.environ.get("LIGHTER_ACCOUNT_INDEX", "0")),
    )
    edgex = EdgeXAdapter(
        base_url=os.environ.get("EDGEX_BASE_URL", "https://pro.edgex.exchange"),
        account_id=int(os.environ.get("EDGEX_ACCOUNT_ID", "0")),
        private_key=os.environ.get("EDGEX_STARK_PRIVATE_KEY", "0x0"),
    )

    try:
        # --- IDLE → check flat ---
        print("\n[IDLE] Checking positions are flat...")
        l_pos = await lighter.get_open_positions()
        e_ok = True
        try:
            e_pos = await edgex.get_open_positions()
        except Exception:
            e_pos = []
            e_ok = False

        l_active = [p for p in l_pos if abs(p.size) > 1e-8]
        e_active = [p for p in e_pos if abs(p.size) > 1e-8]

        if l_active:
            print(f"  Lighter active: {[(p.symbol, p.size) for p in l_active]}")
        if e_active:
            print(f"  EdgeX active:   {[(p.symbol, p.size) for p in e_active]}")

        if not l_active and not e_active:
            _ok("Positions", "both exchanges flat")
        else:
            _fail("Positions", "not flat — close existing positions first")

        # --- ANALYZING → scan ---
        print("\n[ANALYZING] Scanning for opportunities...")
        symbols = [symbol] if symbol else ["BTC", "ETH", "SOL", "DOGE"]
        scan_config = ScanConfig(
            symbols=symbols,
            min_net_apr_threshold=5.0,
            max_spread_pct=0.15,
        )
        candidates = await scan_all(lighter, edgex, scan_config)

        if not candidates:
            print("  No candidates found with current thresholds")
            return

        _ok("Scan", f"{len(candidates)} candidates")
        for i, c in enumerate(candidates[:5]):
            print(f"  {i+1}. {c.symbol:6s}  net_apr={c.net_apr:7.2f}%  spread={c.spread:.4f}%  dir={c.direction}")

        # --- OPENING → size calculation ---
        best = candidates[0]
        print(f"\n[OPENING] Best candidate: {best.symbol}")

        # Market details
        l_md = await lighter.get_market_details(best.symbol)
        e_md = await edgex.get_market_details(best.symbol)
        print(f"  Lighter market_id={l_md.market_id} tick={l_md.price_tick} step={l_md.size_step}")
        print(f"  EdgeX   contract_id={e_md.market_id} tick={e_md.price_tick} step={e_md.size_step}")

        # Balance
        l_bal = await lighter.get_balance()
        try:
            e_bal = await edgex.get_balance()
        except Exception:
            e_bal = type("_", (), {"available": l_bal.available})()
        print(f"  Lighter available={l_bal.available:.2f}")
        print(f"  EdgeX   available={e_bal.available:.2f}{' (fallback)' if e_bal is l_bal else ''}")

        # Size
        mid = (best.lighter_bid + best.lighter_ask + best.edgex_bid + best.edgex_ask) / 4
        size_base = calculate_position_size(
            e_bal.available, l_bal.available,
            leverage=3, mid_price=mid, safety_factor=0.95,
        )
        size_base = unify_size_step(size_base, e_md.size_step, l_md.size_step)
        notional = size_base * mid
        print(f"  Mid price={mid:.2f}")
        print(f"  Position size={size_base:.4f} {best.symbol}  notional={notional:.2f} USD (3x)")

        # Direction & prices
        if best.direction == "long_edgex_short_lighter":
            edgex_side, lighter_side = "buy", "sell"
        else:
            edgex_side, lighter_side = "sell", "buy"

        tick = max(l_md.price_tick, e_md.price_tick)
        edgex_price = cross_price(edgex_side, best.edgex_bid, best.edgex_ask, tick, cross_pct=3.0)
        lighter_price = cross_price(lighter_side, best.lighter_bid, best.lighter_ask, tick, cross_pct=3.0)
        print(f"\n  EdgeX:   {edgex_side} {size_base} @ {edgex_price}")
        print(f"  Lighter:  {lighter_side} {size_base} @ {lighter_price}")

        # EdgeX status
        if not e_ok:
            print(f"\n  ⚠ EdgeX private API unavailable — would fail on open_position()")
            print(f"  → Lighter leg would be placed then rolled back")

        # --- HOLDING → PnL check ---
        print(f"\n[HOLDING] Would hold for ~8h, checking every 60s")
        print(f"  Stop-loss: enabled at {(100/3)*0.7:.0f}% of notional")

        # --- CLOSING ---
        close_edgex_side = "sell" if edgex_side == "buy" else "buy"
        close_lighter_side = "sell" if lighter_side == "buy" else "buy"
        e_close_px = cross_price(close_edgex_side, best.edgex_bid, best.edgex_ask, tick, cross_pct=3.0)
        l_close_px = cross_price(close_lighter_side, best.lighter_bid, best.lighter_ask, tick, cross_pct=3.0)
        print(f"\n[CLOSING] Would close:")
        print(f"  EdgeX:   {close_edgex_side} {size_base} @ {e_close_px}")
        print(f"  Lighter:  {close_lighter_side} {size_base} @ {l_close_px}")

        # --- WAITING ---
        print(f"\n[WAITING] Cool-down 5 min → back to IDLE")

    finally:
        await lighter.close()
        await edgex.close()


# ---------------------------------------------------------------------------
# live Lighter-only cycle
# ---------------------------------------------------------------------------

async def test_live_cycle(symbol: str, notional_usd: float) -> None:
    """Place a Lighter position and close it after a short hold."""
    _header(f"Live Cycle — {symbol} {notional_usd} USD (Lighter only)")

    import lighter as lighter_sdk

    lighter = LighterAdapter(
        ws_url=os.environ.get("LIGHTER_WS_URL", ""),
        rest_url=os.environ.get("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai"),
        account_index=int(os.environ.get("LIGHTER_ACCOUNT_INDEX", "0")),
    )

    private_key = os.environ.get("LIGHTER_PRIVATE_KEY", "")
    api_key_index = int(os.environ.get("LIGHTER_API_KEY_INDEX", "5"))
    account_index = int(os.environ.get("LIGHTER_ACCOUNT_INDEX", "0"))

    try:
        # 1. Pre-flight checks
        positions = await lighter.get_open_positions()
        active = [p for p in positions if abs(p.size) > 1e-8 and p.symbol.upper() == symbol.upper()]
        if active:
            _fail("Pre-flight", f"{symbol} position already open: {[(p.size, p.entry_price) for p in active]}")
            return
        _ok("Pre-flight", "no existing position")

        md = await lighter.get_market_details(symbol)
        bba = await lighter.get_best_bid_ask(md.market_id)
        bal = await lighter.get_balance()
        mid = (bba.bid + bba.ask) / 2
        _ok("Market", f"{symbol} mid={mid:.2f}  balance={bal.available:.2f}")

        # 2. Size
        size_raw = notional_usd / mid
        size = round_size_to_step(size_raw, md.size_step)
        if size <= 0:
            _fail("Size", f"too small: {size_raw} rounded to 0 (step={md.size_step})")
            return
        _ok("Size", f"{notional_usd} USD → {size} {symbol}")

        # 3. Open
        buy_price = cross_price("buy", bba.bid, bba.ask, md.price_tick, cross_pct=3.0)
        client_order_id = int(time.time() * 1_000_000) % 1_000_000
        base_scaled = int(size / md.size_step)
        price_scaled = int(buy_price / md.price_tick)

        print(f"\n  >>> OPENING: BUY {size} {symbol} @ {buy_price}")
        signer = lighter_sdk.SignerClient(
            url=os.environ.get("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai"),
            account_index=account_index,
            api_private_keys={api_key_index: private_key},
        )
        try:
            _tx, tx_hash, err = await signer.create_order(
                market_index=md.market_id,
                client_order_index=client_order_id,
                base_amount=base_scaled,
                price=price_scaled,
                is_ask=False,
                order_type=signer.ORDER_TYPE_LIMIT,
                time_in_force=signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                api_key_index=api_key_index,
            )
            if err:
                _fail("Open", str(err))
                return
            _ok("Open", f"order placed, id={client_order_id}")
        finally:
            await signer.close()

        # 4. Confirm position
        print("\n  Confirming position...")
        confirmed = False
        for _ in range(10):
            await asyncio.sleep(2)
            pos = await lighter.get_open_positions()
            match = [p for p in pos if p.symbol.upper() == symbol.upper() and abs(p.size) > 1e-8]
            if match:
                entry = match[0].entry_price
                psize = match[0].size
                _ok("Confirmed", f"{symbol} size={psize} entry={entry}")
                confirmed = True
                break
            print("    waiting...")

        if not confirmed:
            _fail("Confirm", "position not found after 20s — may still be pending")
            return

        # 5. Brief hold
        hold_s = 5
        print(f"\n  Holding {hold_s}s...")
        await asyncio.sleep(hold_s)

        # 6. Close
        bba2 = await lighter.get_best_bid_ask(md.market_id)
        sell_price = cross_price("sell", bba2.bid, bba2.ask, md.price_tick, cross_pct=3.0)
        close_order_id = int(time.time() * 1_000_000) % 1_000_000
        size_scaled = int(psize / md.size_step)
        price_scaled_close = int(sell_price / md.price_tick)

        print(f"\n  >>> CLOSING: SELL {psize} {symbol} @ {sell_price}")
        signer2 = lighter_sdk.SignerClient(
            url=os.environ.get("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai"),
            account_index=account_index,
            api_private_keys={api_key_index: private_key},
        )
        try:
            _tx, tx_hash, err = await signer2.create_order(
                market_index=md.market_id,
                client_order_index=close_order_id,
                base_amount=size_scaled,
                price=price_scaled_close,
                is_ask=True,
                order_type=signer2.ORDER_TYPE_LIMIT,
                time_in_force=signer2.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                reduce_only=True,
                api_key_index=api_key_index,
            )
            if err:
                _fail("Close", str(err))
                return
            _ok("Close", f"order placed, id={close_order_id}")
        finally:
            await signer2.close()

        # 7. Confirm closed
        print("\n  Confirming closure...")
        await asyncio.sleep(3)
        final_pos = await lighter.get_open_positions()
        still_open = [p for p in final_pos if p.symbol.upper() == symbol.upper() and abs(p.size) > 1e-8]
        if not still_open:
            _ok("Flat", "position closed successfully")
        else:
            print(f"    position still open: {still_open[0].size} — may close shortly")

    finally:
        await lighter.close()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

async def _main() -> None:
    parser = argparse.ArgumentParser(description="Orchestrator dry-run test")
    parser.add_argument("--live", action="store_true", help="Execute real open→close on Lighter")
    parser.add_argument("--symbol", default=None, help="Symbol override (default: best candidate)")
    parser.add_argument("--notional", type=float, default=20.0, help="Live notional in USD (default: 20)")
    args = parser.parse_args()

    await test_dry_run(args.symbol)

    if args.live:
        symbol = args.symbol or "BTC"
        await test_live_cycle(symbol, args.notional)


if __name__ == "__main__":
    asyncio.run(_main())
