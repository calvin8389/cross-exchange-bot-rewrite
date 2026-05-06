"""Phase 1C: DB Store unit tests (no network, no cost)."""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.db.store import Event, Store


@pytest_asyncio.fixture
async def store():
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    s = Store(path)
    await s.start()
    schema = Path(__file__).parent.parent / "src" / "db" / "schema.sql"
    await s.init_schema(schema.read_text())
    yield s
    await s.close()
    os.unlink(path)


class TestStoreInit:
    async def test_start_creates_tables(self, store):
        tables = await store.conn.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        table_names = {r["name"] for r in tables}
        for t in ("bot_kv", "events", "cycles", "positions", "position_legs", "funding_snapshots", "funding_payments"):
            assert t in table_names, f"Table {t} missing"

    async def test_start_sets_wal_mode(self, store):
        row = await store.conn.execute_fetchall("PRAGMA journal_mode")
        mode = row[0][0] if row else ""
        assert mode.upper() in ("WAL", "DELETE"), f"journal_mode={mode}"


class TestKV:
    async def test_set_and_get(self, store):
        await store.kv_set("state", "IDLE")
        assert await store.kv_get("state") == "IDLE"

    async def test_overwrite(self, store):
        await store.kv_set("state", "IDLE")
        await store.kv_set("state", "HOLDING")
        assert await store.kv_get("state") == "HOLDING"

    async def test_get_missing_key(self, store):
        assert await store.kv_get("nonexistent") is None

    async def test_multiple_keys(self, store):
        await store.kv_set("k1", "v1")
        await store.kv_set("k2", "v2")
        assert await store.kv_get("k1") == "v1"
        assert await store.kv_get("k2") == "v2"


class TestEvents:
    async def test_append_and_query(self, store):
        ev = Event(level="info", event_type="TEST", data={"key": "val"})
        await store.append_event(ev)
        rows = await store.conn.execute_fetchall(
            "SELECT * FROM events WHERE event_type='TEST'"
        )
        assert len(rows) == 1
        assert rows[0]["level"] == "info"

    async def test_event_with_cycle_id(self, store):
        ev = Event(level="warning", event_type="OPENING_START", cycle_id=42, data={})
        await store.append_event(ev)
        rows = await store.conn.execute_fetchall("SELECT * FROM events WHERE cycle_id=42")
        assert len(rows) == 1

    async def test_event_timestamp_auto(self, store):
        ev = Event(level="info", event_type="NO_TS", data={})
        await store.append_event(ev)
        rows = await store.conn.execute_fetchall("SELECT * FROM events WHERE event_type='NO_TS'")
        assert rows[0]["ts"] is not None


class TestCleanup:
    async def test_cleanup_deletes_old_events(self, store):
        past_30d = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        past_1d = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        await store.append_event(Event(level="info", event_type="OLD", data={}, ts=past_30d))
        await store.append_event(Event(level="info", event_type="RECENT", data={}, ts=past_1d))
        await store.cleanup_old_data(retention_days=7)
        rows = await store.conn.execute_fetchall("SELECT * FROM events")
        types = {r["event_type"] for r in rows}
        assert "RECENT" in types, "Recent event should be kept"
        assert "OLD" not in types, "Old event should be deleted"

    async def test_cleanup_deletes_old_funding_snapshots(self, store):
        now = datetime.now(timezone.utc)
        past_8d = (now - timedelta(days=8)).isoformat()
        past_1d = (now - timedelta(days=1)).isoformat()
        await store.conn.execute(
            "INSERT INTO funding_snapshots(position_id, exchange_id, rate, apr, recorded_at) VALUES(?,?,?,?,?)",
            (1, "test", 0.0001, 10.0, past_8d),
        )
        await store.conn.execute(
            "INSERT INTO funding_snapshots(position_id, exchange_id, rate, apr, recorded_at) VALUES(?,?,?,?,?)",
            (1, "test", 0.0002, 20.0, past_1d),
        )
        await store.conn.commit()
        await store.cleanup_old_data(retention_days=7)
        rows = await store.conn.execute_fetchall("SELECT * FROM funding_snapshots")
        assert len(rows) == 1
        assert float(rows[0]["apr"]) == 20.0


class TestCyclesLifecycle:
    async def test_insert_cycle(self, store):
        from src.util.time import utc_now_iso
        now = utc_now_iso()
        cursor = await store.conn.execute(
            """INSERT INTO cycles(symbol, state, direction, exchange_long, exchange_short, leverage, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?) RETURNING id""",
            ("BTC", "OPENING", "long/short", "hl", "grvt", 3, now, now),
        )
        cycle_id = (await cursor.fetchone())[0]
        assert cycle_id > 0
        rows = await store.conn.execute_fetchall("SELECT * FROM cycles WHERE id=?", (cycle_id,))
        assert rows[0]["symbol"] == "BTC"


class TestPositionLegsLifecycle:
    async def test_position_and_legs(self, store):
        from src.util.time import utc_now_iso
        now = utc_now_iso()
        c = await store.conn.execute(
            """INSERT INTO cycles(symbol, state, direction, exchange_long, exchange_short, leverage, created_at, updated_at)
               VALUES('ETH','OPENING','long/short','hl','lighter',3,?,?) RETURNING id""",
            (now, now),
        )
        cycle_id = (await c.fetchone())[0]
        p = await store.conn.execute(
            """INSERT INTO positions(cycle_id, symbol, is_active, exchange_long, exchange_short, opened_at, updated_at)
               VALUES(?,?,1,'hl','lighter',?,?) RETURNING id""",
            (cycle_id, "ETH", now, now),
        )
        position_id = (await p.fetchone())[0]
        await store.conn.execute(
            """INSERT INTO position_legs(position_id, exchange_id, side, size, entry_price, market_id, opened_at, updated_at)
               VALUES(?,'hl','long',0.1,3000.0,'ETH',?,?)""",
            (position_id, now, now),
        )
        await store.conn.execute(
            """INSERT INTO position_legs(position_id, exchange_id, side, size, entry_price, market_id, opened_at, updated_at)
               VALUES(?,'lighter','short',0.1,2990.0,'ETH',?,?)""",
            (position_id, now, now),
        )
        await store.conn.commit()
        legs = await store.conn.execute_fetchall(
            "SELECT * FROM position_legs WHERE position_id=?", (position_id,)
        )
        assert len(legs) == 2
        sides = {l["exchange_id"]: l["side"] for l in legs}
        assert sides["hl"] == "long"
        assert sides["lighter"] == "short"
