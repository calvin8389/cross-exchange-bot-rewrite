#!/usr/bin/env python3
"""Phase 8: Dual-leg micro trade. Full cycle: scan → open → confirm → close → verify.

Usage:
  python tests/test_dual_leg.py --symbol DOGE --notional 12 --pair lighter,hyperliquid
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


def _build_adapters(exchange_ids: list[str]) -> dict:
    adapters = {}
    for eid in exchange_ids:
        if eid == "lighter":
            from src.exchanges.lighter_adapter import LighterAdapter
            adapters[eid] = LighterAdapter(
                ws_url=os.environ["LIGHTER_WS_URL"],
                rest_url=os.environ["LIGHTER_BASE_URL"],
                account_index=int(os.environ.get("LIGHTER_ACCOUNT_INDEX", "0")),
            )
        elif eid == "hyperliquid":
            from src.exchanges.hyperliquid_adapter import HyperliquidAdapter
            adapters[eid] = HyperliquidAdapter(
                base_url=os.environ.get("HYPERLIQUID_BASE_URL", "https://api.hyperliquid.xyz"),
                private_key_hex=os.environ["HYPERLIQUID_PRIVATE_KEY"],
                account_address=os.environ["HYPERLIQUID_ACCOUNT_ADDRESS"],
            )
        elif eid == "grvt":
            from src.exchanges.grvt_adapter import GrvtAdapter
            adapters[eid] = GrvtAdapter(
                trading_account_id=os.environ["GRVT_TRADING_ACCOUNT_ID"],
                private_key=os.environ["GRVT_PRIVATE_KEY"],
                api_key=os.environ["GRVT_API_KEY"],
                env=os.environ.get("GRVT_ENV", "prod"),
            )
        elif eid == "edgex":
            from src.exchanges.edgex_adapter import EdgeXAdapter
            adapters[eid] = EdgeXAdapter(
                base_url=os.environ["EDGEX_BASE_URL"],
                account_id=int(os.environ["EDGEX_ACCOUNT_ID"]),
                private_key=os.environ["EDGEX_STARK_PRIVATE_KEY"],
            )
    return adapters


async def _main():
    parser = argparse.ArgumentParser(description="Phase 8: Dual-leg micro trade")
    parser.add_argument("--symbol", default="DOGE")
    parser.add_argument("--notional", type=float, default=12.0)
    parser.add_argument("--pair", default="lighter,hyperliquid",
                        help="Comma-separated exchange pair (e.g. lighter,hyperliquid)")
    args = parser.parse_args()

    symbol = args.symbol.upper()
    notional = args.notional
    ex_a, ex_b = [e.strip() for e in args.pair.split(",")]

    _header(f"Dual-Leg Micro Trade: {symbol} ${notional} on {ex_a}/{ex_b}")

    adapters = _build_adapters([ex_a, ex_b])
    adapter_a, adapter_b = adapters[ex_a], adapters[ex_b]

    # Temp DB for the cycle
    import tempfile as _tmp
    fd, db_path = _tmp.mkstemp(suffix=".sqlite")
    os.close(fd)
    from src.db.store import Store
    store = Store(db_path)
    await store.start()
    schema = Path(__file__).resolve().parent.parent / "src" / "db" / "schema.sql"
    await store.init_schema(schema.read_text())

    try:
        # 1. Scan
        from src.core.scanner import scan_all, ScanConfig
        config = ScanConfig(
            symbols=[symbol],
            min_net_apr_threshold=0.0,
            max_spread_pct=1.0,
        )
        candidates = await scan_all(adapters, config)
        if not candidates:
            _fail("scan", f"no candidates for {symbol}")
            return
        opp = candidates[0]
        _ok("scan", f"{symbol} net_apr={opp.net_apr:.2f}% {opp.long_leg.exchange_id}/{opp.short_leg.exchange_id}")

        # 2. Open
        from src.core.execution import ExecConfig, open_position
        cfg = ExecConfig(leverage=1, cross_pct=3.0, notional_override=notional)

        md_a = await adapter_a.get_market_details(symbol)
        md_b = await adapter_b.get_market_details(symbol)
        _ok("market", f"{ex_a}: tick={md_a.price_tick} step={md_a.size_step} | {ex_b}: tick={md_b.price_tick} step={md_b.size_step}")

        result = await open_position(opp, adapters, store, cfg)
        _ok("open", f"{symbol} long={opp.long_leg.exchange_id} short={opp.short_leg.exchange_id}")

        # 3. Verify both legs exist on exchange
        await asyncio.sleep(5)
        for _tick in range(5):
            pos_a = await adapter_a.get_open_positions()
            pos_b = await adapter_b.get_open_positions()
            match_a = [p for p in pos_a if p.symbol.upper() == symbol.upper() and abs(p.size) > 1e-8]
            match_b = [p for p in pos_b if p.symbol.upper() == symbol.upper() and abs(p.size) > 1e-8]
            if match_a and match_b:
                _ok("confirm", f"{ex_a}: {match_a[0].size:.4f} @ {match_a[0].entry_price:.4f} | {ex_b}: {match_b[0].size:.4f} @ {match_b[0].entry_price:.4f}")
                break
            await asyncio.sleep(2)
        else:
            _fail("confirm", f"not both legs found: {ex_a}={bool(match_a)}, {ex_b}={bool(match_b)}")
            return

        # 4. Close both legs
        bba_a = await adapter_a.get_best_bid_ask(md_a.market_id)
        bba_b = await adapter_b.get_best_bid_ask(md_b.market_id)
        close_side_a = "sell" if match_a[0].size > 0 else "buy"
        close_side_b = "sell" if match_b[0].size > 0 else "buy"
        price_a = bba_a.bid if close_side_a == "sell" else bba_a.ask
        price_b = bba_b.bid if close_side_b == "sell" else bba_b.ask

        r1 = await adapter_a.close_position(symbol, close_side_a, abs(match_a[0].size), price_a, md_a.market_id)
        r2 = await adapter_b.close_position(symbol, close_side_b, abs(match_b[0].size), price_b, md_b.market_id)
        _ok("close", f"{ex_a}: {'OK' if r1 else 'FAIL'} | {ex_b}: {'OK' if r2 else 'FAIL'}")

        # 5. Verify flat
        await asyncio.sleep(5)
        for _tick in range(5):
            pos_a2 = await adapter_a.get_open_positions()
            pos_b2 = await adapter_b.get_open_positions()
            still_a = [p for p in pos_a2 if p.symbol.upper() == symbol.upper() and abs(p.size) > 1e-8]
            still_b = [p for p in pos_b2 if p.symbol.upper() == symbol.upper() and abs(p.size) > 1e-8]
            if not still_a and not still_b:
                _ok("verify flat", "both legs closed")
                break
            # Retry stubborn legs
            if still_a:
                bba = await adapter_a.get_best_bid_ask(md_a.market_id)
                await adapter_a.place_order(symbol, close_side_a, abs(still_a[0].size), bba.bid, md_a.market_id)
            if still_b:
                bba = await adapter_b.get_best_bid_ask(md_b.market_id)
                await adapter_b.place_order(symbol, close_side_b, abs(still_b[0].size), bba.bid, md_b.market_id)
            await asyncio.sleep(3)
        else:
            _fail("verify flat", "positions still open - MANUAL CHECK NEEDED")
    finally:
        for a in adapters.values():
            await a.close()
        await store.close()
        os.unlink(db_path)


if __name__ == "__main__":
    asyncio.run(_main())
