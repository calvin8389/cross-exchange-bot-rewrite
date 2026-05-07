#!/usr/bin/env python3
"""Bot monitor: detect broken legs, print 15-min summaries."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv

load_dotenv()

CHECK_INTERVAL = 60  # seconds between checks
SUMMARY_INTERVAL = 900  # 15 minutes


def _now():
    return datetime.now().strftime("%H:%M:%S")


async def _get_all_positions():
    from src.exchanges.lighter_adapter import LighterAdapter
    from src.exchanges.hyperliquid_adapter import HyperliquidAdapter
    from src.exchanges.grvt_adapter import GrvtAdapter
    from src.exchanges.edgex_adapter import EdgeXAdapter

    configs = [
        (LighterAdapter, {"ws_url": os.environ.get("LIGHTER_WS_URL", ""),
                          "rest_url": os.environ.get("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai"),
                          "account_index": int(os.environ.get("LIGHTER_ACCOUNT_INDEX", "0"))}),
        (HyperliquidAdapter, {"base_url": os.environ.get("HYPERLIQUID_BASE_URL", "https://api.hyperliquid.xyz"),
                              "private_key_hex": os.environ.get("HYPERLIQUID_PRIVATE_KEY", ""),
                              "account_address": os.environ.get("HYPERLIQUID_ACCOUNT_ADDRESS", "")}),
        (GrvtAdapter, {"trading_account_id": os.environ.get("GRVT_TRADING_ACCOUNT_ID", ""),
                       "private_key": os.environ.get("GRVT_PRIVATE_KEY", ""),
                       "api_key": os.environ.get("GRVT_API_KEY", ""),
                       "env": os.environ.get("GRVT_ENV", "prod")}),
        (EdgeXAdapter, {"base_url": os.environ.get("EDGEX_BASE_URL", ""),
                        "account_id": int(os.environ.get("EDGEX_ACCOUNT_ID", "0")),
                        "private_key": os.environ.get("EDGEX_STARK_PRIVATE_KEY", "")}),
    ]

    all_pos = {}
    for cls, kwargs in configs:
        a = cls(**kwargs)
        pos = await a.get_open_positions()
        all_pos[a.exchange_id] = pos
        await a.close()
    return all_pos


def _match_legs(all_pos: dict) -> dict:
    """Match positions across exchanges into hedged pairs."""
    # Collect all positions by symbol
    by_symbol: dict[str, dict] = {}
    for ex_id, positions in all_pos.items():
        for p in positions:
            sym = p.symbol.upper()
            if sym not in by_symbol:
                by_symbol[sym] = {}
            side = "LONG" if p.size > 0 else "SHORT"
            by_symbol[sym][ex_id] = {"side": side, "size": abs(p.size), "entry": p.entry_price, "upnl": p.unrealized_pnl}

    hedged = []
    broken = []

    for sym, legs in by_symbol.items():
        longs = {ex: info for ex, info in legs.items() if info["side"] == "LONG"}
        shorts = {ex: info for ex, info in legs.items() if info["side"] == "SHORT"}
        if longs and shorts:
            hedged.append({"symbol": sym, "longs": longs, "shorts": shorts})
        elif longs and not shorts:
            broken.append({"symbol": sym, "type": "LONG_ONLY", "legs": longs})
        elif shorts and not longs:
            broken.append({"symbol": sym, "type": "SHORT_ONLY", "legs": shorts})

    return {"hedged": hedged, "broken": broken}


async def _close_broken_leg(matched: dict):
    """Close a broken leg on the exchange."""
    sym = matched["symbol"]
    leg_type = matched["type"]
    for ex_id, info in matched["legs"].items():
        side = "sell" if leg_type == "LONG_ONLY" else "buy"
        size = info["size"]
        print(f"  [{_now()}] AUTO-CLOSE {sym} {side} {size:.4f} on {ex_id}")

        if ex_id == "lighter":
            from src.exchanges.lighter_adapter import LighterAdapter
            a = LighterAdapter(
                ws_url=os.environ.get("LIGHTER_WS_URL", ""),
                rest_url=os.environ.get("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai"),
                account_index=int(os.environ.get("LIGHTER_ACCOUNT_INDEX", "0")),
            )
            md = await a.get_market_details(sym)
            bba = await a.get_best_bid_ask(md.market_id)
            px = bba.bid if side == "sell" else bba.ask
            r = await a.close_position(sym, side, size, px, md.market_id)
            print(f"    -> {'OK' if r else 'FAIL'} order_id={r.order_id if r else 'N/A'}")
            await a.close()

        elif ex_id == "hyperliquid":
            from hyperliquid.exchange import Exchange
            from hyperliquid.info import Info
            from hyperliquid.utils.signing import OrderType
            from eth_account import Account
            info = Info("https://api.hyperliquid.xyz", skip_ws=True)
            acct = Account.from_key(os.environ["HYPERLIQUID_PRIVATE_KEY"])
            ex = Exchange(acct, "https://api.hyperliquid.xyz",
                          account_address=os.environ["HYPERLIQUID_ACCOUNT_ADDRESS"])
            bba = info.l2_snapshot(sym)
            levels = bba["levels"]
            bid = float(levels[0][0]["px"])
            ask = float(levels[1][0]["px"])
            px = bid if side == "sell" else ask
            r = ex.order(name=sym, is_buy=(side == "buy"), sz=size, limit_px=px,
                         order_type=OrderType(limit={"tif": "Ioc"}), reduce_only=True)
            result = r["response"]["data"]["statuses"][0]
            if "error" in result:
                print(f"    -> FAIL(IOC): {result['error']}, retry GTC...")
                r2 = ex.order(name=sym, is_buy=(side == "buy"), sz=size, limit_px=px,
                              order_type=OrderType(limit={"tif": "Gtc"}), reduce_only=True)
                print(f"    -> GTC: {r2['response']['data']['statuses'][0]}")

        elif ex_id == "grvt":
            from src.exchanges.grvt_adapter import GrvtAdapter
            a = GrvtAdapter(
                trading_account_id=os.environ["GRVT_TRADING_ACCOUNT_ID"],
                private_key=os.environ["GRVT_PRIVATE_KEY"],
                api_key=os.environ["GRVT_API_KEY"],
                env=os.environ.get("GRVT_ENV", "prod"),
            )
            md = await a.get_market_details(sym)
            bba = await a.get_best_bid_ask(md.market_id)
            px = bba.bid if side == "sell" else bba.ask
            r = await a.close_position(sym, side, size, px, md.market_id)
            print(f"    -> {'OK' if r else 'FAIL'}")
            await a.close()

        elif ex_id == "edgex":
            from src.exchanges.edgex_adapter import EdgeXAdapter
            a = EdgeXAdapter(
                base_url=os.environ["EDGEX_BASE_URL"],
                account_id=int(os.environ["EDGEX_ACCOUNT_ID"]),
                private_key=os.environ["EDGEX_STARK_PRIVATE_KEY"],
            )
            md = await a.get_market_details(sym)
            bba = await a.get_best_bid_ask(md.market_id)
            px = bba.bid if side == "sell" else bba.ask
            r = await a.close_position(sym, side, size, px, md.market_id)
            print(f"    -> {'OK' if r else 'FAIL'}")
            await a.close()


async def _print_summary(all_pos: dict, matched: dict, cycle: int):
    """Print a 15-minute summary."""
    print(f"\n{'='*70}")
    print(f"  BOT SUMMARY  [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]  cycle #{cycle}")
    print(f"{'='*70}")

    # Balances (approximate from positions + uPnL)
    total_upnl = 0.0
    for ex_id, positions in all_pos.items():
        count = len(positions)
        upnl = sum(p.unrealized_pnl for p in positions)
        total_upnl += upnl
        if count:
            syms = ", ".join(f"{p.symbol} {('L' if p.size>0 else 'S')}{abs(p.size):.1f}" for p in positions)
            print(f"  {ex_id:>10}: {count} pos | uPnL={upnl:+.4f} | {syms}")
        else:
            print(f"  {ex_id:>10}: flat")

    print(f"  {'':>10}  Total uPnL: {total_upnl:+.4f}")

    # Hedged / broken
    h = len(matched["hedged"])
    b = len(matched["broken"])
    state = "✅ HEDGED" if h > 0 and b == 0 else ("⚠️ BROKEN" if b > 0 else "⚪ IDLE")
    print(f"  Status: {state} | hedged={h} pairs | broken={b} legs")

    if matched["broken"]:
        for br in matched["broken"]:
            legs_str = ", ".join(f"{ex}({info['side']} {info['size']:.4f})" for ex, info in br["legs"].items())
            print(f"    ❌ {br['symbol']}: {br['type']} -> {legs_str}")

    if matched["hedged"]:
        for hd in matched["hedged"][:8]:
            long_str = ", ".join(f"{ex} L {info['size']:.4f}" for ex, info in hd["longs"].items())
            short_str = ", ".join(f"{ex} S {info['size']:.4f}" for ex, info in hd["shorts"].items())
            print(f"    ✓ {hd['symbol']:>8}: {long_str} | {short_str}")

    print(f"{'='*70}\n")


async def main():
    print(f"[{_now()}] Monitor started. Check every {CHECK_INTERVAL}s, summary every {SUMMARY_INTERVAL//60}min")
    cycle = 0

    while True:
        try:
            all_pos = await _get_all_positions()
            matched = _match_legs(all_pos)
            cycle += 1

            # Detect and close broken legs
            for br in matched["broken"]:
                print(f"\n[{_now()}] ⚠️ BROKEN LEG DETECTED: {br['symbol']} {br['type']}")
                for ex, info in br["legs"].items():
                    print(f"  {ex}: {info['side']} {info['size']:.4f} entry={info['entry']:.4f} uPnL={info['upnl']:+.4f}")
                await _close_broken_leg(br)

            # 15-minute summary
            if cycle % (SUMMARY_INTERVAL // CHECK_INTERVAL) == 0:
                await _print_summary(all_pos, matched, cycle)

        except Exception as e:
            print(f"[{_now()}] Monitor error: {e}")

        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
