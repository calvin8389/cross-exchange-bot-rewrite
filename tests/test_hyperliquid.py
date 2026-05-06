#!/usr/bin/env python3
"""Hyperliquid exchange smoke test — public data + account data + order placement.

Usage:
  python tests/test_hyperliquid.py                  # all checks
  python tests/test_hyperliquid.py --public          # public data only
  python tests/test_hyperliquid.py --account         # account data only
  python tests/test_hyperliquid.py --order           # order placement only (buy ~100 USDC BTC)
  python tests/test_hyperliquid.py --symbol ETH       # test a different coin
  python tests/test_hyperliquid.py --order --notional 50 --symbol SOL
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

from src.exchanges.hyperliquid_adapter import HyperliquidAdapter


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
# public data
# ---------------------------------------------------------------------------

async def test_public(symbol: str) -> None:
    _header("Hyperliquid Public Data (no auth required)")

    adapter = HyperliquidAdapter(
        base_url=os.environ.get("HYPERLIQUID_BASE_URL", "https://api.hyperliquid.xyz"),
        private_key_hex="0x0",  # dummy for public-only
        account_address="0x0",  # dummy for public-only
    )

    try:
        # --- market metadata ---
        md = await adapter.get_market_details(symbol)
        _ok("market details", f"{symbol} -> market_id={md.market_id}  price_tick={md.price_tick}  size_step={md.size_step}")

        # --- order book ---
        bba = await adapter.get_best_bid_ask(md.market_id)
        mid = (bba.bid + bba.ask) / 2
        _ok("order book", f"bid={bba.bid:.2f}  ask={bba.ask:.2f}  mid={mid:.2f}")

        # --- funding rate ---
        fr = await adapter.get_funding_rate(md.market_id)
        if fr:
            _ok("funding rate", f"{symbol} rate={fr.rate:.8f} ({fr.apr:.2f}% APR)")
        else:
            _fail("funding rate", "not found")
    except Exception as e:
        _fail("public data", str(e))
    finally:
        await adapter.close()


# ---------------------------------------------------------------------------
# account data
# ---------------------------------------------------------------------------

async def test_account(symbol: str) -> None:
    _header("Hyperliquid Account Data (requires private key + account address)")

    private_key = os.environ.get("HYPERLIQUID_PRIVATE_KEY", "").strip()
    account_address = os.environ.get("HYPERLIQUID_ACCOUNT_ADDRESS", "").strip()
    if not private_key:
        _fail("config", "HYPERLIQUID_PRIVATE_KEY not set in .env")
        return
    if not account_address:
        _fail("config", "HYPERLIQUID_ACCOUNT_ADDRESS not set in .env")
        return

    adapter = HyperliquidAdapter(
        base_url=os.environ.get("HYPERLIQUID_BASE_URL", "https://api.hyperliquid.xyz"),
        private_key_hex=private_key,
        account_address=account_address,
    )

    try:
        # --- balance ---
        bal = await adapter.get_balance()
        _ok("balance", f"total_equity={bal.total_equity:.2f}  available={bal.available:.2f}")

        # --- positions ---
        positions = await adapter.get_open_positions()
        if positions:
            _ok("positions", f"{len(positions)} active position(s)")
            for p in positions:
                side = "LONG" if p.size > 0 else "SHORT"
                print(f"      {p.symbol:8s}  {side:6s}  size={p.size:+.4f}  entry={p.entry_price:.4f}  uPnL={p.unrealized_pnl:.4f}")
        else:
            _ok("positions", "no open positions")
    except Exception as e:
        _fail("account data", str(e))
    finally:
        await adapter.close()


# ---------------------------------------------------------------------------
# order placement
# ---------------------------------------------------------------------------

async def test_order(symbol: str, notional: float) -> None:
    _header(f"Hyperliquid Order Placement — BUY ~{notional:.0f} USDC {symbol}")

    private_key = os.environ.get("HYPERLIQUID_PRIVATE_KEY", "").strip()
    account_address = os.environ.get("HYPERLIQUID_ACCOUNT_ADDRESS", "").strip()
    if not private_key:
        _fail("config", "HYPERLIQUID_PRIVATE_KEY not set in .env")
        return
    if not account_address:
        _fail("config", "HYPERLIQUID_ACCOUNT_ADDRESS not set in .env")
        return

    adapter = HyperliquidAdapter(
        base_url=os.environ.get("HYPERLIQUID_BASE_URL", "https://api.hyperliquid.xyz"),
        private_key_hex=private_key,
        account_address=account_address,
    )

    try:
        # 1. Market details
        md = await adapter.get_market_details(symbol)
        _ok("market details", f"{symbol} -> market_id={md.market_id}  price_tick={md.price_tick}  size_step={md.size_step}")

        # 2. Best ask
        bba = await adapter.get_best_bid_ask(md.market_id)
        best_ask = bba.ask
        mid = (bba.bid + bba.ask) / 2
        _ok("order book", f"bid={bba.bid:.2f}  ask={bba.ask:.2f}  mid={mid:.2f}")

        # 3. Calculate size for ~notional USDC
        raw_size = notional / best_ask
        base_scaled = max(1, int(raw_size / md.size_step))
        size_base = base_scaled * md.size_step
        actual_notional = size_base * best_ask

        print(f"      target notional: ~{notional} USDC")
        print(f"      size: {size_base} {symbol}  price: {best_ask}  actual: ~{actual_notional:.2f} USDC")

        # 4. Place limit buy order
        oid = await adapter.place_order(
            symbol=symbol,
            side="buy",
            size_base=size_base,
            price=best_ask,
            market_id=md.market_id,
        )
        if oid:
            _ok("order placed", f"order_id={oid}")
        else:
            _fail("order", "returned None — check exchange logs")
    except Exception as e:
        _fail("order", str(e))
    finally:
        await adapter.close()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

async def _main() -> None:
    parser = argparse.ArgumentParser(description="Hyperliquid exchange smoke test")
    parser.add_argument("--public", action="store_true", help="Public data only")
    parser.add_argument("--account", action="store_true", help="Account data only")
    parser.add_argument("--order", action="store_true", help="Order placement only")
    parser.add_argument("--symbol", default="BTC", help="Symbol (default: BTC)")
    parser.add_argument("--notional", type=float, default=100.0, help="Order notional in USDC (default: 100)")
    args = parser.parse_args()

    run_all = not args.public and not args.account and not args.order

    if run_all or args.public:
        await test_public(args.symbol)

    if run_all or args.account:
        await test_account(args.symbol)

    if run_all or args.order:
        await test_order(args.symbol, args.notional)


if __name__ == "__main__":
    asyncio.run(_main())
