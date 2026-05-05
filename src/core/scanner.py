from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

from src.core.models import Opportunity
from src.exchanges.base import ExchangeAdapter

logger = logging.getLogger(__name__)


@dataclass
class ScanConfig:
    min_volume_usd: float = 250_000_000
    max_spread_pct: float = 0.15
    min_net_apr_threshold: float = 5.0
    symbols: list[str] = field(default_factory=list)


async def scan_all(
    lighter: ExchangeAdapter,
    edgex: ExchangeAdapter,
    config: ScanConfig,
) -> list[Opportunity]:
    """Scan all symbols, return filtered opportunities sorted by net APR desc."""

    async def _scan_one(symbol: str) -> Optional[Opportunity]:
        try:
            # Fetch data from both exchanges concurrently
            (l_funding, l_bba), (e_funding, e_bba) = await asyncio.gather(
                _fetch_lighter(lighter, symbol),
                _fetch_edgex(edgex, symbol),
            )
        except Exception as e:
            logger.warning("Scan %s failed: %s", symbol, e)
            return None

        if l_funding is None or e_funding is None:
            return None
        if l_bba is None or e_bba is None:
            return None

        # Spread: difference between exchange mid prices
        l_mid = (l_bba.bid + l_bba.ask) / 2
        e_mid = (e_bba.bid + e_bba.ask) / 2
        if l_mid <= 0 or e_mid <= 0:
            return None
        spread_pct = abs(l_mid - e_mid) / min(l_mid, e_mid) * 100

        if spread_pct > config.max_spread_pct:
            return None

        # Volume (placeholder — adapters will provide this in M4)
        volume = 0.0
        if volume < config.min_volume_usd:
            pass  # volume check is soft for now; adapters don't expose it yet

        # Net APR: determine direction
        edgex_apr = e_funding.apr
        lighter_apr = l_funding.apr
        net_apr = abs(edgex_apr - lighter_apr)

        if net_apr < config.min_net_apr_threshold:
            return None

        if edgex_apr > lighter_apr:
            direction = "long_edgex_short_lighter"
        else:
            direction = "short_edgex_long_lighter"

        return Opportunity(
            symbol=symbol,
            edgex_rate=e_funding.rate,
            lighter_rate=l_funding.rate,
            edgex_apr=edgex_apr,
            lighter_apr=lighter_apr,
            net_apr=net_apr,
            volume=volume,
            spread=spread_pct,
            direction=direction,
            edgex_bid=e_bba.bid,
            edgex_ask=e_bba.ask,
            lighter_bid=l_bba.bid,
            lighter_ask=l_bba.ask,
        )

    tasks = [_scan_one(s) for s in config.symbols]
    results = await asyncio.gather(*tasks)

    candidates = [r for r in results if r is not None]
    candidates.sort(key=lambda o: o.net_apr, reverse=True)
    return candidates


# ---------------------------------------------------------------------------
# Per-exchange fetchers (internal)
# ---------------------------------------------------------------------------

async def _fetch_lighter(adapter: ExchangeAdapter, symbol: str):
    """Fetch Lighter funding rate + best bid/ask for a symbol.

    Returns (FundingRate | None, BestBidAsk | None).
    """
    try:
        md = await adapter.get_market_details(symbol)
    except Exception as e:
        logger.warning("Lighter market_details failed for %s: %s", symbol, e)
        return None, None

    fr, bba = await asyncio.gather(
        adapter.get_funding_rate(md.market_id),
        adapter.get_best_bid_ask(md.market_id),
    )
    return fr, bba


async def _fetch_edgex(adapter: ExchangeAdapter, symbol: str):
    """Fetch EdgeX funding rate + best bid/ask for a symbol."""
    try:
        md = await adapter.get_market_details(symbol)
    except Exception as e:
        logger.warning("EdgeX market_details failed for %s: %s", symbol, e)
        return None, None

    fr, bba = await asyncio.gather(
        adapter.get_funding_rate(md.market_id),
        adapter.get_best_bid_ask(md.market_id),
    )
    return fr, bba
