#!/usr/bin/env python3
"""Lighter exchange smoke test — public data, account data, order query, and order placement.

Usage:
  python tests/test_lighter.py                  # all checks
  python tests/test_lighter.py --public         # public data only
  python tests/test_lighter.py --account        # account data only
  python tests/test_lighter.py --order          # order placement only
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp
from dotenv import load_dotenv

load_dotenv()


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


def _get_env_or_die(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        print(f"FATAL: {key} not set in .env")
        sys.exit(1)
    return val


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai")
ACCOUNT_INDEX = int(os.environ.get("LIGHTER_ACCOUNT_INDEX", "298606"))
API_KEY_INDEX = int(os.environ.get("LIGHTER_API_KEY_INDEX", "5"))
PRIVATE_KEY = os.environ.get("LIGHTER_PRIVATE_KEY", "")


# ---------------------------------------------------------------------------
# public data
# ---------------------------------------------------------------------------

async def test_public(symbol: str) -> None:
    _header("Public Data (no auth required)")

    async with aiohttp.ClientSession() as session:

        # --- order books (market list) ---
        try:
            async with session.get(f"{BASE_URL}/api/v1/orderBooks") as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                data = await resp.json()
                order_books = data.get("order_books", [])
            _ok("orderBooks", f"{len(order_books)} markets loaded")

            for ob in order_books[:5]:
                mid = ob.get("market_id", "?")
                sym = ob.get("symbol", "?")
                pdec = ob.get("supported_price_decimals", "?")
                sdec = ob.get("supported_size_decimals", "?")
                print(f"      market_id={str(mid):>4s}  {sym:8s}  price_dec={pdec}  size_dec={sdec}")
            if len(order_books) > 5:
                print(f"      ... and {len(order_books) - 5} more")

            target = next((ob for ob in order_books if ob.get("symbol", "").upper() == symbol.upper()), None)
            if target:
                market_id = target["market_id"]
                price_tick = 10 ** -target["supported_price_decimals"]
                size_step = 10 ** -target["supported_size_decimals"]
                min_base = float(target.get("min_base_amount", 0))
                _ok("market lookup", f"{symbol} → market_id={market_id} price_tick={price_tick} size_step={size_step} min_base={min_base}")
            else:
                _fail("market lookup", f"{symbol} not found")
                market_id = None
                price_tick = None
                size_step = None
        except Exception as e:
            _fail("orderBooks", str(e))
            market_id = None
            price_tick = None
            size_step = None

        # --- order book depth ---
        if market_id:
            try:
                async with session.get(
                    f"{BASE_URL}/api/v1/orderBookOrders",
                    params={"market_id": str(market_id), "limit": "5"},
                ) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")
                    data = await resp.json()
                bids = data.get("bids", [])
                asks = data.get("asks", [])
                bid = bids[0]["price"] if bids else "N/A"
                ask = asks[0]["price"] if asks else "N/A"
                _ok("orderBookOrders", f"bid={bid}  ask={ask}")
            except Exception as e:
                _fail("orderBookOrders", str(e))

        # --- funding rates ---
        try:
            async with session.get(f"{BASE_URL}/api/v1/funding-rates") as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                data = await resp.json()
            rates = data.get("funding_rates", [])
            _ok("funding-rates", f"{len(rates)} rates loaded")
            btc_rate = next((r for r in rates if r.get("symbol", "").upper() == symbol.upper()), None)
            if btc_rate:
                rate = float(btc_rate.get("rate", 0))
                apr = rate * 365 * 24 * 100
                _ok("funding rate", f"{symbol} rate={rate} ({apr:.2f}% APR)")
            else:
                _fail("funding rate", f"{symbol} not found in funding rates")
        except Exception as e:
            _fail("funding-rates", str(e))


# ---------------------------------------------------------------------------
# account data
# ---------------------------------------------------------------------------

async def test_account(symbol: str) -> None:
    _header("Account Data (no auth for positions, auth for orders)")

    if not PRIVATE_KEY:
        _fail("config", "LIGHTER_PRIVATE_KEY not set, skipping authenticated queries")
        return

    async with aiohttp.ClientSession() as session:

        # --- account info + balance ---
        try:
            async with session.get(
                f"{BASE_URL}/api/v1/account",
                params={"by": "index", "value": str(ACCOUNT_INDEX)},
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                data = await resp.json()
            accounts = data.get("accounts", [])
            if not accounts:
                raise RuntimeError("no account returned")
            acc = accounts[0]
            avail = float(acc.get("available_balance", 0))
            collateral = float(acc.get("collateral", 0))
            total_val = float(acc.get("total_asset_value", 0))
            _ok("account", f"available={avail:.2f}  collateral={collateral:.2f}  total_value={total_val:.2f}")
        except Exception as e:
            _fail("account", str(e))
            acc = {}

        # --- positions ---
        try:
            pos_list = acc.get("positions", [])
            active = [p for p in pos_list if abs(float(p.get("position", 0) or 0)) > 1e-8]
            if active:
                _ok("positions", f"{len(active)} active position(s)")
                for p in active:
                    sym = p.get("symbol", "?")
                    size = float(p.get("position", 0) or 0)
                    sign = int(p.get("sign", 0))
                    signed_size = size * sign
                    entry = float(p.get("avg_entry_price", 0) or 0)
                    pnl = float(p.get("unrealized_pnl", 0) or 0)
                    side = "LONG" if signed_size > 0 else "SHORT"
                    print(f"      {sym:8s}  {side:6s}  size={signed_size:+.4f}  entry={entry:.4f}  uPnL={pnl:.4f}")
            else:
                _ok("positions", "no open positions")
        except Exception as e:
            _fail("positions", str(e))

        # --- active orders (requires auth) ---
        try:
            import lighter

            signer = lighter.SignerClient(
                url=BASE_URL,
                account_index=ACCOUNT_INDEX,
                api_private_keys={API_KEY_INDEX: PRIVATE_KEY},
            )
            try:
                auth_token, err = signer.create_auth_token_with_expiry(api_key_index=API_KEY_INDEX)
                if err:
                    raise RuntimeError(f"auth token error: {err}")

                # Find BTC market_id for orders query
                async with session.get(f"{BASE_URL}/api/v1/orderBooks") as resp:
                    ob_data = await resp.json()
                btc_market = next(
                    (ob for ob in ob_data.get("order_books", [])
                     if ob.get("symbol", "").upper() == symbol.upper()),
                    None,
                )
                mid = btc_market["market_id"] if btc_market else 1

                api = lighter.OrderApi()
                orders = await api.account_active_orders(
                    account_index=ACCOUNT_INDEX,
                    market_id=mid,
                    auth=auth_token,
                )
                order_data = orders.to_dict()
                order_list = order_data.get("orders", [])
                if order_list:
                    _ok("active_orders", f"{len(order_list)} open order(s)")
                    for o in order_list[:5]:
                        oid = o.get("order_id", o.get("order_index", "?"))
                        side = "SELL" if o.get("is_ask") else "BUY"
                        price = o.get("price", "?")
                        size = o.get("remaining_base_amount", o.get("initial_base_amount", "?"))
                        print(f"      {oid} {side} price={price} size={size}")
                else:
                    _ok("active_orders", "no open orders")
                await api.api_client.close()
            finally:
                await signer.close()
        except Exception as e:
            _fail("active_orders", str(e))


# ---------------------------------------------------------------------------
# order placement
# ---------------------------------------------------------------------------

async def test_order(symbol: str) -> None:
    _header("Order Placement (BTC buy, ~100 USDC worth)")

    if not PRIVATE_KEY:
        _fail("config", "LIGHTER_PRIVATE_KEY not set")
        return

    import lighter

    # 1. Get market details
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{BASE_URL}/api/v1/orderBooks") as resp:
            data = await resp.json()
        target = next(
            (ob for ob in data.get("order_books", [])
             if ob.get("symbol", "").upper() == symbol.upper()),
            None,
        )
        if not target:
            _fail("market lookup", f"{symbol} not found")
            return

        market_id = target["market_id"]
        price_tick = 10 ** -target["supported_price_decimals"]
        size_step = 10 ** -target["supported_size_decimals"]
        min_base = float(target.get("min_base_amount", 0))
        _ok("market", f"{symbol} market_id={market_id} price_tick={price_tick} size_step={size_step}")

        # 2. Get best ask
        async with session.get(
            f"{BASE_URL}/api/v1/orderBookOrders",
            params={"market_id": str(market_id), "limit": "1"},
        ) as resp:
            depth = await resp.json()
        asks = depth.get("asks", [])
        if not asks:
            _fail("order book", "no asks available")
            return
        best_ask = float(asks[0]["price"])
        _ok("best ask", str(best_ask))

    # 3. Calculate size for ~100 USDC
    target_notional = 100.0
    raw_size = target_notional / best_ask
    # Round to size_step
    base_scaled = int(raw_size / size_step)
    size_base = base_scaled * size_step
    actual_notional = size_base * best_ask

    if size_base < min_base:
        _fail("size check", f"{size_base} < min_base {min_base}, need more than {target_notional} USDC")
        return

    price_scaled = int(best_ask / price_tick)
    client_order_id = int(__import__("time").time() * 1_000_000) % 1_000_000

    print(f"      target notional: ~{target_notional} USDC")
    print(f"      size: {size_base} BTC  price: {best_ask}  actual: ~{actual_notional:.2f} USDC")
    print(f"      scaled: base_amount={base_scaled}  price={price_scaled}  client_order_id={client_order_id}")

    # 4. Place order
    signer = lighter.SignerClient(
        url=BASE_URL,
        account_index=ACCOUNT_INDEX,
        api_private_keys={API_KEY_INDEX: PRIVATE_KEY},
    )
    try:
        tx, tx_hash, err = await signer.create_order(
            market_index=market_id,
            client_order_index=client_order_id,
            base_amount=base_scaled,
            price=price_scaled,
            is_ask=False,
            order_type=signer.ORDER_TYPE_LIMIT,
            time_in_force=signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
            reduce_only=False,
            trigger_price=0,
            api_key_index=API_KEY_INDEX,
        )
        if err:
            _fail("create_order", str(err))
        else:
            try:
                tx_str = tx.to_json() if hasattr(tx, 'to_json') else str(tx) if tx else "N/A"
            except Exception:
                tx_str = str(tx) if tx else "N/A"
            try:
                hash_str = tx_hash.to_json() if hasattr(tx_hash, 'to_json') else str(tx_hash) if tx_hash else "N/A"
            except Exception:
                hash_str = str(tx_hash) if tx_hash else "N/A"
            _ok("create_order", f"client_order_id={client_order_id}")
            print(f"      tx={tx_str[:200]}")
            print(f"      tx_hash={hash_str[:200]}")
    except Exception as e:
        _fail("create_order", str(e))
    finally:
        await signer.close()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

async def _main() -> None:
    parser = argparse.ArgumentParser(description="Lighter exchange smoke test")
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
        await test_order(args.symbol)


if __name__ == "__main__":
    asyncio.run(_main())
