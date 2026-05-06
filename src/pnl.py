"""P&L calculator — reads cycles/positions/legs from DB and computes realized P&L.

Usage:
  python -m src.pnl                    # summary
  python -m src.pnl --detail           # per-cycle breakdown
  python -m src.pnl --since 2026-05-01 # filter by date
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from typing import Optional

import aiosqlite


def _fmt_usd(v: float) -> str:
    color = "\033[32m" if v >= 0 else "\033[31m"
    return f"{color}${v:+.2f}\033[0m"


async def _query(conn: aiosqlite.Connection, since: Optional[str] = None) -> dict:
    """Query all data needed for P&L."""
    where = ""
    params: tuple = ()
    if since:
        where = "WHERE closed_at >= ?"
        params = (since,)

    if params:
        cycles = await conn.execute_fetchall(
            f"SELECT * FROM cycles {where} ORDER BY closed_at DESC", params
        )
    else:
        cycles = await conn.execute_fetchall(
            f"SELECT * FROM cycles ORDER BY closed_at DESC"
        )

    fee_rates = {
        "lighter": {"taker": 0.0006, "maker": 0.0002},
        "hyperliquid": {"taker": 0.00035, "maker": 0.0001},
        "edgex": {"taker": 0.00038, "maker": 0.00015},
        "grvt": {"taker": 0.0005, "maker": 0.0002},
    }

    return {
        "cycles": cycles,
        "fee_rates": fee_rates,
    }


def _estimate_fees(cycle_row, fee_rates: dict) -> tuple[float, float]:
    """Estimate taker fees for both legs based on notional and fee rates."""
    long_ex = cycle_row["exchange_long"]
    short_ex = cycle_row["exchange_short"]
    long_size = float(cycle_row["long_size"] or 0)
    short_size = float(cycle_row["short_size"] or 0)
    long_entry = float(cycle_row["long_entry_price"] or 0)
    short_entry = float(cycle_row["short_entry_price"] or 0)

    # Use taker fee since we cross the spread
    long_rate = fee_rates.get(long_ex, {}).get("taker", 0.0005)
    short_rate = fee_rates.get(short_ex, {}).get("taker", 0.0005)

    # Open + close fees for each leg
    long_open_fee = long_size * long_entry * long_rate
    long_close_fee = long_size * long_entry * long_rate  # approximate
    short_open_fee = short_size * short_entry * short_rate
    short_close_fee = short_size * short_entry * short_rate

    total_fees = long_open_fee + long_close_fee + short_open_fee + short_close_fee
    return total_fees, long_rate + short_rate


async def run(db_path: str = "bot.sqlite3", detail: bool = False, since: Optional[str] = None):
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    try:
        data = await _query(conn, since)

        cycles = data["cycles"]
        fee_rates = data["fee_rates"]

        if not cycles:
            print("No closed cycles found.")
            return

        # Summary accumulators
        total_price_pnl = 0.0
        total_funding_pnl = 0.0
        total_fees = 0.0
        winning = 0
        losing = 0

        # Count by exchange pair
        pair_stats: dict[str, dict] = {}

        if detail:
            print(f"{'ID':>4} {'Symbol':>8} {'State':>7} {'Pair':>20} {'Price PnL':>10} {'Fund PnL':>10} {'Est Fees':>10} {'Net':>10}")
            print("-" * 95)

        for cy in cycles:
            long_ex = cy["exchange_long"]
            short_ex = cy["exchange_short"]
            symbol = cy["symbol"]
            state = cy["state"]

            long_pnl = float(cy["long_close_pnl"] or 0)
            short_pnl = float(cy["short_close_pnl"] or 0)
            long_fund = float(cy["long_funding_pnl"] or 0)
            short_fund = float(cy["short_funding_pnl"] or 0)

            price_pnl = long_pnl + short_pnl
            funding_pnl = long_fund + short_fund
            fees, _ = _estimate_fees(cy, fee_rates)
            net = price_pnl + funding_pnl - fees

            total_price_pnl += price_pnl
            total_funding_pnl += funding_pnl
            total_fees += fees

            if net > 0:
                winning += 1
            else:
                losing += 1

            pair = f"{long_ex}/{short_ex}"
            if pair not in pair_stats:
                pair_stats[pair] = {"count": 0, "price_pnl": 0.0, "funding_pnl": 0.0, "fees": 0.0}
            ps = pair_stats[pair]
            ps["count"] += 1
            ps["price_pnl"] += price_pnl
            ps["funding_pnl"] += funding_pnl
            ps["fees"] += fees

            if detail:
                print(f"{cy['id']:>4} {symbol:>8} {state:>7} {pair:>20} {_fmt_usd(price_pnl):>20} {_fmt_usd(funding_pnl):>20} {_fmt_usd(-fees):>20} {_fmt_usd(net):>20}")

        # Summary
        total_net = total_price_pnl + total_funding_pnl - total_fees
        print()
        print("=" * 60)
        print(f"  Total cycles: {len(cycles)}  (win={winning}, lose={losing})")
        print(f"  Price P&L:    {_fmt_usd(total_price_pnl)}")
        print(f"  Funding P&L:  {_fmt_usd(total_funding_pnl)}")
        print(f"  Est. Fees:    {_fmt_usd(-total_fees)}")
        print(f"  ─────────────────────────")
        print(f"  Net P&L:      {_fmt_usd(total_net)}")
        print("=" * 60)

        # By exchange pair
        if len(pair_stats) > 1:
            print("\n  By exchange pair:")
            for pair, ps in sorted(pair_stats.items()):
                net_p = ps["price_pnl"] + ps["funding_pnl"] - ps["fees"]
                print(f"    {pair:>20}: {ps['count']:>3} cycles  net={_fmt_usd(net_p)}")

        # Active positions P&L (unrealized)
        active = await conn.execute_fetchall(
            "SELECT p.*, pl.exchange_id, pl.side, pl.size, pl.entry_price, pl.unrealized_pnl "
            "FROM positions p JOIN position_legs pl ON p.id=pl.position_id "
            "WHERE p.is_active=1"
        )
        if active:
            print("\n  Unrealized (active positions):")
            by_symbol: dict[str, float] = {}
            for a in active:
                sym = a["symbol"]
                uPnL = float(a["unrealized_pnl"] or 0)
                by_symbol[sym] = by_symbol.get(sym, 0) + uPnL
            for sym, upnl in by_symbol.items():
                print(f"    {sym:>8}: {_fmt_usd(upnl)}")

    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description="P&L report")
    parser.add_argument("--detail", action="store_true", help="Per-cycle breakdown")
    parser.add_argument("--since", help="Filter by date (YYYY-MM-DD)")
    parser.add_argument("--db", default="bot.sqlite3", help="DB path")
    args = parser.parse_args()
    asyncio.run(run(args.db, args.detail, args.since))


if __name__ == "__main__":
    main()
