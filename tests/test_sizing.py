#!/usr/bin/env python3
"""Phase 1A: Precision math unit tests for src/core/sizing.py.

Uses real tick/step values from Phase 0 fixtures (no network, no cost).
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.sizing import (
    calculate_position_size,
    cross_price,
    round_price_to_tick,
    round_size_to_step,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load_contracts(exchange: str) -> dict:
    filename = {
        "lighter": "lighter_markets.json",
        "edgex": "edgex_contracts.json",
        "hl": "hl_markets.json",
        "grvt": "grvt_markets.json",
    }[exchange]
    with open(FIXTURES / filename) as f:
        return json.load(f)["contracts"]


# ---------------------------------------------------------------------------
# round_price_to_tick
# ---------------------------------------------------------------------------

REAL_TICK_CASES = [
    # (tick, side, input_price, expected)
    (1.0,    "buy",  81337.5,  81338.0),   # BTC on HL (ceil)
    (1.0,    "sell", 81337.5,  81337.0),   # BTC on HL (floor)
    (0.1,    "buy",  81337.51, 81337.6),   # BTC on Lighter/GRVT/ETH on HL
    (0.1,    "sell", 81337.59, 81337.5),
    (0.01,   "buy",  2409.351, 2409.36),   # ETH on Lighter/BCH on HL
    (0.01,   "sell", 2409.359, 2409.35),
    (0.0001, "buy",  10.14903, 10.1491),   # LINK on HL
    (0.0001, "sell", 10.14909, 10.1490),
    (0.00001,"buy",  1.027655, 1.02766),   # SUI on HL
    (0.00001,"sell", 1.027659, 1.02765),
    # Edge cases
    (0.000001, "buy",  0.000009, 0.000009), # tiny tick: ceil(9e-6 / 1e-6) = ceil(9) = 9 → 9e-6
    (0.000001, "sell", 0.000009, 0.000009), # floor(9e-6 / 1e-6) = floor(9) = 9 → 9e-6
]


@pytest.mark.parametrize("tick,side,price,expected", REAL_TICK_CASES)
def test_round_price_to_tick(tick, side, price, expected):
    result = round_price_to_tick(price, tick, side)
    assert result == expected, f"{price} @ tick={tick} {side}: expected {expected}, got {result}"
    # The result must be an integer multiple of tick (allow float precision)
    ratio = round(result / tick, 10)
    assert ratio == round(ratio), f"{result} / {tick} = {ratio} (not an integer)"


def test_round_price_to_tick_buy_ceil():
    """BUY always rounds up."""
    for tick in [1.0, 0.1, 0.01, 0.0001, 0.00001]:
        for offset in [0.0, 0.00001, 0.001, 0.1, 0.5, 0.99]:
            price = 100.0 + offset * tick
            result = round_price_to_tick(price, tick, "buy")
            assert result >= price, f"buy {price} @ tick={tick}: {result} < {price}"


def test_round_price_to_tick_sell_floor():
    """SELL always rounds down."""
    for tick in [1.0, 0.1, 0.01, 0.0001, 0.00001]:
        for offset in [0.0, 0.00001, 0.001, 0.1, 0.5, 0.99]:
            price = 100.0 + offset * tick
            result = round_price_to_tick(price, tick, "sell")
            assert result <= price, f"sell {price} @ tick={tick}: {result} > {price}"


def test_round_price_to_tick_zero_tick():
    """Zero or negative tick should pass through."""
    assert round_price_to_tick(100.5, 0, "buy") == 100.5
    assert round_price_to_tick(100.5, -1, "buy") == 100.5


# ---------------------------------------------------------------------------
# round_size_to_step
# ---------------------------------------------------------------------------

REAL_STEP_CASES = [
    # (step, input_size, expected)
    (1.0,    1500.4, 1500.0),
    (0.1,    194.95, 194.9),
    (0.01,   0.432,  0.43),
    (0.001,  0.0062, 0.006),
    (0.0001, 0.2079, 0.2079),
    (0.00001,0.00122,0.00122),
    (100.0,  1499.0, 1400.0),  # large step
    (100.0,  1501.0, 1500.0),
]


@pytest.mark.parametrize("step,size,expected", REAL_STEP_CASES)
def test_round_size_to_step(step, size, expected):
    result = round_size_to_step(size, step)
    assert result == expected, f"{size} @ step={step}: expected {expected}, got {result}"
    # Must be an integer multiple of step (allow float precision)
    ratio = round(result / step, 10)
    assert ratio == round(ratio), f"{result} / {step} = {ratio} (not integer)"
    # Conservative: result <= size (floor)
    assert result <= size, f"{result} > {size} (should floor)"


def test_round_size_to_step_zero_step():
    assert round_size_to_step(100.0, 0) == 100.0


# ---------------------------------------------------------------------------
# cross_price
# ---------------------------------------------------------------------------

CROSS_PRICE_CASES = [
    # (side, bid, ask, tick, cross_pct, expected)
    ("buy",  81335.0, 81337.0, 1.0, 3.0, None),  # dynamic check below
    ("sell", 81335.0, 81337.0, 1.0, 3.0, None),
    ("buy",  2409.1, 2409.2, 0.1, 3.0, None),
    ("sell", 2409.1, 2409.2, 0.1, 3.0, None),
    ("buy",  10.14, 10.15, 0.0001, 3.0, None),
    ("sell", 10.14, 10.15, 0.0001, 3.0, None),
]


@pytest.mark.parametrize("side,bid,ask,tick,cross_pct,expected", CROSS_PRICE_CASES)
def test_cross_price_tick_alignment(side, bid, ask, tick, cross_pct, expected):
    result = cross_price(side, bid, ask, tick, cross_pct)
    # Result must be an integer multiple of tick
    remainder = (round(result, 10) / round(tick, 10)) % 1
    # Use isclose for float comparison
    assert math.isclose(remainder % 1, 0.0, abs_tol=1e-10) or math.isclose(remainder % 1, 1.0, abs_tol=1e-10), \
        f"{side} px={result} / tick={tick} = {result/tick} (remainder={remainder})"


def test_cross_price_buy_above_mid():
    """Buy cross_price should produce a price above mid."""
    result = cross_price("buy", 100.0, 102.0, 0.1, 3.0)
    mid = (100.0 + 102.0) / 2.0
    assert result > mid, f"buy cross price {result} <= mid {mid}"


def test_cross_price_sell_below_mid():
    """Sell cross_price should produce a price below mid."""
    result = cross_price("sell", 100.0, 102.0, 0.1, 3.0)
    mid = (100.0 + 102.0) / 2.0
    assert result < mid, f"sell cross price {result} >= mid {mid}"


def test_cross_price_higher_cross_pct():
    """Higher cross_pct gives more aggressive prices."""
    r1 = cross_price("buy", 100.0, 102.0, 0.1, 1.0)
    r2 = cross_price("buy", 100.0, 102.0, 0.1, 5.0)
    assert r2 > r1, f"5% cross_pct ({r2}) should be above 1% ({r1})"

    r1 = cross_price("sell", 100.0, 102.0, 0.1, 1.0)
    r2 = cross_price("sell", 100.0, 102.0, 0.1, 5.0)
    assert r2 < r1, f"5% cross_pct ({r2}) should be below 1% ({r1})"


# ---------------------------------------------------------------------------
# Hyperliquid float_to_wire compatibility
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("symbol", ["BTC", "ETH", "LINK", "OP", "BCH", "SUI"])
def test_cross_price_compatible_with_hl_float_to_wire(symbol):
    """Cross price output must survive HL's float_to_wire without rounding error."""
    from hyperliquid.utils.signing import float_to_wire

    contracts = _load_contracts("hl")
    c = contracts.get(symbol)
    if not c:
        pytest.skip(f"{symbol} not in HL fixture")

    tick = c["price_tick"]
    # Use fake BBA that's close to realistic
    mid = 1000.0 if tick >= 0.1 else 10.0 if tick >= 0.01 else 1.0
    bid, ask = mid, mid * 1.0001

    for cross_pct in [3.0, 4.5, 6.0]:
        px = cross_price("buy", bid, ask, tick, cross_pct)
        try:
            float_to_wire(px)
        except ValueError as e:
            pytest.fail(
                f"{symbol} tick={tick} cross_pct={cross_pct}%: "
                f"cross_price={px} rejected by HL float_to_wire: {e}"
            )


# ---------------------------------------------------------------------------
# calculate_position_size
# ---------------------------------------------------------------------------

def test_calculate_position_size_basic():
    result = calculate_position_size(1000.0, 1000.0, 3, 100.0)
    # max_notional = min(1000, 1000) * 3 * 0.95 = 2850
    # size = 2850 / 100 = 28.5
    assert result > 0
    assert 25 < result < 35


def test_calculate_position_size_zero_mid():
    with pytest.raises(ValueError, match="Invalid mid_price"):
        calculate_position_size(1000.0, 1000.0, 3, 0)


def test_calculate_position_size_zero_leverage():
    """leverage=0 gives zero notional → size=0 (no error)."""
    r = calculate_position_size(1000.0, 1000.0, 0, 100.0)
    assert r == 0.0


def test_calculate_position_size_imbalanced():
    """When one exchange has less balance, size is limited by the smaller one."""
    r = calculate_position_size(100.0, 10000.0, 3, 100.0)
    # max_notional = min(100, 10000) * 3 * 0.95 = 285
    # size = 285 / 100 = 2.85
    assert r == pytest.approx(2.85, rel=1e-6)
