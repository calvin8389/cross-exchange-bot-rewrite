#!/usr/bin/env python3
"""Execution and sizing module test — validate sizing math with real market data,
optionally test Lighter order placement.

Usage:
  python tests/test_execution.py              # sizing math only (safe, no orders)
  python tests/test_execution.py --live       # include Lighter test order
  python tests/test_execution.py --symbol ETH  # test a specific symbol
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

from src.core.sizing import (
    calculate_position_size,
    cross_price,
    round_price_to_tick,
    round_size_to_step,
    unify_size_step,
)
from src.exchanges.lighter_adapter import LighterAdapter
from src.exchanges.edgex_adapter import EdgeXAdapter


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
# sizing math tests
# ---------------------------------------------------------------------------

async def test_sizing(symbol: str) -> None:
    _header("Sizing Math — Real Market Data")

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
        # 1. Fetch balances
        l_bal = await lighter.get_balance()
        _ok("Lighter balance", f"available={l_bal.available:.2f}  total_equity={l_bal.total_equity:.2f}")

        try:
            e_bal = await edgex.get_balance()
            _ok("EdgeX balance", f"available={e_bal.available:.2f}  total_equity={e_bal.total_equity:.2f}")
            edgex_available = e_bal.available
        except Exception as e:
            _fail("EdgeX balance", str(e)[:80])
            edgex_available = l_bal.available  # fallback: assume equal

        # 2. Fetch market details
        l_md = await lighter.get_market_details(symbol)
        e_md = await edgex.get_market_details(symbol)
        _ok("Market details",
            f"{symbol} Lighter#{l_md.market_id} tick={l_md.price_tick} step={l_md.size_step}  "
            f"EdgeX#{e_md.market_id} tick={e_md.price_tick} step={e_md.size_step}")

        # 3. Fetch prices
        l_bba = await lighter.get_best_bid_ask(l_md.market_id)
        e_bba = await edgex.get_best_bid_ask(e_md.market_id)
        mid = (l_bba.bid + l_bba.ask + e_bba.bid + e_bba.ask) / 4.0
        _ok("Prices", f"Lighter {l_bba.bid}/{l_bba.ask}  EdgeX {e_bba.bid}/{e_bba.ask}  mid={mid:.2f}")

        # 4. Size calculation
        print()
        for lev in [1, 2, 3, 5]:
            size = calculate_position_size(
                edgex_available, l_bal.available,
                leverage=lev, mid_price=mid, safety_factor=0.95,
            )
            notional = size * mid
            print(f"      leverage={lev}x  size={size:.4f} {symbol}  notional={notional:.2f} USD")

        size_base = calculate_position_size(
            edgex_available, l_bal.available,
            leverage=3, mid_price=mid, safety_factor=0.95,
        )
        _ok("Default (3x)", f"size={size_base:.4f} {symbol}  notional={size_base * mid:.2f} USD")

        # 5. Tick/step rounding
        raw_buy_price = mid * 1.03  # 3% cross
        raw_sell_price = mid * 0.97
        tick = max(l_md.price_tick, e_md.price_tick)  # unify to coarser
        buy_px = round_price_to_tick(raw_buy_price, tick, "buy")
        sell_px = round_price_to_tick(raw_sell_price, tick, "sell")
        _ok("Price rounding", f"tick={tick}  buy: {raw_buy_price:.6f}→{buy_px}  sell: {raw_sell_price:.6f}→{sell_px}")

        step = max(l_md.size_step, e_md.size_step)
        raw_size = 0.00123456
        rounded = round_size_to_step(raw_size, step)
        _ok("Size rounding", f"step={step}  {raw_size}→{rounded}")

        unified = unify_size_step(size_base, e_md.size_step, l_md.size_step)
        _ok("Unify step", f"edgex_step={e_md.size_step} lighter_step={l_md.size_step} → {size_base:.8f}→{unified}")

        # 6. Cross price calculation
        cp_buy = cross_price("buy", l_bba.bid, l_bba.ask, tick, 3.0)
        cp_sell = cross_price("sell", l_bba.bid, l_bba.ask, tick, 3.0)
        mid_l = (l_bba.bid + l_bba.ask) / 2
        _ok("Cross price", f"Lighter mid={mid_l:.2f}  buy(3%)={cp_buy}  sell(3%)={cp_sell}")

    finally:
        await lighter.close()
        await edgex.close()


# ---------------------------------------------------------------------------
# Lighter live order test
# ---------------------------------------------------------------------------

async def test_live_order(symbol: str, notional_usd: float) -> None:
    _header(f"Live Order — {symbol} {notional_usd} USD (Lighter only)")

    import lighter as lighter_sdk

    lighter = LighterAdapter(
        ws_url=os.environ.get("LIGHTER_WS_URL", ""),
        rest_url=os.environ.get("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai"),
        account_index=int(os.environ.get("LIGHTER_ACCOUNT_INDEX", "0")),
    )

    try:
        # 1. Market data
        md = await lighter.get_market_details(symbol)
        bba = await lighter.get_best_bid_ask(md.market_id)
        mid = (bba.bid + bba.ask) / 2
        _ok("Market", f"{symbol} id={md.market_id} mid={mid:.2f} tick={md.price_tick} step={md.size_step}")

        # 2. Balance
        bal = await lighter.get_balance()
        _ok("Balance", f"available={bal.available:.2f}")

        # 3. Size
        size_raw = notional_usd / mid
        size_base = round_size_to_step(size_raw, md.size_step)
        actual_notional = size_base * mid
        _ok("Size", f"{notional_usd} USD / {mid} = {size_raw:.6f} → rounded={size_base} ({actual_notional:.2f} USD)")

        if size_base <= 0:
            _fail("Size", "too small")
            return

        # 4. Price (aggressive cross)
        buy_price = cross_price("buy", bba.bid, bba.ask, md.price_tick, cross_pct=3.0)
        _ok("Order price", f"ask={bba.ask}  cross(3%)={buy_price}")

        # 5. Place order
        private_key = os.environ.get("LIGHTER_PRIVATE_KEY", "")
        api_key_index = int(os.environ.get("LIGHTER_API_KEY_INDEX", "5"))
        account_index = int(os.environ.get("LIGHTER_ACCOUNT_INDEX", "0"))

        import time
        client_order_id = int(time.time() * 1_000_000) % 1_000_000

        base_scaled = int(size_base / md.size_step)
        price_scaled = int(buy_price / md.price_tick)

        print(f"\n      Placing: {symbol} BUY {size_base} @ {buy_price}")
        print(f"      Scaled: base_amount={base_scaled} price={price_scaled} client_order_id={client_order_id}")

        signer = lighter_sdk.SignerClient(
            url=os.environ.get("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai"),
            account_index=account_index,
            api_private_keys={api_key_index: private_key},
        )
        try:
            tx, tx_hash, err = await signer.create_order(
                market_index=md.market_id,
                client_order_index=client_order_id,
                base_amount=base_scaled,
                price=price_scaled,
                is_ask=False,
                order_type=signer.ORDER_TYPE_LIMIT,
                time_in_force=signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                reduce_only=False,
                trigger_price=0,
                api_key_index=api_key_index,
            )
            if err:
                _fail("Order", str(err))
            else:
                _ok("Order placed", f"client_order_id={client_order_id}")
                try:
                    hash_dict = tx_hash.to_dict() if hasattr(tx_hash, 'to_dict') else str(tx_hash)
                except Exception:
                    hash_dict = str(tx_hash) if tx_hash else "N/A"
                print(f"      tx_hash={hash_dict}")

                # 6. Verify position
                await asyncio.sleep(3)
                pos = await lighter.get_open_positions()
                active = [p for p in pos if p.symbol.upper() == symbol.upper() and abs(p.size) > 1e-8]
                if active:
                    _ok("Position confirmed", f"{active[0].symbol} size={active[0].size} entry={active[0].entry_price}")
                else:
                    print(f"      (position may take a moment to appear)")
        finally:
            await signer.close()

    finally:
        await lighter.close()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

async def _main() -> None:
    parser = argparse.ArgumentParser(description="Execution & sizing module test")
    parser.add_argument("--live", action="store_true", help="Place a real Lighter test order")
    parser.add_argument("--symbol", default="BTC", help="Symbol (default: BTC)")
    parser.add_argument("--notional", type=float, default=50.0, help="Live order notional in USD (default: 50)")
    args = parser.parse_args()

    await test_sizing(args.symbol)

    if args.live:
        await test_live_order(args.symbol, args.notional)


if __name__ == "__main__":
    asyncio.run(_main())
