import json
from dataclasses import dataclass
from typing import Optional

import aiosqlite

from util.time import utc_now_iso


@dataclass
class Event:
    level: str
    event_type: str
    data: dict
    message: str = ""
    cycle_id: Optional[int] = None
    position_id: Optional[int] = None
    ts: Optional[str] = None


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def start(self) -> None:
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA synchronous=NORMAL;")
        await self._conn.execute("PRAGMA busy_timeout=5000;")
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Store not started"
        return self._conn

    async def init_schema(self, schema_sql: str) -> None:
        await self.conn.executescript(schema_sql)
        await self.conn.commit()

    async def kv_set(self, key: str, value: str) -> None:
        now = utc_now_iso()
        await self.conn.execute(
            "INSERT INTO bot_kv(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, now),
        )
        await self.conn.commit()

    async def append_event(self, ev: Event) -> None:
        ts = ev.ts or utc_now_iso()
        await self.conn.execute(
            "INSERT INTO events(ts,level,event_type,cycle_id,position_id,data_json,message) VALUES(?,?,?,?,?,?,?)",
            (ts, ev.level, ev.event_type, ev.cycle_id, ev.position_id, json.dumps(ev.data, ensure_ascii=False), ev.message),
        )
        await self.conn.commit()
