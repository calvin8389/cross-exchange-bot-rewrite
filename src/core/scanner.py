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
    estimated_taker_fee_bps: float = 4.0
    estimated_slippage_bps: float = 2.0
    estimated_impact_bps: float = 1.0
    # Per-symbol exchange exclusions: {"LDO": ["edgex"]} skips LDO on EdgeX
    symbol_exchange_excludes: dict[str, list[str]] = field(default_factory=dict)


def _estimate_cost_apr(config: ScanConfig) -> float:
    hold_hours = 8.0
    # 4 = 2 legs (long + short) × 2 trades (open + close)
    round_trip_cost_pct = (
        (config.estimated_taker_fee_bps + config.estimated_slippage_bps + config.estimated_impact_bps)
        * 4
        / 10_000
        * 100
    )
    annualization = (24 * 365) / hold_hours
    return round_trip_cost_pct * annualization


async def scan_all(
    adapters: dict[str, ExchangeAdapter],
    config: ScanConfig,
) -> list[Opportunity]:
    """Scan all symbols across all exchange pairs, return opportunities sorted by net APR."""

    async def _scan_one(symbol: str) -> list[Opportunity]:
        # Fetch data from all exchanges concurrently, skipping excluded pairs
        excluded_exchanges = set(config.symbol_exchange_excludes.get(symbol, []))
        adapter_ids = [eid for eid in adapters.keys() if eid not in excluded_exchanges]
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

            gross_apr = abs(leg_a.apr - leg_b.apr)
            estimated_cost_apr = _estimate_cost_apr(config)
            net_apr = gross_apr - estimated_cost_apr
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
                gross_apr=gross_apr,
                estimated_cost_apr=estimated_cost_apr,
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
    if md.market_id in ("", None) or md.price_tick <= 0 or md.size_step <= 0:
        logger.warning("%s market_details invalid for %s", exchange_id, symbol)
        return None
    if bba.bid <= 0 or bba.ask <= 0 or bba.ask < bba.bid:
        logger.warning("%s best_bid_ask invalid for %s", exchange_id, symbol)
        return None

    leg = ExchangeLeg(
        exchange_id=exchange_id,
        rate=fr.rate,
        apr=fr.apr,
        bid=bba.bid,
        ask=bba.ask,
    )
    return (exchange_id, leg)
