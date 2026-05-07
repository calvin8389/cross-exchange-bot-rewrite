from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

from dotenv import load_dotenv

from src.config import load_bot_config, load_env
from src.core.orchestrator import BotState, Orchestrator
from src.db.store import Event, Store
from src.exchanges.base import ExchangeAdapter
from src.exchanges.edgex_adapter import EdgeXAdapter
from src.exchanges.lighter_adapter import LighterAdapter
from src.logging_ import setup_logging

logger = logging.getLogger(__name__)


def _build_adapters(env) -> dict[str, ExchangeAdapter]:
    """Construct exchange adapters based on active_exchanges config."""
    adapters: dict[str, ExchangeAdapter] = {}

    if "edgex" in env.active_exchanges:
        adapters["edgex"] = EdgeXAdapter(
            base_url=env.edgex_base_url,
            account_id=env.edgex_account_id,
            private_key=env.edgex_stark_private_key,
        )
        logger.info("EdgeX adapter registered")

    if "lighter" in env.active_exchanges:
        adapters["lighter"] = LighterAdapter(
            ws_url=env.lighter_ws_url,
            rest_url=env.lighter_base_url,
            account_index=env.account_index,
        )
        logger.info("Lighter adapter registered")

    if "hyperliquid" in env.active_exchanges:
        from src.exchanges.hyperliquid_adapter import HyperliquidAdapter
        adapters["hyperliquid"] = HyperliquidAdapter(
            base_url=env.hyperliquid_base_url,
            private_key_hex=env.hyperliquid_private_key,
            account_address=env.hyperliquid_account_address,
        )
        logger.info("Hyperliquid adapter registered")

    if "grvt" in env.active_exchanges:
        from src.exchanges.grvt_adapter import GrvtAdapter
        adapters["grvt"] = GrvtAdapter(
            trading_account_id=env.grvt_trading_account_id,
            private_key=env.grvt_private_key,
            api_key=env.grvt_api_key,
            env=env.grvt_env,
        )
        logger.info("GRVT adapter registered")

    return adapters


async def main() -> None:
    setup_logging()
    load_dotenv()

    store = Store("bot.sqlite3")
    await store.start()
    await store.init_schema(Path("src/db/schema.sql").read_text(encoding="utf-8"))

    orch = None
    try:
        env = load_env()
        bot_config = load_bot_config("bot_config.json")

        adapters = _build_adapters(env)
        if len(adapters) < 2:
            raise RuntimeError(f"Need at least 2 active exchanges, got {len(adapters)}")
        await _run_startup_health_checks(store, adapters, bot_config)

        logger.info("Starting orchestrator with %d exchanges: %s",
                    len(adapters), list(adapters.keys()))
        logger.info("Monitoring %d symbols", len(bot_config.symbols_to_monitor))

        orch = Orchestrator(adapters, bot_config, store)

        # SIGUSR1 → trigger emergency CLOSING without killing the process
        def _request_close(signum, frame):  # noqa: ANN001
            logger.warning("SIGUSR1 received — requesting emergency close of all positions")
            orch.state = BotState.CLOSING

        signal.signal(signal.SIGUSR1, _request_close)

        await store.kv_set("state", "BOOT")
        await store.append_event(Event(level="info", event_type="BOOT",
                                       data={"mode": "orchestrator", "exchanges": list(adapters.keys())}))
        await orch.run()

    except RuntimeError as e:
        logger.warning("Config incomplete, running M1 demo mode: %s", e)
        await _run_demo(store)
    except asyncio.CancelledError:
        logger.info("Received shutdown signal")
    finally:
        if orch:
            await orch.stop()
        await store.close()


