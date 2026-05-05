#!/usr/bin/env python3
"""Cross-exchange scanner integration test — fetches public data from Lighter + EdgeX,
computes funding-rate arbitrage opportunities, and prints ranked signals.

Usage:
  python tests/test_scanner.py
  python tests/test_scanner.py --min-apr 10 --max-spread 0.1
  python tests/test_scanner.py --top 10
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp
from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

LIGHTER_REST = "https://mainnet.zklighter.elliot.ai"
EDGEX_REST = "https://pro.edgex.exchange"


# ---------------------------------------------------------------------------
# data fetching
# ---------------------------------------------------------------------------

async def fetch_lighter_markets(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch all Lighter order books (market metadata)."""
    async with session.get(f"{LIGHTER_REST}/api/v1/orderBooks") as resp:
        if resp.status != 200:
            raise RuntimeError(f"Lighter orderBooks HTTP {resp.status}")
        data = await resp.json()
    return data.get("order_books", [])


async def fetch_lighter_funding_rates(session: aiohttp.ClientSession) -> dict[int, float]:
    """Return {market_id: rate} for all Lighter funding rates."""
    async with session.get(f"{LIGHTER_REST}/api/v1/funding-rates") as resp:
        if resp.status != 200:
            raise RuntimeError(f"Lighter funding-rates HTTP {resp.status}")
        data = await resp.json()
    return {
        int(r["market_id"]): float(r["rate"])
        for r in data.get("funding_rates", [])
    }


async def fetch_lighter_order_book(
    session: aiohttp.ClientSession, market_id: int, limit: int = 1
) -> tuple[Optional[float], Optional[float]]:
    """Return (best_bid, best_ask) for a Lighter market."""
    async with session.get(
        f"{LIGHTER_REST}/api/v1/orderBookOrders",
        params={"market_id": str(market_id), "limit": str(limit)},
    ) as resp:
        if resp.status != 200:
            return None, None
        data = await resp.json()
    bids = data.get("bids", [])
    asks = data.get("asks", [])
    bid = float(bids[0]["price"]) if bids else None
    ask = float(asks[0]["price"]) if asks else None
    return bid, ask


async def fetch_edgex_metadata() -> list[dict]:
    """Fetch EdgeX contract metadata (public, no auth)."""
    from edgex_sdk import Client as EdgeXClient

    client = EdgeXClient(
        base_url=EDGEX_REST,
        account_id=0,
        stark_private_key="0x0",
    )
    try:
        meta = await client.get_metadata()
        return meta.get("data", {}).get("contractList", [])
    finally:
        await client.close()


async def fetch_edgex_tickers(
    contract_ids: list[str]
) -> dict[str, dict]:
    """Fetch EdgeX 24h tickers for multiple contracts. Returns {contract_id: ticker}."""
    from edgex_sdk import Client as EdgeXClient

    client = EdgeXClient(
        base_url=EDGEX_REST,
        account_id=0,
        stark_private_key="0x0",
    )
    try:
        tickers = {}
        for cid in contract_ids:
            try:
                quote = await client.get_24_hour_quote(cid)
                data = quote.get("data", [])
                if isinstance(data, list) and data:
                    tickers[cid] = data[0]
                elif isinstance(data, dict):
                    tickers[cid] = data
            except Exception:
                pass
        return tickers
    finally:
        await client.close()


async def fetch_edgex_order_books(
    contract_ids: list[str]
) -> dict[str, tuple[Optional[float], Optional[float]]]:
    """Return {contract_id: (best_bid, best_ask)} for EdgeX order books."""
    from edgex_sdk import Client as EdgeXClient, GetOrderBookDepthParams

    client = EdgeXClient(
        base_url=EDGEX_REST,
        account_id=0,
        stark_private_key="0x0",
    )
    try:
        results = {}
        for cid in contract_ids:
            try:
                depth = await client.quote.get_order_book_depth(
                    GetOrderBookDepthParams(contract_id=cid, limit=15)
                )
                ob_data = depth.get("data", [{}])[0]
                bids = ob_data.get("bids", [])
                asks = ob_data.get("asks", [])
                bid = float(bids[0].get("price")) if bids else None
                ask = float(asks[0].get("price")) if asks else None
                results[cid] = (bid, ask)
            except Exception:
                results[cid] = (None, None)
        return results
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# signal computation
# ---------------------------------------------------------------------------

