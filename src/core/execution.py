"""Position open / close execution engine.

Concurrent dual-exchange order placement with rollback on partial failure.
Generalised to work with any pair of exchanges via the adapters dict.
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
    notional_override: float = 0.0  # if > 0, cap position notional to this value


async def open_position(
    opp: Opportunity,
    adapters: dict[str, ExchangeAdapter],
    store: Store,
    config: ExecConfig,
) -> PositionState:
    """Open a delta-neutral position across two exchanges.

    1. Fetch balances -> compute size
    2. Place orders concurrently (asyncio.gather)
    3. Confirm both legs established
    4. Rollback if one leg failed
    """
    long_adapter = adapters[opp.long_leg.exchange_id]
    short_adapter = adapters[opp.short_leg.exchange_id]

    # ---- 1. Balances & sizing ------------------------------------------
    long_bal, short_bal = await asyncio.gather(
        long_adapter.get_balance(), short_adapter.get_balance(),
    )
    mid = (opp.long_leg.bid + opp.long_leg.ask + opp.short_leg.bid + opp.short_leg.ask) / 4.0

    # Resolve market metadata for tick/step unification
    long_md = await long_adapter.get_market_details(opp.symbol)
    short_md = await short_adapter.get_market_details(opp.symbol)
    long_market_id = long_md.market_id
    short_market_id = short_md.market_id

    # Calculate size: use balance-based sizing if both balances available,
    # otherwise fall back to tier notional
    if long_bal.available > 0 and short_bal.available > 0:
        size_base = calculate_position_size(
            long_bal.available, short_bal.available,
            config.leverage, mid, config.safety_factor,
        )
    elif config.notional_override > 0:
        size_base = config.notional_override / mid
    else:
        size_base = 0.0

    # Unify size across exchanges
    size_base = unify_size_step(size_base, long_md.size_step, short_md.size_step)

    # Apply tier-based notional cap if configured (and balances were used)
    if config.notional_override > 0 and long_bal.available > 0 and short_bal.available > 0:
        max_size = config.notional_override / mid
        size_base = min(size_base, max_size)
        size_base = unify_size_step(size_base, long_md.size_step, short_md.size_step)

    # Safety: ensure size is valid on both exchanges
    if size_base < long_md.size_step or size_base < short_md.size_step:
        raise ValueError(
            f"Position size {size_base} too small for {opp.symbol}: "
            f"min steps long={long_md.size_step} short={short_md.size_step}"
        )
    if size_base <= 0:
        raise ValueError(f"Position size rounded to 0 for {opp.symbol}")

    # Long leg = BUY, Short leg = SELL
    long_side, short_side = "buy", "sell"

    # Prices (aggressive to ensure fill)
    long_price = cross_price("buy", opp.long_leg.bid, opp.long_leg.ask, tick=long_md.price_tick, cross_pct=config.cross_pct)
    short_price = cross_price("sell", opp.short_leg.bid, opp.short_leg.ask, tick=short_md.price_tick, cross_pct=config.cross_pct)

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # ---- 2. Insert cycle record ----------------------------------------
    from src.util.time import utc_now_iso

    row = await store.conn.execute(
        """INSERT INTO cycles(symbol, state, direction, exchange_long, exchange_short, leverage, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?) RETURNING id""",
        (opp.symbol, "OPENING", opp.direction, opp.long_leg.exchange_id, opp.short_leg.exchange_id,
         config.leverage, utc_now_iso(), utc_now_iso()),
    )
    cycle_id = (await row.fetchone())[0]
    await store.conn.commit()

    await store.append_event(Event(
        level="info", event_type="OPENING_START", cycle_id=cycle_id,
        data={"symbol": opp.symbol, "direction": opp.direction, "size": size_base,
              "long_exchange": opp.long_leg.exchange_id, "short_exchange": opp.short_leg.exchange_id},
    ))

    # ---- 3. Place orders concurrently ----------------------------------
    from src.exchanges.base import OrderResult

    long_result: Optional[OrderResult] = None
    short_result: Optional[OrderResult] = None

    async def _place_long():
        nonlocal long_result
        long_result = await long_adapter.place_order(
            symbol=opp.symbol, side=long_side, size_base=size_base,
            price=long_price, market_id=long_market_id,
        )

    async def _place_short():
        nonlocal short_result
        short_result = await short_adapter.place_order(
            symbol=opp.symbol, side=short_side, size_base=size_base,
            price=short_price, market_id=short_market_id,
        )

    results = await asyncio.gather(
        _place_long(), _place_short(), return_exceptions=True,
    )

    long_err = results[0] if isinstance(results[0], Exception) else None
    short_err = results[1] if isinstance(results[1], Exception) else None

    # ---- 4. Rollback on partial failure --------------------------------
    if long_err and not short_err:
        logger.error("Long leg (%s) failed, rolling back short leg (%s): %s",
                     opp.long_leg.exchange_id, opp.short_leg.exchange_id, long_err)
        await short_adapter.close_position(
            symbol=opp.symbol, side="buy", size_base=size_base, price=short_price,
            market_id=short_market_id,
        )
        await _fail_cycle(store, cycle_id, f"Long leg ({opp.long_leg.exchange_id}) failed")
        raise RuntimeError(f"Long leg ({opp.long_leg.exchange_id}) failed, short leg rolled back: {long_err}")

    if short_err and not long_err:
        logger.error("Short leg (%s) failed, rolling back long leg (%s): %s",
                     opp.short_leg.exchange_id, opp.long_leg.exchange_id, short_err)
        await long_adapter.close_position(
            symbol=opp.symbol, side="sell", size_base=size_base, price=long_price,
        )
        await _fail_cycle(store, cycle_id, f"Short leg ({opp.short_leg.exchange_id}) failed")
        raise RuntimeError(f"Short leg ({opp.short_leg.exchange_id}) failed, long leg rolled back: {short_err}")

    if long_err and short_err:
        await _fail_cycle(store, cycle_id, "Both legs failed")
        raise RuntimeError(f"Both legs failed: long={long_err}, short={short_err}")

    # Treat None return (silent failure, no exception) as failure
    if long_result is None and not long_err:
        logger.error("Long leg (%s) returned None, rolling back", opp.long_leg.exchange_id)
        await short_adapter.close_position(
            symbol=opp.symbol, side="buy", size_base=size_base, price=short_price,
            market_id=short_market_id,
        )
        try:
            await long_adapter.close_position(
                symbol=opp.symbol, side="sell", size_base=size_base, price=long_price,
            )
        except Exception:
            pass
        await _fail_cycle(store, cycle_id, f"Long leg ({opp.long_leg.exchange_id}) returned None")
        raise RuntimeError(f"Long leg ({opp.long_leg.exchange_id}) returned None")

    if short_result is None and not short_err:
        logger.error("Short leg (%s) returned None, rolling back", opp.short_leg.exchange_id)
        await long_adapter.close_position(
            symbol=opp.symbol, side="sell", size_base=size_base, price=long_price,
        )
        try:
            await short_adapter.close_position(
                symbol=opp.symbol, side="buy", size_base=size_base, price=short_price,
                market_id=short_market_id,
            )
        except Exception:
            pass
        await _fail_cycle(store, cycle_id, f"Short leg ({opp.short_leg.exchange_id}) returned None")
        raise RuntimeError(f"Short leg ({opp.short_leg.exchange_id}) returned None")

    # ---- 5. Confirm positions exist ------------------------------------
    confirmed = await _confirm_positions(
        long_adapter, short_adapter, opp.symbol, config,
    )
    if not confirmed:
        logger.error("Position confirmation failed, attempting emergency close")
        await asyncio.gather(
            long_adapter.close_position(symbol=opp.symbol, side="sell", size_base=size_base, price=long_price),
            short_adapter.close_position(symbol=opp.symbol, side="buy", size_base=size_base, price=short_price, market_id=short_market_id),
            return_exceptions=True,
        )
        await _fail_cycle(store, cycle_id, "Confirmation failed")
        raise RuntimeError("Position confirmation failed, both legs closed")

    # ---- 6. Read actual fill prices from exchange positions -------------
    long_positions, short_positions = await asyncio.gather(
        long_adapter.get_open_positions(),
        short_adapter.get_open_positions(),
    )
    long_fill_entry = next(
        (p.entry_price for p in long_positions if p.symbol.upper() == opp.symbol.upper() and abs(p.size) > 1e-8),
        long_price,  # fallback to limit price
    )
    short_fill_entry = next(
        (p.entry_price for p in short_positions if p.symbol.upper() == opp.symbol.upper() and abs(p.size) > 1e-8),
        short_price,  # fallback to limit price
    )

    # ---- 7. Insert position record + legs ------------------------------
    pos_row = await store.conn.execute(
        """INSERT INTO positions(cycle_id, symbol, is_active,
           exchange_long, exchange_short,
           opened_at, updated_at)
           VALUES(?,?,1,?,?,?,?) RETURNING id""",
        (cycle_id, opp.symbol, opp.long_leg.exchange_id, opp.short_leg.exchange_id,
         utc_now_iso(), utc_now_iso()),
    )
    position_id = (await pos_row.fetchone())[0]
    await store.conn.commit()

    # Insert leg records with actual fill prices
    await store.conn.execute(
        """INSERT INTO position_legs(position_id, exchange_id, side, size, entry_price, market_id, opened_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (position_id, opp.long_leg.exchange_id, "long", size_base, long_fill_entry,
         str(long_market_id), utc_now_iso(), utc_now_iso()),
    )
    await store.conn.execute(
        """INSERT INTO position_legs(position_id, exchange_id, side, size, entry_price, market_id, opened_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (position_id, opp.short_leg.exchange_id, "short", size_base, short_fill_entry,
         str(short_market_id), utc_now_iso(), utc_now_iso()),
    )
    await store.conn.commit()

    await store.conn.execute(
        "UPDATE cycles SET state='HOLDING', opened_at=?, "
        "long_size=?, short_size=?, long_entry_price=?, short_entry_price=?, "
        "updated_at=? WHERE id=?",
        (utc_now_iso(), size_base, size_base, long_fill_entry, short_fill_entry, utc_now_iso(), cycle_id),
    )
    await store.conn.commit()

    # Insert OPEN order records into orders table
    _legs = [
        (cycle_id, position_id, opp.long_leg.exchange_id, opp.symbol, "OPEN", "buy",
         long_result.order_id if long_result else None, long_price, long_fill_entry, size_base,
         (long_fill_entry or long_price) * size_base, long_result.fee if long_result else 0.0, now_iso),
        (cycle_id, position_id, opp.short_leg.exchange_id, opp.symbol, "OPEN", "sell",
         short_result.order_id if short_result else None, short_price, short_fill_entry, size_base,
         (short_fill_entry or short_price) * size_base, short_result.fee if short_result else 0.0, now_iso),
    ]
    await store.conn.executemany(
        """INSERT INTO orders(cycle_id, position_id, exchange_id, symbol, action, side,
           order_id, order_price, fill_price, size, notional, fee, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        _legs,
    )
    await store.conn.commit()

    await store.append_event(Event(
        level="info", event_type="OPENING_DONE", cycle_id=cycle_id,
        data={"symbol": opp.symbol, "size": size_base, "long_exchange": opp.long_leg.exchange_id,
              "short_exchange": opp.short_leg.exchange_id},
    ))

    return PositionState(
        symbol=opp.symbol, cycle_id=cycle_id,
        legs={
            opp.long_leg.exchange_id: {"side": "buy", "size": size_base, "entry": long_price},
            opp.short_leg.exchange_id: {"side": "sell", "size": size_base, "entry": short_price},
        },
        opened_at=now_iso,
    )


async def close_position(
    adapters: dict[str, ExchangeAdapter],
    store: Store,
    config: ExecConfig,
    position_id: int | None = None,
) -> None:
    """Close a delta-neutral position.

    If ``position_id`` is given, close that specific position.
    Otherwise close the first active position found.

    1. Load position from DB
    2. Close both legs concurrently (reduce-only)
    3. Confirm both sides are zero - retry up to 2x with wider cross_pct
    4. Still incomplete -> ERROR
    """
    from src.util.time import utc_now_iso

    if position_id is not None:
        row = await store.conn.execute("SELECT * FROM positions WHERE id=? AND is_active=1", (position_id,))
    else:
        row = await store.conn.execute("SELECT * FROM positions WHERE is_active=1")
    pos = await row.fetchone()
    if not pos:
        raise RuntimeError("No active position to close")

    cycle_id = pos["cycle_id"]
    symbol = pos["symbol"]
    exchange_long_id = pos["exchange_long"]
    exchange_short_id = pos["exchange_short"]

    long_adapter = adapters[exchange_long_id]
    short_adapter = adapters[exchange_short_id]

    # Load leg details
    leg_rows = await store.conn.execute(
        "SELECT * FROM position_legs WHERE position_id=?", (pos["id"],)
    )
    legs = await leg_rows.fetchall()
    leg_by_exchange = {l["exchange_id"]: l for l in legs}
    long_leg = leg_by_exchange[exchange_long_id]
    short_leg = leg_by_exchange[exchange_short_id]

    # Closing side is opposite of opening
    close_long_side = "sell"  # long leg was BUY, close = SELL
    close_short_side = "buy"  # short leg was SELL, close = BUY

    # Get fresh prices for closing
    long_md = await long_adapter.get_market_details(symbol)
    short_md = await short_adapter.get_market_details(symbol)
    long_market_id = long_md.market_id
    short_market_id = short_md.market_id

    long_bba, short_bba = await asyncio.gather(
        long_adapter.get_best_bid_ask(long_market_id),
        short_adapter.get_best_bid_ask(short_market_id),
    )
    long_close_px = cross_price("sell", long_bba.bid, long_bba.ask, tick=long_md.price_tick, cross_pct=config.cross_pct)
    short_close_px = cross_price("buy", short_bba.bid, short_bba.ask, tick=short_md.price_tick, cross_pct=config.cross_pct)

    await store.append_event(Event(
        level="info", event_type="CLOSING_START", cycle_id=cycle_id,
        data={"symbol": symbol, "long_exchange": exchange_long_id, "short_exchange": exchange_short_id},
    ))

    closed = False
    close_results: list[Optional[OrderResult]] = [None, None]  # [long, short]
    for attempt in range(3):  # up to 2 retries
        if attempt > 0:
            wider_pct = config.cross_pct * (1.0 + attempt * 0.5)
            logger.warning("Close retry %d/2 with cross_pct=%.1f%%", attempt, wider_pct)

        # Check which legs are still open; only retry those
        long_positions, short_positions = await asyncio.gather(
            long_adapter.get_open_positions(),
            short_adapter.get_open_positions(),
        )
        long_still_open = any(p.symbol.upper() == symbol.upper() and abs(p.size) > 1e-8 for p in long_positions)
        short_still_open = any(p.symbol.upper() == symbol.upper() and abs(p.size) > 1e-8 for p in short_positions)

        if not long_still_open and not short_still_open:
            closed = True
            break

        close_tasks = []
        if long_still_open:
            if attempt > 0:
                bba = await long_adapter.get_best_bid_ask(long_market_id)
                long_close_px = cross_price("sell", bba.bid, bba.ask, tick=long_md.price_tick, cross_pct=wider_pct)
            close_tasks.append(
                long_adapter.close_position(symbol=symbol, side=close_long_side,
                                            size_base=long_leg["size"], price=long_close_px,
                                            market_id=long_market_id)
            )
        if short_still_open:
            if attempt > 0:
                bba = await short_adapter.get_best_bid_ask(short_market_id)
                short_close_px = cross_price("buy", bba.bid, bba.ask, tick=short_md.price_tick, cross_pct=wider_pct)
            close_tasks.append(
                short_adapter.close_position(symbol=symbol, side=close_short_side,
                                             size_base=short_leg["size"], price=short_close_px,
                                             market_id=short_market_id)
            )

        if close_tasks:
            _results = await asyncio.gather(*close_tasks, return_exceptions=True)
            # Capture results for orders table
            _idx = 0
            if long_still_open:
                long_result = _results[_idx]
                if not isinstance(long_result, Exception) and long_result is not None:
                    close_results[0] = long_result
                _idx += 1
            if short_still_open:
                short_result = _results[_idx]
                if not isinstance(short_result, Exception) and short_result is not None:
                    close_results[1] = short_result

        if await _confirm_flat(long_adapter, short_adapter, symbol, config):
            closed = True
            break

    if not closed:
        await store.conn.execute(
            "UPDATE cycles SET state='ERROR', updated_at=? WHERE id=?",
            (utc_now_iso(), cycle_id),
        )
        await store.conn.commit()
        await store.append_event(Event(
            level="error", event_type="CLOSING_FAILED", cycle_id=cycle_id,
            data={"reason": "Position not flat after retries"},
        ))
        raise RuntimeError("Close incomplete after 3 attempts - ESCALATE TO ERROR")

    # ---- Record close prices, realized PnL, and CLOSE orders --------------
    long_entry = long_leg["entry_price"]
    short_entry = short_leg["entry_price"]
    long_size = long_leg["size"]
    short_size = short_leg["size"]

    close_long_result, close_short_result = close_results[0], close_results[1]
    long_fill_close_px = (
        close_long_result.fill_price
        if close_long_result and close_long_result.fill_price is not None
        else long_close_px
    )
    short_fill_close_px = (
        close_short_result.fill_price
        if close_short_result and close_short_result.fill_price is not None
        else short_close_px
    )

    # Long leg: opened BUY → closed SELL: PnL = (close - entry) * size
    long_realized = (long_fill_close_px - long_entry) * long_size
    # Short leg: opened SELL → closed BUY: PnL = (entry - close) * size
    short_realized = (short_entry - short_fill_close_px) * short_size

    # Update leg records with close prices
    for leg in [long_leg, short_leg]:
        close_px = long_fill_close_px if leg["exchange_id"] == exchange_long_id else short_fill_close_px
        await store.conn.execute(
            "UPDATE position_legs SET close_price=?, updated_at=? WHERE id=?",
            (close_px, utc_now_iso(), leg["id"]),
        )

    # Mark position inactive with PnL
    await store.conn.execute(
        "UPDATE positions SET is_active=0, updated_at=? WHERE id=?",
        (utc_now_iso(), pos["id"]),
    )
    # Sum actual funding payments for this position
    fp_cursor = await store.conn.execute(
        "SELECT exchange_id, SUM(amount) as total FROM funding_payments WHERE position_id=? GROUP BY exchange_id",
        (pos["id"],),
    )
    fp_rows = await fp_cursor.fetchall()
    funding_by_exchange = {row["exchange_id"]: (row["total"] or 0.0) for row in fp_rows}
    long_funding_pnl = funding_by_exchange.get(exchange_long_id, 0.0)
    short_funding_pnl = funding_by_exchange.get(exchange_short_id, 0.0)

    await store.conn.execute(
        "UPDATE cycles SET state='CLOSED', closed_at=?, long_close_pnl=?, short_close_pnl=?, long_funding_pnl=?, short_funding_pnl=?, updated_at=? WHERE id=?",
        (utc_now_iso(), long_realized, short_realized, long_funding_pnl, short_funding_pnl, utc_now_iso(), cycle_id),
    )
    await store.conn.commit()

    # Insert CLOSE order records
    close_now = utc_now_iso()
    close_legs = [
        (cycle_id, pos["id"], exchange_long_id, symbol, "CLOSE", close_long_side,
         close_long_result.order_id if close_long_result else None, long_close_px, long_fill_close_px,
         long_leg["size"], long_fill_close_px * long_leg["size"],
         close_long_result.fee if close_long_result else 0.0, close_now),
        (cycle_id, pos["id"], exchange_short_id, symbol, "CLOSE", close_short_side,
         close_short_result.order_id if close_short_result else None, short_close_px, short_fill_close_px,
         short_leg["size"], short_fill_close_px * short_leg["size"],
         close_short_result.fee if close_short_result else 0.0, close_now),
    ]
    await store.conn.executemany(
        """INSERT INTO orders(cycle_id, position_id, exchange_id, symbol, action, side,
           order_id, order_price, fill_price, size, notional, fee, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        close_legs,
    )
    await store.conn.commit()

    await store.append_event(Event(
        level="info", event_type="CLOSING_DONE", cycle_id=cycle_id,
        data={"symbol": symbol, "long_realized": long_realized, "short_realized": short_realized,
              "long_close_px": long_fill_close_px, "short_close_px": short_fill_close_px},
    ))


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

async def _confirm_positions(
    long_adapter: ExchangeAdapter,
    short_adapter: ExchangeAdapter,
    symbol: str,
    config: ExecConfig,
) -> bool:
    """Poll until both exchanges show non-zero positions or timeout."""
    deadline = time.time() + config.confirm_timeout_seconds
    while time.time() < deadline:
        try:
            long_pos, short_pos = await asyncio.gather(
                long_adapter.get_open_positions(),
                short_adapter.get_open_positions(),
            )
        except Exception:
            await asyncio.sleep(config.confirm_poll_interval)
            continue

        long_match = any(p.symbol.upper() == symbol.upper() and abs(p.size) > 1e-8 for p in long_pos)
        short_match = any(p.symbol.upper() == symbol.upper() and abs(p.size) > 1e-8 for p in short_pos)

        if long_match and short_match:
            return True

        await asyncio.sleep(config.confirm_poll_interval)

    return False


async def _confirm_flat(
    adapter_a: ExchangeAdapter,
    adapter_b: ExchangeAdapter,
    symbol: str,
    config: ExecConfig,
) -> bool:
    """Poll until both exchange positions are zero or timeout."""
    deadline = time.time() + config.confirm_timeout_seconds
    while time.time() < deadline:
        try:
            pos_a, pos_b = await asyncio.gather(
                adapter_a.get_open_positions(),
                adapter_b.get_open_positions(),
            )
        except Exception:
            await asyncio.sleep(config.confirm_poll_interval)
            continue

        match_a = any(p.symbol.upper() == symbol.upper() and abs(p.size) > 1e-8 for p in pos_a)
        match_b = any(p.symbol.upper() == symbol.upper() and abs(p.size) > 1e-8 for p in pos_b)

        if not match_a and not match_b:
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
