from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.scanner import ScanConfig, scan_all


def _adapter(*, exchange_id: str, apr: float):
    adapter = MagicMock(exchange_id=exchange_id)
    adapter.get_market_details = AsyncMock(return_value=MagicMock(
        market_id=f"{exchange_id}_BTC",
        price_tick=0.1,
        size_step=0.001,
        min_order_size=0.001,
        min_notional=10.0,
    ))
    adapter.get_funding_rate = AsyncMock(return_value=MagicMock(rate=0.0001, apr=apr))
    adapter.get_best_bid_ask = AsyncMock(return_value=MagicMock(bid=100.0, ask=100.1))
    return adapter


@pytest.mark.asyncio
async def test_scan_filters_out_candidates_after_cost_adjustment():
    adapters = {
        "ex_a": _adapter(exchange_id="ex_a", apr=12.0),
        "ex_b": _adapter(exchange_id="ex_b", apr=8.0),
    }
    config = ScanConfig(
        symbols=["BTC"],
        min_net_apr_threshold=1.0,
        hold_duration_hours=8.0,
        estimated_taker_fee_bps=10.0,
        estimated_slippage_bps=10.0,
        estimated_impact_bps=10.0,
    )

    results = await scan_all(adapters, config)

    assert results == []


@pytest.mark.asyncio
async def test_scan_records_gross_and_cost_apr():
    adapters = {
        "ex_a": _adapter(exchange_id="ex_a", apr=20.0),
        "ex_b": _adapter(exchange_id="ex_b", apr=5.0),
    }
    config = ScanConfig(
        symbols=["BTC"],
        min_net_apr_threshold=0.1,
        hold_duration_hours=24.0 * 365,
        estimated_taker_fee_bps=1.0,
        estimated_slippage_bps=1.0,
        estimated_impact_bps=1.0,
    )

    results = await scan_all(adapters, config)

    assert len(results) == 1
    assert results[0].gross_apr == pytest.approx(15.0)
    assert results[0].estimated_cost_apr > 0
    assert results[0].net_apr < results[0].gross_apr