async def _run_startup_health_checks(
    store: Store,
    adapters: dict[str, ExchangeAdapter],
    bot_config,
) -> None:
    close_script = Path("close_all.sh")
    if not close_script.exists():
        await store.append_event(Event(
            level="error",
            event_type="STARTUP_HEALTHCHECK_FAILED",
            data={"reason": "close_all.sh missing"},
        ))
        raise RuntimeError("Startup health check failed: close_all.sh missing")

    exchanges_checked: list[str] = []
    symbols_checked: dict[str, int] = {}
    for exchange_id, adapter in adapters.items():
        try:
            balance = await adapter.get_balance()
            await adapter.get_open_positions()
            for symbol in bot_config.symbols_to_monitor:
                await adapter.get_market_details(symbol)
            exchanges_checked.append(exchange_id)
            symbols_checked[exchange_id] = len(bot_config.symbols_to_monitor)
            logger.info(
                "Startup health check OK: %s available=%.4f total=%.4f symbols=%d",
                exchange_id,
                balance.available,
                balance.total_equity,
                len(bot_config.symbols_to_monitor),
            )
        except Exception as exc:
            await store.append_event(Event(
                level="error",
                event_type="STARTUP_HEALTHCHECK_FAILED",
                data={"exchange_id": exchange_id, "error": str(exc)},
            ))
            raise RuntimeError(f"Startup health check failed on {exchange_id}: {exc}") from exc

    await store.append_event(Event(
        level="info",
        event_type="STARTUP_HEALTHCHECK_PASSED",
        data={
            "exchanges": exchanges_checked,
            "symbols_checked": symbols_checked,
            "close_all_script": str(close_script),
        },
    ))


async def _run_demo(store: Store) -> None:
    """M1 demo fallback: print balance + ticker snapshot every 5s."""
    from src.services.lighter_ticker_service import LighterTickerService, StaleDataError as TickerStaleError
    from src.services.lighter_userstats_service import LighterUserStatsService, StaleDataError

    ws_url = os.environ.get("LIGHTER_WS_URL", "wss://mainnet.zklighter.elliot.ai/stream")
    account_index = int(os.environ.get("ACCOUNT_INDEX", "0"))
    market_id = int(os.environ.get("MARKET_ID", "0"))

    await store.kv_set("state", "BOOT")
    await store.append_event(Event(level="info", event_type="BOOT", data={"ws_url": ws_url}))

    userstats = LighterUserStatsService(ws_url, account_index)
    ticker = LighterTickerService(ws_url)

    await userstats.start()
    await ticker.start()
    await ticker.subscribe(market_id)

    await userstats.wait_ready(timeout=30)
    await store.kv_set("state", "RUNNING")

    stale_count = 0
    try:
        while True:
            try:
                avail, port = await userstats.get_balance()
                bid, ask = await ticker.get_best_bid_ask(market_id)
            except (StaleDataError, TickerStaleError) as e:
                stale_count += 1
                logger.warning("Stale data (count=%d): %s", stale_count, e)
                await store.append_event(
                    Event(level="warn", event_type="STALE_DATA", data={"reason": str(e)})
                )
                if stale_count >= 3:
                    logger.error("Stale data persisted, restarting services...")
                    await userstats.stop()
                    await ticker.stop()
                    await userstats.start()
                    await ticker.start()
                    await ticker.subscribe(market_id)
                    await userstats.wait_ready(timeout=30)
                    stale_count = 0
                await asyncio.sleep(3)
                continue
            stale_count = 0

            logger.info("balance avail=%.2f port=%.2f | ticker bid=%.2f ask=%.2f", avail, port, bid, ask)

            await store.append_event(
                Event(
                    level="info",
                    event_type="SNAPSHOT",
                    data={
                        "exchange": "lighter",
                        "available": avail,
                        "portfolio": port,
                        "market_id": market_id,
                        "bid": bid,
                        "ask": ask,
                    },
                )
            )

            await asyncio.sleep(5)

    finally:
        await userstats.stop()
        await ticker.stop()
        await store.close()


if __name__ == "__main__":
    # Clear stale bytecode cache to avoid running old code
    import shutil
    _src_root = Path(__file__).resolve().parent.parent
    for _pycache in _src_root.rglob("__pycache__"):
        shutil.rmtree(_pycache, ignore_errors=True)
    logger.info("Cleared __pycache__ directories")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down...")
        # Give aiosqlite background thread time to drain
        import time
        time.sleep(0.5)
