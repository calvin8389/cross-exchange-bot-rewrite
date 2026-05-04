"""Position open / close execution engine.

Concurrent dual-exchange order placement with rollback on partial failure.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from src.core.models import Opportunity, PositionState
from src.core.sizing import calculate_position_size, cross_price, unify_size_step
from src.db.store import Event, Store
from src.exchanges.base import ExchangeAdapter

logger = logging.getLogger(__name__)


@dataclass
class ExecConfig:
    leverage: int = 3
    cross_pct: float = 3.0
    safety_factor: float = 0.95
    confirm_timeout_seconds: float = 30.0
    confirm_poll_interval: float = 2.0


async def open_position(
    opp: Opportunity,
    edgex: ExchangeAdapter,
    lighter: ExchangeAdapter,
    store: Store,
    config: ExecConfig,
) -> PositionState:
    """Open a delta-neutral position across both exchanges.

    1. Fetch balances → compute size
    2. Place orders concurrently (asyncio.gather)
    3. Confirm both legs established
    4. Rollback if one leg failed
    """

    # ---- 1. Balances & sizing ------------------------------------------
    edgex_bal, lighter_bal = await asyncio.gather(
        edgex.get_balance(), lighter.get_balance()
    )
    mid = (opp.edgex_bid + opp.edgex_ask + opp.lighter_bid + opp.lighter_ask) / 4.0
    size_base = calculate_position_size(
        edgex_bal.available, lighter_bal.available,
        config.leverage, mid, config.safety_factor,
    )

    # Unify across exchanges (use placeholder steps — adapters will provide real values)
    size_base = unify_size_step(size_base, edgex_step=0.01, lighter_step=0.01)

    # Determine which exchange takes which side
    if opp.direction == "long_edgex_short_lighter":
        edgex_side, lighter_side = "buy", "sell"
    else:
        edgex_side, lighter_side = "sell", "buy"

    # Prices (aggressive to ensure fill)
    edgex_price = cross_price(edgex_side, opp.edgex_bid, opp.edgex_ask, tick=0.01, cross_pct=config.cross_pct)
    lighter_price = cross_price(lighter_side, opp.lighter_bid, opp.lighter_ask, tick=0.01, cross_pct=config.cross_pct)

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # ---- 2. Insert cycle record ----------------------------------------
    from src.util.time import utc_now_iso

    await store.conn.execute(
        """INSERT INTO cycles(symbol, state, direction, leverage, created_at, updated_at)
           VALUES(?,?,?,?,?,?)""",
        (opp.symbol, "OPENING", opp.direction, config.leverage, utc_now_iso(), utc_now_iso()),
    )
    await store.conn.commit()
    row = await store.conn.execute("SELECT last_insert_rowid()")
    cycle_id = (await row.fetchone())[0]

    await store.append_event(Event(
        level="info", event_type="OPENING_START", cycle_id=cycle_id,
        data={"symbol": opp.symbol, "direction": opp.direction, "size": size_base},
    ))

    # ---- 3. Place orders concurrently ----------------------------------
    edgex_order_id: Optional[str] = None
    lighter_order_id: Optional[str] = None

    async def _place_edgex():
        nonlocal edgex_order_id
        edgex_order_id = await edgex.place_order(
            symbol=opp.symbol,
            side=edgex_side,
            size_base=size_base,
            price=edgex_price,
        )

    async def _place_lighter():
        nonlocal lighter_order_id
        market_id = _symbol_to_lighter_market(opp.symbol)
        lighter_order_id = await lighter.place_order(
            symbol=opp.symbol,
            side=lighter_side,
            size_base=size_base,
            price=lighter_price,
            market_id=market_id,
        )

    results = await asyncio.gather(
        _place_edgex(), _place_lighter(), return_exceptions=True,
    )

    edgex_err = results[0] if isinstance(results[0], Exception) else None
    lighter_err = results[1] if isinstance(results[1], Exception) else None

    # ---- 4. Rollback on partial failure --------------------------------
    if edgex_err and not lighter_err:
        logger.error("EdgeX leg failed, rolling back Lighter: %s", edgex_err)
        await lighter.close_position(
            symbol=opp.symbol, side="buy" if lighter_side == "sell" else "sell",
            size_base=size_base, price=lighter_price,
            market_id=_symbol_to_lighter_market(opp.symbol),
        )
        await _fail_cycle(store, cycle_id, "EdgeX leg failed")
        raise RuntimeError(f"EdgeX leg failed, Lighter rolled back: {edgex_err}")

    if lighter_err and not edgex_err:
        logger.error("Lighter leg failed, rolling back EdgeX: %s", lighter_err)
        await edgex.close_position(
            symbol=opp.symbol, side="buy" if edgex_side == "sell" else "sell",
            size_base=size_base, price=edgex_price,
        )
        await _fail_cycle(store, cycle_id, "Lighter leg failed")
        raise RuntimeError(f"Lighter leg failed, EdgeX rolled back: {lighter_err}")

    if edgex_err and lighter_err:
        await _fail_cycle(store, cycle_id, "Both legs failed")
        raise RuntimeError(f"Both legs failed: EdgeX={edgex_err}, Lighter={lighter_err}")

    # ---- 5. Confirm positions exist ------------------------------------
    confirmed = await _confirm_positions(edgex, lighter, opp.symbol, config)
    if not confirmed:
        # Emergency: close both sides
        logger.error("Position confirmation failed, attempting emergency close")
        await asyncio.gather(
            edgex.close_position(symbol=opp.symbol, side="buy" if edgex_side == "sell" else "sell", size_base=size_base, price=edgex_price),
            lighter.close_position(symbol=opp.symbol, side="buy" if lighter_side == "sell" else "sell", size_base=size_base, price=lighter_price, market_id=_symbol_to_lighter_market(opp.symbol)),
            return_exceptions=True,
        )
        await _fail_cycle(store, cycle_id, "Confirmation failed")
        raise RuntimeError("Position confirmation failed, both legs closed")

    # ---- 6. Insert position record -------------------------------------
    await store.conn.execute(
        """INSERT INTO positions(cycle_id, symbol, is_active,
           edgex_side, edgex_size, edgex_entry_price,
           lighter_market_id, lighter_side, lighter_size, lighter_entry_price,
           opened_at, updated_at)
           VALUES(?,?,1,?,?,?,?,?,?,?,?,?)""",
        (cycle_id, opp.symbol,
         edgex_side, size_base, edgex_price,
         _symbol_to_lighter_market(opp.symbol), lighter_side, size_base, lighter_price,
         utc_now_iso(), utc_now_iso()),
    )
    await store.conn.commit()

    await store.conn.execute(
        "UPDATE cycles SET state='HOLDING', opened_at=?, "
        "edgex_size=?, lighter_size=?, edgex_entry_price=?, lighter_entry_price=?, "
        "updated_at=? WHERE id=?",
        (utc_now_iso(), size_base, size_base, edgex_price, lighter_price, utc_now_iso(), cycle_id),
    )
    await store.conn.commit()

    await store.append_event(Event(
        level="info", event_type="OPENING_DONE", cycle_id=cycle_id,
        data={"symbol": opp.symbol, "size": size_base, "edgex_side": edgex_side, "lighter_side": lighter_side},
    ))

    return PositionState(
        symbol=opp.symbol, cycle_id=cycle_id,
        edgex_size=size_base, lighter_size=size_base,
        edgex_entry=edgex_price, lighter_entry=lighter_price,
        opened_at=now_iso,
    )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

async def _confirm_positions(
    edgex: ExchangeAdapter,
    lighter: ExchangeAdapter,
    symbol: str,
    config: ExecConfig,
) -> bool:
    """Poll until both exchanges show non-zero positions or timeout."""
    deadline = time.time() + config.confirm_timeout_seconds
    while time.time() < deadline:
        try:
            e_pos, l_pos = await asyncio.gather(
                edgex.get_open_positions(),
                lighter.get_open_positions(),
            )
        except Exception:
            await asyncio.sleep(config.confirm_poll_interval)
            continue

        e_match = any(p.symbol.upper() == symbol.upper() and abs(p.size) > 1e-8 for p in e_pos)
        l_match = any(p.symbol.upper() == symbol.upper() and abs(p.size) > 1e-8 for p in l_pos)

        if e_match and l_match:
            return True

        await asyncio.sleep(config.confirm_poll_interval)

    return False


async def _fail_cycle(store: Store, cycle_id: int, reason: str) -> None:
    from src.util.time import utc_now_iso

    await store.conn.execute(
        "UPDATE cycles SET state='ERROR', updated_at=? WHERE id=?",
        (utc_now_iso(), cycle_id),
    )
    await store.conn.commit()
    await store.append_event(Event(
        level="error", event_type="OPENING_ROLLBACK", cycle_id=cycle_id,
        data={"reason": reason},
    ))


def _symbol_to_lighter_market(symbol: str) -> int:
    mapping: dict[str, int] = {"BTC": 0, "ETH": 0, "SOL": 2, "DOGE": 3, "SUI": 5}
    return mapping.get(symbol.upper(), 0)
