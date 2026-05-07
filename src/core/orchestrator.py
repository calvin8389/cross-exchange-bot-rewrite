"""State-machine orchestrator for the cross-exchange rotation bot.

Supports multi-position parallel trading: each cycle picks the top N
candidates (up to ``max_concurrent_positions``), opens them concurrently,
and monitors each position's funding-rate spread.  When a pair's net APR
drops below the threshold, that position is closed and a replacement is
scanned.

Position sizes are determined by per-symbol tiers (large / medium / small).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from src.config import BotConfig
from src.core.execution import ExecConfig, close_position, open_position
from src.core.models import BotState, Opportunity
from src.core.scanner import ScanConfig, scan_all
from src.db.store import Event, Store
from src.exchanges.base import ExchangeAdapter

logger = logging.getLogger(__name__)
PERCENT_MULTIPLIER = 100.0
STOP_LOSS_BUFFER_RATIO = 0.7
MIN_POSITION_SIZE = 1e-8


class Orchestrator:
    def __init__(
        self,
        adapters: dict[str, ExchangeAdapter],
        bot_config: BotConfig,
        store: Store,
    ):
        self.adapters = adapters
        self.bot_config = bot_config
        self.store = store
        self.state = BotState.IDLE
        self._stop = asyncio.Event()
        self._waiting_start: float = 0.0
        self._db_lock = asyncio.Lock()  # serialize SQLite writes
        self._broken_leg_count: dict[int, int] = {}  # position_id → consecutive detections

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        await self.store.kv_set("state", "IDLE")

    async def stop(self) -> None:
        self._stop.set()
        for adapter in self.adapters.values():
            try:
                await adapter.close()
            except Exception as e:
                logger.warning("Error closing adapter %s: %s", adapter.exchange_id, e)

    # ------------------------------------------------------------------
    # Tier helpers
    # ------------------------------------------------------------------

    def _get_notional(self, symbol: str) -> float:
        """Resolve position notional for a symbol from tier config."""
        tiers = self.bot_config.symbol_tiers
        amounts = self.bot_config.position_tiers
        tier = tiers.get(symbol.upper(), "medium")
        return amounts.get(tier, self.bot_config.notional_per_position)

    def _position_age_hours(self, opened_at: str | None) -> float:
        if not opened_at:
            return 0.0
        try:
            if opened_at.endswith("Z"):
                opened_dt = datetime.fromisoformat(opened_at.removesuffix("Z")).replace(tzinfo=timezone.utc)
            else:
                opened_dt = datetime.fromisoformat(opened_at)
                if opened_dt.tzinfo is None:
                    opened_dt = opened_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return 0.0
        return max(0.0, (datetime.now(timezone.utc) - opened_dt).total_seconds() / 3600)

    def _stop_loss_threshold_pct(self) -> float:
        leverage = max(1, self.bot_config.leverage)
        return (PERCENT_MULTIPLIER / leverage) * STOP_LOSS_BUFFER_RATIO

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    async def recover(self) -> None:
        """Check for active positions on restart and resume if valid."""
        rows = await self.store.conn.execute("SELECT * FROM positions WHERE is_active=1")
        active = await rows.fetchall()
        if not active:
            logger.info("Recovery: no active positions, starting fresh")
            return

        logger.info("Recovery: found %d active position(s)", len(active))

        all_ok = True
        for pos in active:
            exchange_long_id = pos["exchange_long"]
            exchange_short_id = pos["exchange_short"]
            symbol = pos["symbol"]
            long_adapter = self.adapters.get(exchange_long_id)
            short_adapter = self.adapters.get(exchange_short_id)
            if not long_adapter or not short_adapter:
                logger.error("Recovery: adapters missing for %s/%s - clearing position %d",
                             exchange_long_id, exchange_short_id, pos["id"])
                await self.store.conn.execute("UPDATE positions SET is_active=0 WHERE id=?", (pos["id"],))
                await self.store.conn.commit()
                all_ok = False
                continue

            try:
                long_positions = await long_adapter.get_open_positions()
                short_positions = await short_adapter.get_open_positions()
            except Exception as e:
                logger.error("Recovery: fetch failed for %s: %s", symbol, e)
                all_ok = False
                continue

            long_match = any(p.symbol.upper() == symbol.upper() and abs(p.size) > 1e-8 for p in long_positions)
            short_match = any(p.symbol.upper() == symbol.upper() and abs(p.size) > 1e-8 for p in short_positions)

            if not long_match and not short_match:
                logger.warning("Recovery: %s flat on both exchanges - clearing", symbol)
                await self.store.conn.execute("UPDATE positions SET is_active=0 WHERE id=?", (pos["id"],))
                await self.store.conn.commit()
                all_ok = False
            elif long_match and short_match:
                logger.info("Recovery: %s hedge verified (%s/%s)", symbol, exchange_long_id, exchange_short_id)
            else:
                logger.error("Recovery: %s UNHEDGED! %s=%s %s=%s",
                             symbol, exchange_long_id, long_match, exchange_short_id, short_match)
                await self.store.append_event(Event(
                    level="error", event_type="RECOVERY_UNHEDGED",
                    data={"symbol": symbol, "long_exchange": exchange_long_id,
                          "short_exchange": exchange_short_id,
                          "long_match": long_match, "short_match": short_match},
                ))
                all_ok = False

        if all_ok and active:
            self.state = BotState.HOLDING
            await self.store.kv_set("state", "HOLDING")
        elif not all_ok:
            still_active = await self.store.conn.execute_fetchall("SELECT id FROM positions WHERE is_active=1")
            if still_active:
                self.state = BotState.HOLDING
                await self.store.kv_set("state", "HOLDING")
            else:
                self.state = BotState.IDLE

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        await self.start()
        await self.recover()

        while not self._stop.is_set():
            try:
                if self.state == BotState.IDLE:
                    await self._do_idle()
                elif self.state == BotState.ANALYZING:
                    await self._do_analyzing()
                elif self.state == BotState.OPENING:
                    await self._do_opening()
                elif self.state == BotState.HOLDING:
                    await self._do_holding()
                elif self.state == BotState.CLOSING:
                    await self._do_closing()
                elif self.state == BotState.WAITING:
                    await self._do_waiting()
                elif self.state == BotState.ERROR:
                    await self._do_error()
                else:
                    logger.error("Unknown state: %s", self.state)
                    break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Unhandled error in state %s: %s", self.state, e)

        await self.stop()

    # ------------------------------------------------------------------
    # State handlers
    # ------------------------------------------------------------------

    async def _do_idle(self) -> None:
        logger.info("=== IDLE - starting new cycle ===")
        try:
            all_positions = await asyncio.gather(
                *(adapter.get_open_positions() for adapter in self.adapters.values()),
                return_exceptions=True,
            )
        except Exception:
            await asyncio.sleep(5)
            return

        any_open = False
        for positions in all_positions:
            if isinstance(positions, list) and positions:
                any_open = True
                break

        if any_open:
            logger.error("IDLE: found unexpected open positions - entering ERROR")
            self.state = BotState.ERROR
            return

        self.state = BotState.ANALYZING
        await self.store.kv_set("state", "ANALYZING")

    async def _do_analyzing(self) -> None:
        logger.info("=== ANALYZING ===")
        threshold = self.bot_config.min_net_apr_threshold
        scan_config = ScanConfig(
            symbols=self.bot_config.symbols_to_monitor,
            min_net_apr_threshold=threshold,
            max_spread_pct=self.bot_config.max_spread_pct,
            min_volume_usd=self.bot_config.min_volume_usd,
        )

        candidates = await scan_all(self.adapters, scan_config)
        await self.store.append_event(Event(
            level="info", event_type="SCAN_RESULT",
            data={"candidates": [{"symbol": o.symbol, "net_apr": o.net_apr, "spread": o.spread_pct,
                                  "pair": f"{o.long_leg.exchange_id}/{o.short_leg.exchange_id}"}
                                 for o in candidates[:10]]},
        ))

        if not candidates:
            logger.info("No candidates meet %.0f%% threshold - waiting 60s", threshold)
            await asyncio.sleep(60)
            return

        # Pick top N by net_apr, one per symbol
        seen = set()
        top: list[Opportunity] = []
        for c in candidates:
            if c.symbol.upper() in seen:
                continue
            seen.add(c.symbol.upper())
            top.append(c)
            if len(top) >= self.bot_config.max_concurrent_positions:
                break

        self._batch = top
        symbols = [c.symbol for c in top]
        notional_info = ", ".join(f"{c.symbol}=${self._get_notional(c.symbol):.0f}" for c in top)
        logger.info("Selected %d positions: %s | notional: %s", len(top), symbols, notional_info)
        self.state = BotState.OPENING
        await self.store.kv_set("state", "OPENING")

    async def _do_opening(self) -> None:
        batch = self._batch
        logger.info("=== OPENING %d positions ===", len(batch))

        opened: list[tuple[str, str, str]] = []

        async def _open_one(opp: Opportunity):
            notional = self._get_notional(opp.symbol)
            cfg = ExecConfig(
                leverage=self.bot_config.leverage,
                cross_pct=self.bot_config.cross_pct,
                notional_override=notional,
            )
            logger.info("  Opening %s ($%.0f) pair=%s/%s net_apr=%.2f%%",
                        opp.symbol, notional, opp.long_leg.exchange_id,
                        opp.short_leg.exchange_id, opp.net_apr)
            async with self._db_lock:  # serialize SQLite writes across concurrent opens
                try:
                    result = await open_position(opp, self.adapters, self.store, cfg)
                    opened.append((opp.symbol, opp.long_leg.exchange_id, opp.short_leg.exchange_id))
                    logger.info("  OK %s opened size=%.6f", opp.symbol,
                                result.legs[opp.long_leg.exchange_id]["size"])
                except Exception as e:
                    logger.error("  FAIL %s open: %s", opp.symbol, e)
                    raise

        results = await asyncio.gather(*[_open_one(opp) for opp in batch], return_exceptions=True)
        failures = [(batch[i], results[i]) for i in range(len(batch)) if isinstance(results[i], Exception)]

        if failures:
            logger.error("%d positions failed to open; rolling back %d opened", len(failures), len(opened))
            # Rollback directly from exchange positions, NOT from DB
            for sym, long_ex, short_ex in opened:
                logger.warning("  Rolling back %s (%s/%s)", sym, long_ex, short_ex)
                for ex_id, close_side in [(long_ex, "sell"), (short_ex, "buy")]:
                    adapter = self.adapters.get(ex_id)
                    if not adapter:
                        continue
                    try:
                        positions = await adapter.get_open_positions()
                        target = next((p for p in positions if p.symbol.upper() == sym.upper() and abs(p.size) > 1e-8), None)
                        if not target:
                            continue
                        md = await adapter.get_market_details(sym)
                        bba = await adapter.get_best_bid_ask(md.market_id)
                        actual_side = "sell" if target.size > 0 else "buy"
                        px = bba.bid if actual_side == "sell" else bba.ask
                        await adapter.close_position(sym, actual_side, abs(target.size), px, md.market_id)
                        logger.info("  Rolled back %s %s %.4f", ex_id, actual_side, abs(target.size))
                    except Exception as e2:
                        logger.error("  Rollback %s/%s failed: %s", sym, ex_id, e2)
            self.state = BotState.IDLE
            await self.store.kv_set("state", "IDLE")
            return

        self.state = BotState.HOLDING
        logger.info("=== HOLDING %d position(s) ===", len(batch))
        await self.store.kv_set("state", "HOLDING")

    # ------------------------------------------------------------------
    # HOLDING: monitor net APR, close when spread collapses
    # ------------------------------------------------------------------

    async def _do_holding(self) -> None:
        threshold = self.bot_config.min_net_apr_threshold

        rows = await self.store.conn.execute("SELECT * FROM positions WHERE is_active=1")
        active = await rows.fetchall()

        if not active:
            logger.info("HOLDING: no active positions - back to WAITING")
            self.state = BotState.WAITING
            self._waiting_start = asyncio.get_running_loop().time()
            await self.store.kv_set("state", "WAITING")
            return

        from src.util.time import utc_now_iso

        closed_ids: list[int] = []
        still_open = 0

        for pos in active:
            pos_id = pos["id"]
            symbol = pos["symbol"]
            long_ex = pos["exchange_long"]
            short_ex = pos["exchange_short"]

            long_adapter = self.adapters.get(long_ex)
            short_adapter = self.adapters.get(short_ex)

            # Update PnL
            if long_adapter and short_adapter:
                try:
                    long_positions = await long_adapter.get_open_positions()
                    short_positions = await short_adapter.get_open_positions()
                except Exception:
                    long_positions = []
                    short_positions = []

                long_symbol_positions = [
                    p for p in long_positions
                    if p.symbol.upper() == symbol.upper() and abs(p.size) > MIN_POSITION_SIZE
                ]
                short_symbol_positions = [
                    p for p in short_positions
                    if p.symbol.upper() == symbol.upper() and abs(p.size) > MIN_POSITION_SIZE
                ]
                long_pnl = sum(p.unrealized_pnl for p in long_symbol_positions)
                short_pnl = sum(p.unrealized_pnl for p in short_symbol_positions)

                leg_rows = await self.store.conn.execute(
                    "SELECT * FROM position_legs WHERE position_id=?", (pos_id,)
                )
                legs = await leg_rows.fetchall()
                for leg in legs:
                    if leg["exchange_id"] == long_ex:
                        await self.store.conn.execute(
                            "UPDATE position_legs SET unrealized_pnl=?, updated_at=? WHERE id=?",
                            (long_pnl, utc_now_iso(), leg["id"]),
                        )
                    elif leg["exchange_id"] == short_ex:
                        await self.store.conn.execute(
                            "UPDATE position_legs SET unrealized_pnl=?, updated_at=? WHERE id=?",
                            (short_pnl, utc_now_iso(), leg["id"]),
                        )
                await self.store.conn.commit()

                # Detect broken legs: one side flat, the other not
                long_still_there = bool(long_symbol_positions)
                short_still_there = bool(short_symbol_positions)
                if long_still_there != short_still_there:
                    broken_side = long_ex if long_still_there else short_ex
                    flat_side = short_ex if long_still_there else long_ex
                    self._broken_leg_count[pos_id] = self._broken_leg_count.get(pos_id, 0) + 1
                    count = self._broken_leg_count[pos_id]
                    logger.warning(
                        "UNHEDGED %s: %s has position, %s is flat (check %d/3)",
                        symbol, broken_side, flat_side, count,
                    )
                    if count >= 3:
                        logger.error("UNHEDGED %s confirmed after %d checks - entering ERROR",
                                     symbol, count)
                        await self.store.append_event(Event(
                            level="error", event_type="UNHEDGED_POSITION", position_id=pos_id,
                            data={"symbol": symbol, "broken_side": broken_side, "flat_side": flat_side},
                        ))
                        self.state = BotState.ERROR
                        await self.store.kv_set("state", "ERROR")
                        return
                elif pos_id in self._broken_leg_count:
                    # Legs rebalanced — reset counter
                    self._broken_leg_count.pop(pos_id, None)

                # Stop loss check
                avg_leg_notional = 0.0
                if legs:
                    leg_notionals = [abs(leg["size"] * leg["entry_price"]) for leg in legs]
                    if leg_notionals:
                        avg_leg_notional = sum(leg_notionals) / len(leg_notionals)

                total_unrealized_pnl = long_pnl + short_pnl
                if self.bot_config.enable_stop_loss and avg_leg_notional > 0:
                    stop_loss_pct = self._stop_loss_threshold_pct()
                    loss_pct = max(0.0, (-total_unrealized_pnl / avg_leg_notional) * PERCENT_MULTIPLIER)
                    if loss_pct >= stop_loss_pct:
                        logger.warning(
                            "STOP LOSS %s: loss_pct=%.2f%% threshold=%.2f%% pnl=%.4f",
                            symbol, loss_pct, stop_loss_pct, total_unrealized_pnl,
                        )
                        try:
                            await close_position(self.adapters, self.store,
                                                 ExecConfig(cross_pct=self.bot_config.cross_pct),
                                                 position_id=pos_id)
                            closed_ids.append(pos_id)
                            await self.store.append_event(Event(
                                level="info", event_type="CLOSE_STOP_LOSS", position_id=pos_id,
                                data={"symbol": symbol, "loss_pct": loss_pct, "threshold_pct": stop_loss_pct},
                            ))
                        except Exception as e:
                            logger.error("Stop loss close %s failed: %s", symbol, e)
                            still_open += 1
                        continue

                # Max hold duration check
                max_hold_hours = self.bot_config.hold_duration_hours
                held_hours = self._position_age_hours(pos["opened_at"])
                if max_hold_hours > 0 and held_hours >= max_hold_hours:
                    logger.info("MAX HOLD %s: held %.2fh >= %.2fh - closing position %d",
                                symbol, held_hours, max_hold_hours, pos_id)
                    try:
                        await close_position(self.adapters, self.store,
                                             ExecConfig(cross_pct=self.bot_config.cross_pct),
                                             position_id=pos_id)
                        closed_ids.append(pos_id)
                        await self.store.append_event(Event(
                            level="info", event_type="CLOSE_MAX_HOLD", position_id=pos_id,
                            data={"symbol": symbol, "held_hours": held_hours, "max_hold_hours": max_hold_hours},
                        ))
                    except Exception as e:
                        logger.error("Max hold close %s failed: %s", symbol, e)
                        still_open += 1
                    continue

            # Re-check funding rate spread for this position
            if not long_adapter or not short_adapter:
                still_open += 1
                continue

            try:
                long_md = await long_adapter.get_market_details(symbol)
                short_md = await short_adapter.get_market_details(symbol)
                long_fr, short_fr = await asyncio.gather(
                    long_adapter.get_funding_rate(long_md.market_id),
                    short_adapter.get_funding_rate(short_md.market_id),
                )
            except Exception as e:
                logger.warning("HOLDING: re-fetch funding for %s failed: %s - keeping open", symbol, e)
                still_open += 1
                continue

            if long_fr and short_fr:
                current_net_apr = abs(long_fr.apr - short_fr.apr)
                logger.info("HOLDING %s: net_apr=%.2f%% (long=%s %.2f%%, short=%s %.2f%%) threshold=%.0f%%",
                            symbol, current_net_apr, long_ex, long_fr.apr, short_ex, short_fr.apr, threshold)

                # Record funding rate snapshot
                now_iso = utc_now_iso()
                await self.store.conn.execute(
                    "INSERT INTO funding_snapshots(position_id, exchange_id, rate, apr, recorded_at) VALUES(?,?,?,?,?)",
                    (pos_id, long_ex, long_fr.rate, long_fr.apr, now_iso),
                )
                await self.store.conn.execute(
                    "INSERT INTO funding_snapshots(position_id, exchange_id, rate, apr, recorded_at) VALUES(?,?,?,?,?)",
                    (pos_id, short_ex, short_fr.rate, short_fr.apr, now_iso),
                )
                await self.store.conn.commit()

                # Fetch actual settled funding payments for each leg
                leg_rows2 = await self.store.conn.execute(
                    "SELECT opened_at FROM position_legs WHERE position_id=? LIMIT 1", (pos_id,)
                )
                opened_row = await leg_rows2.fetchone()
                since = opened_row["opened_at"] if opened_row else None

                for adapter, ex_id in [(long_adapter, long_ex), (short_adapter, short_ex)]:
                    try:
                        payments = await adapter.get_funding_history(
                            symbol=symbol, market_id=None,
                            since_ts=since, until_ts=now_iso,
                        )
                        for p in payments:
                            await self.store.conn.execute(
                                "INSERT OR IGNORE INTO funding_payments(position_id, exchange_id, ts, amount, rate) VALUES(?,?,?,?,?)",
                                (pos_id, ex_id, p.ts, p.amount, p.rate),
                            )
                    except Exception:
                        pass  # funding history is best-effort
                await self.store.conn.commit()

                if current_net_apr < threshold:
                    logger.info("  >> %s net_apr %.2f%% < %.0f%% - closing position %d",
                                symbol, current_net_apr, threshold, pos_id)
                    try:
                        await close_position(self.adapters, self.store,
                                             ExecConfig(cross_pct=self.bot_config.cross_pct),
                                             position_id=pos_id)
                        closed_ids.append(pos_id)
                        await self.store.append_event(Event(
                            level="info", event_type="CLOSE_APR_DROP", position_id=pos_id,
                            data={"symbol": symbol, "net_apr": current_net_apr, "threshold": threshold},
                        ))
                    except Exception as e:
                        logger.error("Close %s position %d failed: %s", symbol, pos_id, e)
                        still_open += 1
                else:
                    still_open += 1
            else:
                logger.warning("HOLDING: %s funding rate missing - keeping open", symbol)
                still_open += 1

        # Re-scan for replacements if positions closed OR we have open slots
        available_slots = self.bot_config.max_concurrent_positions - still_open
        if closed_ids or available_slots > 0:
            if closed_ids:
                logger.info("Closed %d position(s); %d still open, %d slots available. Scanning...",
                            len(closed_ids), still_open, available_slots)
            else:
                logger.info("%d/%d slots filled; %d empty — scanning for new positions...",
                            still_open, self.bot_config.max_concurrent_positions, available_slots)
            scan_config = ScanConfig(
                symbols=self.bot_config.symbols_to_monitor,
                min_net_apr_threshold=threshold,
                max_spread_pct=self.bot_config.max_spread_pct,
                min_volume_usd=self.bot_config.min_volume_usd,
            )
            candidates = await scan_all(self.adapters, scan_config)

            # Filter out already-open symbols
            rows2 = await self.store.conn.execute("SELECT * FROM positions WHERE is_active=1")
            still_active = await rows2.fetchall()
            open_symbols = {p["symbol"].upper() for p in still_active}
            slots = self.bot_config.max_concurrent_positions - len(still_active)

            if slots <= 0:
                logger.info("No slots available (%d/%d filled)", len(still_active), self.bot_config.max_concurrent_positions)

            seen_sym = set()
            new_positions: list[Opportunity] = []
            for c in candidates:
                sym = c.symbol.upper()
                if sym in open_symbols or sym in seen_sym:
                    continue
                seen_sym.add(sym)
                new_positions.append(c)
                if len(new_positions) >= slots:
                    break

            if new_positions:
                logger.info("Opening %d replacement(s)...", len(new_positions))

                async def _open_one(opp):
                    notional = self._get_notional(opp.symbol)
                    cfg = ExecConfig(leverage=self.bot_config.leverage,
                                     cross_pct=self.bot_config.cross_pct,
                                     notional_override=notional)
                    logger.info("  Opening %s ($%.0f) pair=%s/%s net_apr=%.2f%%",
                                opp.symbol, notional, opp.long_leg.exchange_id,
                                opp.short_leg.exchange_id, opp.net_apr)
                    await open_position(opp, self.adapters, self.store, cfg)

                open_results = await asyncio.gather(
                    *[_open_one(opp) for opp in new_positions], return_exceptions=True
                )
                for opp, result in zip(new_positions, open_results):
                    if isinstance(result, Exception):
                        logger.error("Replacement open failed for %s: %s", opp.symbol, result)
                        await self.store.append_event(Event(
                            level="error", event_type="REPLACEMENT_OPEN_FAILED",
                            data={"symbol": opp.symbol, "error": str(result)},
                        ))

        # Check final state
        rows3 = await self.store.conn.execute("SELECT id FROM positions WHERE is_active=1")
        remaining = await rows3.fetchall()
        logger.info("HOLDING: %d active position(s)", len(remaining))
        if not remaining:
            self.state = BotState.WAITING
            self._waiting_start = asyncio.get_running_loop().time()
            await self.store.kv_set("state", "WAITING")
            return

        await asyncio.sleep(self.bot_config.check_interval_seconds)

    # ------------------------------------------------------------------
    # CLOSING: emergency / manual — close everything
    # ------------------------------------------------------------------

    async def _do_closing(self) -> None:
        logger.info("=== CLOSING all active positions ===")
        exec_config = ExecConfig(cross_pct=self.bot_config.cross_pct)

        rows = await self.store.conn.execute("SELECT * FROM positions WHERE is_active=1")
        active = await rows.fetchall()
        logger.info("Closing %d position(s)", len(active))

        success_count = 0
        tasks = [
            close_position(self.adapters, self.store, exec_config, position_id=pos["id"])
            for pos in active
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for pos, result in zip(active, results):
            if isinstance(result, Exception):
                logger.error("Close position %d failed: %s", pos["id"], result)
            else:
                success_count += 1

        still_open = await self.store.conn.execute_fetchall("SELECT id FROM positions WHERE is_active=1")
        if still_open:
            logger.error("%d positions still open after closing attempts", len(still_open))
            self.state = BotState.ERROR
            await self.store.kv_set("state", "ERROR")
            return

        logger.info("=== CLOSED %d/%d positions ===", success_count, len(active))
        self.state = BotState.WAITING
        self._waiting_start = asyncio.get_running_loop().time()
        await self.store.kv_set("state", "WAITING")

    async def _do_waiting(self) -> None:
        elapsed = asyncio.get_running_loop().time() - self._waiting_start
        wait_seconds = self.bot_config.wait_between_cycles_minutes * 60

        if elapsed >= wait_seconds:
            logger.info("=== WAITING done - back to IDLE ===")
            await self.store.cleanup_old_data(retention_days=7)
            self.state = BotState.IDLE
            await self.store.kv_set("state", "IDLE")
        else:
            remaining = wait_seconds - elapsed
            logger.info("WAITING %.0fs remaining", remaining)
            await asyncio.sleep(min(10, remaining))

    async def _do_error(self) -> None:
        logger.error("=== ERROR - bot stopped, manual intervention required ===")
        await self.store.append_event(Event(
            level="error", event_type="ERROR_STATE",
            data={"message": "Bot in ERROR state, manual intervention required"},
        ))
        while not self._stop.is_set():
            await asyncio.sleep(30)
