from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from src.config import load_bot_config, load_env
from src.core.orchestrator import Orchestrator
from src.db.store import Event, Store
from src.logging_ import setup_logging

logger = logging.getLogger(__name__)


async def main() -> None:
    setup_logging()
    load_dotenv()

    store = Store("bot.sqlite3")
    await store.start()
    await store.init_schema(Path("src/db/schema.sql").read_text(encoding="utf-8"))

    # Try full orchestrator path; fall back to M1 demo if config incomplete
    try:
        env = load_env()
        bot_config = load_bot_config("bot_config.json")
        logger.info("Starting orchestrator with %d symbols", len(bot_config.symbols_to_monitor))

        orch = Orchestrator(env, bot_config, store)
        await store.kv_set("state", "BOOT")
        await store.append_event(Event(level="info", event_type="BOOT", data={"mode": "orchestrator"}))
        await orch.run()

    except RuntimeError as e:
        logger.warning("Config incomplete, running M1 demo mode: %s", e)
        await _run_demo(store)


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
    asyncio.run(main())
