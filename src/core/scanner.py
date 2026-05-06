from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from itertools import combinations
from typing import Optional

from src.core.models import ExchangeLeg, Opportunity
from src.exchanges.base import ExchangeAdapter

logger = logging.getLogger(__name__)


@dataclass
class ScanConfig:
    min_volume_usd: float = 250_000_000
    max_spread_pct: float = 0.15
    min_net_apr_threshold: float = 5.0
    symbols: list[str] = field(default_factory=list)


async def scan_all(
    adapters: dict[str, ExchangeAdapter],
    config: ScanConfig,
) -> list[Opportunity]:
    """Scan all symbols across all exchange pairs, return opportunities sorted by net APR."""

    async def _scan_one(symbol: str) -> list[Opportunity]:
        # Fetch data from all exchanges concurrently
        adapter_ids = list(adapters.keys())
        tasks = [_fetch_exchange_data(adapters[eid], symbol, eid) for eid in adapter_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect successful results: (exchange_id, funding_rate, best_bid_ask)
        entries: list[tuple[str, ExchangeLeg]] = []
        for eid, result in zip(adapter_ids, results):
            if isinstance(result, Exception):
                logger.warning("Scan %s on %s failed: %s", symbol, eid, result)
                continue
            if result is not None:
                entries.append(result)

        # Compare all pairs
        opportunities: list[Opportunity] = []
        for (eid_a, leg_a), (eid_b, leg_b) in combinations(entries, 2):
            # Spread between exchange mid prices
            mid_a = (leg_a.bid + leg_a.ask) / 2
            mid_b = (leg_b.bid + leg_b.ask) / 2
            if mid_a <= 0 or mid_b <= 0:
                continue
            spread_pct = abs(mid_a - mid_b) / min(mid_a, mid_b) * 100

            if spread_pct > config.max_spread_pct:
                continue

            # Net APR
            net_apr = abs(leg_a.apr - leg_b.apr)
            if net_apr < config.min_net_apr_threshold:
                continue

            # Assign long/short: higher APR = long (receive funding), lower APR = short (pay funding)
            if leg_a.apr >= leg_b.apr:
                long_leg = ExchangeLeg(
                    exchange_id=eid_a, side="long",
                    rate=leg_a.rate, apr=leg_a.apr, bid=leg_a.bid, ask=leg_a.ask,
                )
                short_leg = ExchangeLeg(
                    exchange_id=eid_b, side="short",
                    rate=leg_b.rate, apr=leg_b.apr, bid=leg_b.bid, ask=leg_b.ask,
                )
            else:
                long_leg = ExchangeLeg(
                    exchange_id=eid_b, side="long",
                    rate=leg_b.rate, apr=leg_b.apr, bid=leg_b.bid, ask=leg_b.ask,
                )
                short_leg = ExchangeLeg(
                    exchange_id=eid_a, side="short",
                    rate=leg_a.rate, apr=leg_a.apr, bid=leg_a.bid, ask=leg_a.ask,
                )

            opportunities.append(Opportunity(
                symbol=symbol,
                long_leg=long_leg,
                short_leg=short_leg,
                net_apr=net_apr,
                spread_pct=spread_pct,
            ))

        return opportunities

    sem = asyncio.Semaphore(4)  # limit concurrent symbol scans to avoid overloading SDKs

    async def _scan_one_sem(symbol: str) -> list[Opportunity]:
        async with sem:
            return await _scan_one(symbol)

    tasks = [_scan_one_sem(s) for s in config.symbols]
    results = await asyncio.gather(*tasks)

    candidates: list[Opportunity] = []
    for r in results:
        candidates.extend(r)

    candidates.sort(key=lambda o: o.net_apr, reverse=True)
    return candidates


# ---------------------------------------------------------------------------
# Per-exchange fetcher (generic)
# ---------------------------------------------------------------------------

async def _fetch_exchange_data(
    adapter: ExchangeAdapter,
    symbol: str,
    exchange_id: str,
) -> Optional[tuple[str, ExchangeLeg]]:
    """Fetch funding rate + best bid/ask for a symbol from one exchange.

    Returns (exchange_id, ExchangeLeg) or None on failure.
    """
    try:
        md = await adapter.get_market_details(symbol)
    except Exception as e:
        logger.warning("%s market_details failed for %s: %s", exchange_id, symbol, e)
        return None

    try:
        fr, bba = await asyncio.gather(
            adapter.get_funding_rate(md.market_id),
            adapter.get_best_bid_ask(md.market_id),
        )
    except Exception as e:
        logger.warning("%s fetch failed for %s: %s", exchange_id, symbol, e)
        return None

    if fr is None or bba is None:
        return None

    leg = ExchangeLeg(
        exchange_id=exchange_id,
        rate=fr.rate,
        apr=fr.apr,
        bid=bba.bid,
        ask=bba.ask,
    )
    return (exchange_id, leg)