@dataclass
class Signal:
    symbol: str
    lighter_market_id: int
    edgex_contract_id: str
    edgex_rate: float
    lighter_rate: float
    edgex_apr: float
    lighter_apr: float
    net_apr: float
    direction: str
    spread_pct: float
    lighter_bid: float
    lighter_ask: float
    edgex_bid: float
    edgex_ask: float


def compute_signals(
    common_symbols: list[dict],
    lighter_funding: dict[int, float],
    lighter_ob: dict[int, tuple[Optional[float], Optional[float]]],
    edgex_tickers: dict[str, dict],
    edgex_ob: dict[str, tuple[Optional[float], Optional[float]]],
    min_net_apr: float = 5.0,
    max_spread_pct: float = 0.15,
) -> list[Signal]:
    """Compute funding-rate arbitrage signals for common symbols."""

    signals = []
    for row in common_symbols:
        symbol = row["symbol"]
        l_mid = row["lighter_market_id"]
        e_cid = row["edgex_contract_id"]

        # Funding
        l_rate = lighter_funding.get(l_mid)
        if l_rate is None:
            continue
        e_ticker = edgex_tickers.get(e_cid, {})
        e_rate_str = e_ticker.get("fundingRate")
        if e_rate_str is None:
            continue
        e_rate = float(e_rate_str)

        # Annualise to percentage: rate * 365 * 24 * 100
        # (matches adapter convention; actual interval count differs but ranking is preserved)
        e_apr = e_rate * 365 * 24 * 100
        l_apr = l_rate * 365 * 24 * 100

        net_apr = abs(e_apr - l_apr)
        if net_apr < min_net_apr:
            continue

        # Order book
        l_bba = lighter_ob.get(l_mid, (None, None))
        e_bba = edgex_ob.get(e_cid, (None, None))
        l_bid, l_ask = l_bba
        e_bid, e_ask = e_bba
        if any(v is None for v in (l_bid, l_ask, e_bid, e_ask)):
            continue

        # Spread between exchange mid-prices
        l_mid_price = (l_bid + l_ask) / 2
        e_mid_price = (e_bid + e_ask) / 2
        spread_pct = abs(l_mid_price - e_mid_price) / min(l_mid_price, e_mid_price) * 100

        if spread_pct > max_spread_pct:
            continue

        if e_apr > l_apr:
            direction = "long_edgex_short_lighter"
        else:
            direction = "short_edgex_long_lighter"

        signals.append(Signal(
            symbol=symbol,
            lighter_market_id=l_mid,
            edgex_contract_id=e_cid,
            edgex_rate=e_rate,
            lighter_rate=l_rate,
            edgex_apr=e_apr,
            lighter_apr=l_apr,
            net_apr=net_apr,
            direction=direction,
            spread_pct=spread_pct,
            lighter_bid=l_bid,
            lighter_ask=l_ask,
            edgex_bid=e_bid,
            edgex_ask=e_ask,
        ))

    signals.sort(key=lambda s: s.net_apr, reverse=True)
    return signals


# ---------------------------------------------------------------------------
# symbol cross-reference
# ---------------------------------------------------------------------------

def build_symbol_map(
    lighter_markets: list[dict],
    edgex_contracts: list[dict],
    target_symbols: Optional[list[str]] = None,
) -> list[dict]:
    """Build cross-reference between Lighter and EdgeX symbols.

    EdgeX contract names are like "BTCUSD", Lighter symbols are "BTC".
    Match by checking if EdgeX contract_name starts with Lighter symbol.
    """
    pairs = []
    for lm in lighter_markets:
        l_sym = lm.get("symbol", "").upper()
        if target_symbols and l_sym not in target_symbols:
            continue
        # EdgeX contract: e.g. "BTCUSD" → base is "BTC"
        for ec in edgex_contracts:
            e_name = ec.get("contractName", "").upper()
            if e_name == f"{l_sym}USD" or e_name.startswith(l_sym):
                pairs.append({
                    "symbol": l_sym,
                    "lighter_market_id": lm["market_id"],
                    "edgex_contract_id": ec["contractId"],
                })
                break
    return pairs


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

