#!/usr/bin/env python3
"""EdgeX adapter smoke test — public data + account data + order placement.

Usage:
  python tests/test_edgex.py              # all checks
  python tests/test_edgex.py --public     # public data only (no auth)
  python tests/test_edgex.py --account    # account data only
  python tests/test_edgex.py --order      # order placement only (buy ~100 USDC BTC)
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
# public data
# ---------------------------------------------------------------------------

async def test_public(contract: str) -> None:
    _header("Public Data (no auth required)")

    from edgex_sdk import Client

    client = Client(
        base_url="https://pro.edgex.exchange",
        account_id=0,
        stark_private_key="0x0",
    )

    # --- metadata (market list) ---
    try:
        meta = await client.get_metadata()
        contracts = meta.get("data", {}).get("contractList", [])
        _ok("get_metadata", f"{len(contracts)} contracts loaded")

        for c in contracts[:5]:
            cid = c.get("contractId", "?")
            name = c.get("contractName", "?")
            tick = c.get("tickSize", "?")
            step = c.get("stepSize", "?")
            print(f"      {cid:>10s}  {name:20s}  tick={tick}\tstep={step}")
        if len(contracts) > 5:
            print(f"      ... and {len(contracts) - 5} more")

        target = next((c for c in contracts if c.get("contractName") == contract), None)
        if target:
            _ok("contract lookup", f"{contract} → id={target['contractId']} tick={target['tickSize']} step={target['stepSize']}")
            contract_id = target["contractId"]
        else:
            _fail("contract lookup", f"{contract} not found in metadata")
            contract_id = None
    except Exception as e:
        _fail("get_metadata", str(e))
        contract_id = None

    # --- 24h ticker ---
    if contract_id:
        try:
            quote = await client.get_24_hour_quote(contract_id)
            data = quote.get("data", [])
            if isinstance(data, list) and data:
                ticker = data[0]
            else:
                ticker = data if isinstance(data, dict) else {}
            last = ticker.get("lastPrice", "N/A")
            mark = ticker.get("markPrice", "N/A")
            vol = ticker.get("value", "N/A")
            funding = ticker.get("fundingRate", "N/A")
            _ok("get_24_hour_quote", f"last={last}  mark={mark}  vol={vol}  funding={funding}")
        except Exception as e:
            _fail("get_24_hour_quote", str(e))

    # --- order book (bid/ask) ---
    if contract_id:
        try:
            from edgex_sdk import GetOrderBookDepthParams
            depth = await client.quote.get_order_book_depth(
                GetOrderBookDepthParams(contract_id=contract_id, limit=15)
            )
            ob_data = depth.get("data", [{}])[0]
            bids = ob_data.get("bids", [])
            asks = ob_data.get("asks", [])
            bid = bids[0].get("price", "N/A") if bids else "N/A"
            ask = asks[0].get("price", "N/A") if asks else "N/A"
            _ok("order_book_depth", f"bid={bid}  ask={ask}")
        except Exception as e:
            _fail("order_book_depth", str(e))

    await client.close()


# ---------------------------------------------------------------------------
# account data
# ---------------------------------------------------------------------------

async def test_account(contract: str) -> None:
    _header("Account Data (auth required)")

    from edgex_sdk import Client

    base_url = _get_env_or_die("EDGEX_BASE_URL")
    account_id = int(_get_env_or_die("EDGEX_ACCOUNT_ID"))
    private_key = _get_env_or_die("EDGEX_STARK_PRIVATE_KEY")
    if private_key.startswith("0x") or private_key.startswith("0X"):
        private_key = private_key[2:]

    client = Client(
        base_url=base_url,
        account_id=account_id,
        stark_private_key=private_key,
    )

    # --- balance ---
    try:
        asset = await client.get_account_asset()
        asset_data = asset.get("data", {})
        collateral_list = asset_data.get("collateralAssetModelList", [])
        equity = sum(float(a.get("totalEquity", 0) or 0) for a in collateral_list)
        avail = sum(float(a.get("availableAmount", 0) or 0) for a in collateral_list)
        _ok("get_account_asset", f"totalEquity={equity:.2f}  available={avail:.2f}")
    except Exception as e:
        _fail("get_account_asset", str(e))

    # --- positions ---
    try:
        positions = await client.get_account_positions()
        pos_data = positions.get("data", {})
        pos_list = pos_data.get("positionAssetList", [])
        active = [p for p in pos_list if abs(float(p.get("size", 0) or 0)) > 1e-8]
        if active:
            _ok("get_account_positions", f"{len(active)} active position(s)")
            for p in active:
                cid = p.get("contractId", "?")
                size = float(p.get("size", 0) or 0)
                entry = float(p.get("entryPrice", 0) or 0)
                pnl = float(p.get("unrealizedPnl", 0) or 0)
                side = "LONG" if size > 0 else "SHORT"
                print(f"      {cid:20s}  {side:6s}  size={size:+.4f}  entry={entry:.4f}  uPnL={pnl:.4f}")
        else:
            _ok("get_account_positions", "no open positions")
    except Exception as e:
        _fail("get_account_positions", str(e))

    # --- active orders ---
    try:
        from edgex_sdk import GetActiveOrderParams
        orders = await client.get_active_orders(GetActiveOrderParams())
        order_data = orders.get("data", {})
        order_list = order_data.get("orderList", order_data.get("orders", []))
        if order_list:
            _ok("get_active_orders", f"{len(order_list)} open order(s)")
            for o in order_list[:5]:
                cid = o.get("contractId", "?")
                side = o.get("side", "?")
                size = o.get("size", "?")
                price = o.get("price", "?")
                print(f"      {cid} {side} size={size} price={price}")
        else:
            _ok("get_active_orders", "no open orders")
    except Exception as e:
        _fail("get_active_orders", str(e))

    await client.close()


# ---------------------------------------------------------------------------
# order placement
# ---------------------------------------------------------------------------

async def test_order(contract: str, notional: float) -> None:
    _header(f"EdgeX Order Placement — BUY ~{notional:.0f} USDC {contract}")

    from edgex_sdk import Client as EdgeXClient, CreateOrderParams, OrderSide, OrderType, TimeInForce

    base_url = _get_env_or_die("EDGEX_BASE_URL")
    account_id = int(_get_env_or_die("EDGEX_ACCOUNT_ID"))
    private_key = _get_env_or_die("EDGEX_STARK_PRIVATE_KEY")
    if private_key.startswith("0x") or private_key.startswith("0X"):
        private_key = private_key[2:]

    client = EdgeXClient(
        base_url=base_url,
        account_id=account_id,
        stark_private_key=private_key,
    )

    try:
        # 1. Metadata → find contract
        meta = await client.get_metadata()
        contracts = meta.get("data", {}).get("contractList", [])
        target = next((c for c in contracts if c.get("contractName") == contract), None)
        if not target:
            _fail("contract lookup", f"{contract} not found in metadata")
            return
        contract_id = target["contractId"]
        price_tick = float(target.get("tickSize", 0.01) or 0.01)
        size_step = float(target.get("stepSize", 0.001) or 0.001)
        _ok("contract", f"{contract} -> id={contract_id} tick={price_tick} step={size_step}")

        # 2. Best ask from order book depth
        from edgex_sdk import GetOrderBookDepthParams
        depth = await client.quote.get_order_book_depth(
            GetOrderBookDepthParams(contract_id=contract_id, limit=15)
        )
        ob_data = depth.get("data", [{}])[0]
        asks = ob_data.get("asks", [])
        if not asks:
            _fail("order book", "no asks available")
            return
        best_ask = float(asks[0].get("price", 0))
        _ok("best ask", str(best_ask))

        # 3. Calculate size for ~notional USDC
        raw_size = notional / best_ask
        base_scaled = max(1, int(raw_size / size_step))
        size_base = base_scaled * size_step
        actual_notional = size_base * best_ask

        print(f"      target notional: ~{notional} USDC")
        print(f"      size: {size_base} {contract}  price: {best_ask}  actual: ~{actual_notional:.2f} USDC")

        # 4. Place limit buy order
        params = CreateOrderParams(
            contract_id=contract_id,
            side=OrderSide.BUY,
            type=OrderType.LIMIT,
            price=str(best_ask),
            size=str(size_base),
            time_in_force=TimeInForce.GOOD_TIL_CANCEL,
        )
        result = await client.create_order(params)
        data = result.get("data", {}) if result else {}
        oid = data.get("orderId", "") if isinstance(data, dict) else (result or {}).get("orderId", "")
        if oid:
            _ok("order placed", f"order_id={oid}")
        else:
            _fail("order", f"returned {result}")
    except Exception as e:
        _fail("order", str(e))
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

async def _main() -> None:
    parser = argparse.ArgumentParser(description="EdgeX adapter smoke test")
    parser.add_argument("--public", action="store_true", help="Public data only")
    parser.add_argument("--account", action="store_true", help="Account data only")
    parser.add_argument("--order", action="store_true", help="Order placement only")
    parser.add_argument("--contract", default="BTCUSD", help="Contract name (default: BTCUSD)")
    parser.add_argument("--notional", type=float, default=100.0, help="Order notional in USDC (default: 100)")
    args = parser.parse_args()

    run_all = not args.public and not args.account and not args.order

    if run_all or args.public:
        await test_public(args.contract)

    if run_all or args.account:
        await test_account(args.contract)

    if run_all or args.order:
        await test_order(args.contract, args.notional)


if __name__ == "__main__":
    asyncio.run(_main())
