from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from db.store import Event, Store
from logging_ import setup_logging
from services.lighter_ticker_service import LighterTickerService
from services.lighter_userstats_service import LighterUserStatsService

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

    try:
        while True:
            avail, port = await userstats.get_balance()
            bid, ask = await ticker.get_best_bid_ask(market_id)

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

            await asyncio.sleep(10)

    finally:
        await userstats.stop()
        await ticker.stop()
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
