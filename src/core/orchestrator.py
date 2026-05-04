"""State-machine orchestrator for the cross-exchange rotation bot.

Ties together adapters, scanner, execution, and store into the main
cycle loop: IDLE → ANALYZING → OPENING → HOLDING → CLOSING → WAITING.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from src.config import BotConfig, Env
from src.core.execution import ExecConfig, close_position, open_position
from src.core.models import BotState, Opportunity
from src.core.scanner import ScanConfig, scan_all
from src.db.store import Event, Store
from src.exchanges.edgex_adapter import EdgeXAdapter
from src.exchanges.lighter_adapter import LighterAdapter

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, env: Env, bot_config: BotConfig, store: Store):
        self.env = env
        self.bot_config = bot_config
        self.store = store
        self.state = BotState.IDLE

        # Built on start()
        self.lighter: Optional[LighterAdapter] = None
        self.edgex: Optional[EdgeXAdapter] = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self.lighter = LighterAdapter(
            self.env.lighter_ws_url,
            self.env.lighter_base_url,
            self.env.account_index,
        )
        self.edgex = EdgeXAdapter(
            self.env.edgex_base_url,
            self.env.edgex_account_id,
            self.env.edgex_stark_private_key,
        )
        await self.store.kv_set("state", "IDLE")

    async def stop(self) -> None:
        self._stop.set()
        if self.lighter:
            await self.lighter.close()
        if self.edgex:
            await self.edgex.close()

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    async def recover(self) -> None:
        """Check for active position on restart and resume if valid."""
        row = await self.store.conn.execute("SELECT * FROM positions WHERE is_active=1")
        pos = await row.fetchone()
        if not pos:
            logger.info("Recovery: no active position, starting fresh")
            return

        # Verify positions still exist on exchanges
        try:
            e_positions = await self.edgex.get_open_positions() if self.edgex else []
            l_positions = await self.lighter.get_open_positions() if self.lighter else []
        except Exception as e:
            logger.error("Recovery: failed to fetch positions: %s", e)
            self.state = BotState.ERROR
            return

        symbol = pos["symbol"]
        e_match = any(p.symbol.upper() == symbol.upper() and abs(p.size) > 1e-8 for p in e_positions)
        l_match = any(p.symbol.upper() == symbol.upper() and abs(p.size) > 1e-8 for p in l_positions)

        if e_match and l_match:
            logger.info("Recovery: found active hedge for %s, resuming HOLDING", symbol)
            self.state = BotState.HOLDING
            await self.store.kv_set("state", "HOLDING")
        elif not e_match and not l_match:
            logger.warning("Recovery: DB had active position but exchanges are flat — clearing")
            await self.store.conn.execute(
                "UPDATE positions SET is_active=0 WHERE id=?", (pos["id"],)
            )
            await self.store.conn.commit()
            self.state = BotState.IDLE
        else:
            logger.error("Recovery: unhedged position! One exchange has %s, the other doesn't", symbol)
            self.state = BotState.ERROR
            await self.store.kv_set("state", "ERROR")
            await self.store.append_event(Event(
                level="error", event_type="RECOVERY_UNHEDGED",
                data={"symbol": symbol, "edgex_match": e_match, "lighter_match": l_match},
            ))

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
        logger.info("=== IDLE — starting new cycle ===")
        # Ensure flat before starting
        try:
            e_pos = await self.edgex.get_open_positions() if self.edgex else []
            l_pos = await self.lighter.get_open_positions() if self.lighter else []
        except Exception:
            await asyncio.sleep(5)
            return

        if e_pos or l_pos:
            logger.error("IDLE: found unexpected open positions — entering ERROR")
            self.state = BotState.ERROR
            return

        self.state = BotState.ANALYZING
        await self.store.kv_set("state", "ANALYZING")

    async def _do_analyzing(self) -> None:
        logger.info("=== ANALYZING ===")
        scan_config = ScanConfig(
            symbols=self.bot_config.symbols_to_monitor,
            min_net_apr_threshold=self.bot_config.min_net_apr_threshold,
            max_spread_pct=self.bot_config.max_spread_pct,
            min_volume_usd=self.bot_config.min_volume_usd,
        )

        candidates = await scan_all(self.lighter, self.edgex, scan_config)
        await self.store.append_event(Event(
            level="info", event_type="SCAN_RESULT",
            data={"candidates": [{"symbol": o.symbol, "net_apr": o.net_apr, "spread": o.spread} for o in candidates[:5]]},
        ))

        if not candidates:
            logger.info("No candidates meet thresholds — waiting 60s")
            await asyncio.sleep(60)
            return

        self._next_opportunity = candidates[0]
        logger.info("Best candidate: %s net_apr=%.2f%% spread=%.3f%%", candidates[0].symbol, candidates[0].net_apr, candidates[0].spread)
        self.state = BotState.OPENING
        await self.store.kv_set("state", "OPENING")

    async def _do_opening(self) -> None:
        logger.info("=== OPENING %s ===", self._next_opportunity.symbol)
        exec_config = ExecConfig(
            leverage=self.bot_config.leverage,
            cross_pct=self.bot_config.cross_pct,
        )
        try:
            await open_position(
                self._next_opportunity, self.edgex, self.lighter, self.store, exec_config,
            )
        except Exception as e:
            logger.error("Open failed: %s — returning to IDLE", e)
            self.state = BotState.IDLE
            return

        self.state = BotState.HOLDING
        self._holding_start = asyncio.get_running_loop().time()
        await self.store.kv_set("state", "HOLDING")

    async def _do_holding(self) -> None:
        elapsed = asyncio.get_running_loop().time() - self._holding_start
        hold_seconds = self.bot_config.hold_duration_hours * 3600

        # Update position PnL in DB
        try:
            e_pos = await self.edgex.get_open_positions() if self.edgex else []
            l_pos = await self.lighter.get_open_positions() if self.lighter else []
            from src.util.time import utc_now_iso

            e_pnl = sum(p.unrealized_pnl for p in e_pos)
            l_pnl = sum(p.unrealized_pnl for p in l_pos)
            await self.store.conn.execute(
                "UPDATE positions SET edgex_unrealized_pnl=?, lighter_unrealized_pnl=?, updated_at=? WHERE is_active=1",
                (e_pnl, l_pnl, utc_now_iso()),
            )
            await self.store.conn.commit()
            logger.info("HOLDING elapsed=%.1fh/%.1fh PnL edgex=%.2f lighter=%.2f",
                        elapsed / 3600, hold_seconds / 3600, e_pnl, l_pnl)

            # Stop-loss check
            if self.bot_config.enable_stop_loss and self.bot_config.leverage > 0:
                stop_loss_pct = (100 / self.bot_config.leverage) * 0.7
                if e_pnl + l_pnl < -stop_loss_pct / 100 * self.bot_config.notional_per_position:
                    logger.warning("Stop-loss triggered")
                    self.state = BotState.CLOSING
                    await self.store.kv_set("state", "CLOSING")
                    return
        except Exception as e:
            logger.warning("HOLDING PnL update failed: %s", e)

        if elapsed >= hold_seconds:
            self.state = BotState.CLOSING
            await self.store.kv_set("state", "CLOSING")
        else:
            await asyncio.sleep(self.bot_config.check_interval_seconds)

    async def _do_closing(self) -> None:
        logger.info("=== CLOSING ===")
        exec_config = ExecConfig(cross_pct=self.bot_config.cross_pct)
        try:
            await close_position(self.edgex, self.lighter, self.store, exec_config)
        except Exception as e:
            logger.error("Close failed: %s", e)
            self.state = BotState.ERROR
            await self.store.kv_set("state", "ERROR")
            return

        self.state = BotState.WAITING
        self._waiting_start = asyncio.get_running_loop().time()
        await self.store.kv_set("state", "WAITING")

    async def _do_waiting(self) -> None:
        elapsed = asyncio.get_running_loop().time() - self._waiting_start
        wait_seconds = self.bot_config.wait_between_cycles_minutes * 60

        if elapsed >= wait_seconds:
            logger.info("=== WAITING done — back to IDLE ===")
            self.state = BotState.IDLE
            await self.store.kv_set("state", "IDLE")
        else:
            remaining = wait_seconds - elapsed
            logger.info("WAITING %.0fs remaining", remaining)
            await asyncio.sleep(min(10, remaining))

    async def _do_error(self) -> None:
        logger.error("=== ERROR — bot stopped, manual intervention required ===")
        await self.store.append_event(Event(
            level="error", event_type="ERROR_STATE",
            data={"message": "Bot in ERROR state, manual intervention required"},
        ))
        # Stay in ERROR until restarted
        while not self._stop.is_set():
            await asyncio.sleep(30)
