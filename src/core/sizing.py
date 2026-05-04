"""Position sizing and tick/step unification.

Uses ``Decimal`` arithmetic to avoid floating-point precision errors
that can cause exchange order rejections.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_UP, Decimal


def _to_decimal(value: float) -> Decimal:
    return Decimal(str(value))


def round_price_to_tick(price: float, tick: float, side: str) -> float:
    """Round price to tick boundary.

    BUY  → ceil  (avoid limit too low to fill)
    SELL → floor (avoid limit too high to fill)
    """
    if not tick or tick <= 0:
        return price
    d_price = _to_decimal(price)
    d_tick = _to_decimal(tick)
    rounding = ROUND_CEILING if side == "buy" else ROUND_FLOOR
    return float((d_price / d_tick).quantize(Decimal("1"), rounding=rounding) * d_tick)


def round_size_to_step(size: float, step: float) -> float:
    """Floor size to step boundary — conservative, avoids oversizing."""
    if not step or step <= 0:
        return size
    d_size = _to_decimal(size)
    d_step = _to_decimal(step)
    return float((d_size / d_step).quantize(Decimal("1"), rounding=ROUND_DOWN) * d_step)


def unify_size_step(size: float, edgex_step: float, lighter_step: float) -> float:
    """Floor to the coarser step so both exchange sizes are identical."""
    return round_size_to_step(size, max(edgex_step, lighter_step))


def unify_price_tick(price: float, edgex_tick: float, lighter_tick: float, side: str) -> float:
    """Round to the coarser tick with direction-aware rounding."""
    return round_price_to_tick(price, max(edgex_tick, lighter_tick), side)


def cross_price(
    side: str,
    ref_bid: float,
    ref_ask: float,
    tick: float,
    cross_pct: float = 3.0,
) -> float:
    """Aggressive limit price: mid × (1 ± cross_pct/100)."""
    mid = (ref_bid + ref_ask) / 2.0 if ref_bid and ref_ask else (ref_bid or ref_ask)
    if side == "buy":
        return round_price_to_tick(mid * (1.0 + cross_pct / 100.0), tick, "buy")
    else:
        return round_price_to_tick(mid * (1.0 - cross_pct / 100.0), tick, "sell")


def calculate_position_size(
    available_edgex: float,
    available_lighter: float,
    leverage: int,
    mid_price: float,
    safety_factor: float = 0.95,
) -> float:
    """Maximum delta-neutral position size in base currency."""
    if mid_price <= 0:
        raise ValueError(f"Invalid mid_price: {mid_price}")
    max_notional = min(available_edgex, available_lighter) * leverage * safety_factor
    return max_notional / mid_price