async def _main() -> None:
    parser = argparse.ArgumentParser(description="Cross-exchange scanner integration test")
    parser.add_argument("--min-apr", type=float, default=5.0, help="Min net APR threshold (default: 5.0%%)")
    parser.add_argument("--max-spread", type=float, default=0.15, help="Max spread %% (default: 0.15%%)")
    parser.add_argument("--top", type=int, default=20, help="Show top N signals (default: 20)")
    parser.add_argument("--symbols", nargs="*", help="Filter symbols (e.g. --symbols BTC ETH SOL)")
    args = parser.parse_args()

    target = [s.upper() for s in args.symbols] if args.symbols else None

    print("=" * 70)
    print("  Cross-Exchange Scanner — Public Data Signal Test")
    print("=" * 70)

    async with aiohttp.ClientSession() as session:

        # --- Phase 1: Fetch metadata from both exchanges ---
        print("\n[1/3] Fetching market metadata...")
        l_markets, e_contracts = await asyncio.gather(
            fetch_lighter_markets(session),
            fetch_edgex_metadata(),
        )
        print(f"      Lighter: {len(l_markets)} markets")
        print(f"      EdgeX:   {len(e_contracts)} contracts")

        # Build cross-reference
        pairs = build_symbol_map(l_markets, e_contracts, target)
        print(f"      Matched: {len(pairs)} common symbols")
        if target:
            for p in pairs:
                print(f"        {p['symbol']}: Lighter #{p['lighter_market_id']} ↔ EdgeX {p['edgex_contract_id']}")

        if not pairs:
            print("\n  No common symbols found. Exiting.")
            return

        # --- Phase 2: Fetch rates and order books ---
        print(f"\n[2/3] Fetching funding rates + order books for {len(pairs)} symbols...")

        l_funding, e_tickers = await asyncio.gather(
            fetch_lighter_funding_rates(session),
            fetch_edgex_tickers([p["edgex_contract_id"] for p in pairs]),
        )
        print(f"      Lighter funding rates: {len(l_funding)} loaded")
        print(f"      EdgeX tickers:         {len(e_tickers)} loaded")

        # Bulk fetch order books for matched symbols
        l_mids = [p["lighter_market_id"] for p in pairs]
        e_cids = [p["edgex_contract_id"] for p in pairs]

        l_ob_tasks = [fetch_lighter_order_book(session, mid) for mid in l_mids]
        e_ob_tasks = fetch_edgex_order_books(e_cids)

        l_ob_results, e_ob_results = await asyncio.gather(
            asyncio.gather(*l_ob_tasks),
            e_ob_tasks,
        )

        l_ob = {mid: result for mid, result in zip(l_mids, l_ob_results)}

        # --- Phase 3: Compute signals ---
        print(f"\n[3/3] Computing signals (min_apr={args.min_apr}%, max_spread={args.max_spread}%)...")

        signals = compute_signals(
            pairs,
            l_funding,
            l_ob,
            e_tickers,
            e_ob_results,
            min_net_apr=args.min_apr,
            max_spread_pct=args.max_spread,
        )

        # --- Display ---
        if not signals:
            print("\n  No signals found matching the criteria.")
            return

        print(f"\n  {'Rank':<5} {'Symbol':<8} {'Net APR':>8} {'EdgeX APR':>10} {'LTR APR':>10} {'Spread':>8} {'Direction'}")
        print(f"  {'-'*5} {'-'*8} {'-'*8} {'-'*10} {'-'*10} {'-'*8} {'-'*35}")

        for i, s in enumerate(signals[:args.top], 1):
            print(
                f"  {i:<5} {s.symbol:<8} {s.net_apr:>7.2f}% "
                f"{s.edgex_apr:>9.2f}% {s.lighter_apr:>9.2f}% "
                f"{s.spread_pct:>7.3f}%  "
                f"{s.direction}"
            )

        print(f"\n  --- {len(signals)} signals total (showing top {min(args.top, len(signals))}) ---")

        # Detail for top 3
        if signals:
            print("\n  Top signal details:")
            print(f"  {'-'*65}")
            for s in signals[:3]:
                print(f"\n  {s.symbol}:")
                print(f"    Direction:  {s.direction}")
                print(f"    EdgeX APR:  {s.edgex_apr:.4f}%  (rate={s.edgex_rate:.8f})")
                print(f"    Lighter APR:{s.lighter_apr:.4f}%  (rate={s.lighter_rate:.8f})")
                print(f"    Net APR:    {s.net_apr:.4f}%")
                print(f"    Spread:     {s.spread_pct:.4f}%  "
                      f"(EdgeX {s.edgex_bid}/{s.edgex_ask}, Lighter {s.lighter_bid}/{s.lighter_ask})")


if __name__ == "__main__":
    asyncio.run(_main())
