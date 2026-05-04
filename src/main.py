from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from src.db.store import Event, Store
from src.logging_ import setup_logging
from src.services.lighter_ticker_service import LighterTickerService, StaleDataError as TickerStaleError
from src.services.lighter_userstats_service import LighterUserStatsService, StaleDataError

logger = logging.getLogger(__name__)


async def main() -> None:
    setup_logging()
    load_dotenv()

    ws_url = os.environ.get("LIGHTER_WS_URL", "wss://mainnet.zklighter.elliot.ai/stream")
    account_index = int(os.environ.get("ACCOUNT_INDEX", "0"))
    market_id = int(os.environ.get("MARKET_ID", "0"))

    store = Store("bot.sqlite3")
    await store.start()
    await store.init_schema(Path("src/db/schema.sql").read_text(encoding="utf-8"))

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
