from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest_asyncio
from src.util.time import utc_now_iso

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import BotConfig
from src.core.models import BotState
from src.core.orchestrator import Orchestrator


@pytest_asyncio.fixture
async def store():
    from src.db.store import Store

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        path = tmp.name
    s = Store(path)
    await s.start()
    schema = Path(__file__).parent.parent / "src" / "db" / "schema.sql"
    await s.init_schema(schema.read_text())
    yield s
    await s.close()
    os.unlink(path)


class FakePosition:
    def __init__(self, symbol: str, size: float, entry_price: float, unrealized_pnl: float):
        self.symbol = symbol
        self.size = size
        self.entry_price = entry_price
        self.unrealized_pnl = unrealized_pnl


async def _insert_active_position(store, *, symbol: str, opened_at: str, entry_price: float = 100.0, size: float = 1.0):
    cycle_cur = await store.conn.execute(
        """INSERT INTO cycles(symbol, state, direction, exchange_long, exchange_short, leverage, opened_at, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?) RETURNING id""",
        (symbol, "HOLDING", "long/short", "lighter", "grvt", 10, opened_at, opened_at, opened_at),
    )
    cycle_id = (await cycle_cur.fetchone())[0]
    pos_cur = await store.conn.execute(
        """INSERT INTO positions(cycle_id, symbol, is_active, exchange_long, exchange_short, opened_at, updated_at)
           VALUES(?,?,?,?,?,?,?) RETURNING id""",
        (cycle_id, symbol, 1, "lighter", "grvt", opened_at, opened_at),
    )
    position_id = (await pos_cur.fetchone())[0]
    await store.conn.execute(
        """INSERT INTO position_legs(position_id, exchange_id, side, size, entry_price, market_id, opened_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (position_id, "lighter", "long", size, entry_price, f"lighter_{symbol}", opened_at, opened_at),
    )
    await store.conn.execute(
        """INSERT INTO position_legs(position_id, exchange_id, side, size, entry_price, market_id, opened_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (position_id, "grvt", "short", size, entry_price, f"grvt_{symbol}", opened_at, opened_at),
    )
    await store.conn.commit()
    return cycle_id, position_id


def _make_adapters(*, long_pnl: float, short_pnl: float, symbol: str = "BTC"):
    long_adapter = MagicMock(exchange_id="lighter")
    short_adapter = MagicMock(exchange_id="grvt")
    long_adapter.get_open_positions = AsyncMock(
        return_value=[FakePosition(symbol=symbol, size=1.0, entry_price=100.0, unrealized_pnl=long_pnl)]
    )
    short_adapter.get_open_positions = AsyncMock(
        return_value=[FakePosition(symbol=symbol, size=-1.0, entry_price=100.0, unrealized_pnl=short_pnl)]
    )
    return {"lighter": long_adapter, "grvt": short_adapter}


async def _mark_position_closed(store, position_id: int):
    await store.conn.execute(
        "UPDATE positions SET is_active=0, updated_at='2025-01-01T00:00:00Z' WHERE id=?",
        (position_id,),
    )
    await store.conn.commit()


class TestHoldingRiskControls:
    async def test_recovery_unhedged_enters_error(self, store):
        opened_at = "2025-01-01T00:00:00Z"
        await _insert_active_position(store, symbol="BTC", opened_at=opened_at)

        long_adapter = MagicMock(exchange_id="lighter")
        short_adapter = MagicMock(exchange_id="grvt")
        long_adapter.get_open_positions = AsyncMock(
            return_value=[FakePosition(symbol="BTC", size=1.0, entry_price=100.0, unrealized_pnl=0.0)]
        )
        short_adapter.get_open_positions = AsyncMock(return_value=[])

        orch = Orchestrator(
            adapters={"lighter": long_adapter, "grvt": short_adapter},
            bot_config=BotConfig(symbols_to_monitor=["BTC"]),
            store=store,
        )

        await orch.recover()

        assert orch.state == BotState.ERROR
        event_rows = await store.conn.execute_fetchall(
            "SELECT event_type FROM events WHERE event_type='RECOVERY_UNHEDGED'"
        )
        assert len(event_rows) == 1

    async def test_holding_closes_when_max_hold_exceeded(self, store):
        _, position_id = await _insert_active_position(
            store,
            symbol="BTC",
            opened_at="2025-01-01T00:00:00Z",
        )
        adapters = _make_adapters(long_pnl=0.0, short_pnl=0.0)
        config = BotConfig(
            symbols_to_monitor=["BTC"],
            leverage=3,
            hold_duration_hours=0.5,
            enable_stop_loss=False,
            check_interval_seconds=0,
            max_concurrent_positions=1,
        )
        orch = Orchestrator(adapters=adapters, bot_config=config, store=store)
        orch.state = BotState.HOLDING

        async def _fake_close(*args, **kwargs):
            await _mark_position_closed(store, position_id)

        with patch("src.core.orchestrator.close_position", new=AsyncMock(side_effect=_fake_close)) as close_mock, \
                patch("src.core.orchestrator.scan_all", new=AsyncMock(return_value=[])):
            await orch._do_holding()

        close_mock.assert_awaited_once()
        assert orch.state == BotState.WAITING

        events = await store.conn.execute_fetchall(
            "SELECT event_type, data_json FROM events WHERE position_id=? ORDER BY id",
            (position_id,),
        )
        assert any(event["event_type"] == "CLOSE_MAX_HOLD" for event in events)

    async def test_holding_closes_when_stop_loss_triggered(self, store):
        _, position_id = await _insert_active_position(
            store,
            symbol="ETH",
            opened_at="2025-01-01T00:00:00Z",
        )
        adapters = _make_adapters(long_pnl=-5.0, short_pnl=-4.0, symbol="ETH")
        config = BotConfig(
            symbols_to_monitor=["ETH"],
            leverage=10,
            hold_duration_hours=48.0,
            enable_stop_loss=True,
            check_interval_seconds=0,
            max_concurrent_positions=1,
        )
        orch = Orchestrator(adapters=adapters, bot_config=config, store=store)
        orch.state = BotState.HOLDING

        async def _fake_close(*args, **kwargs):
            await _mark_position_closed(store, position_id)

        with patch("src.core.orchestrator.close_position", new=AsyncMock(side_effect=_fake_close)) as close_mock, \
                patch("src.core.orchestrator.scan_all", new=AsyncMock(return_value=[])):
            await orch._do_holding()

        close_mock.assert_awaited_once()
        assert orch.state == BotState.WAITING

        event_rows = await store.conn.execute_fetchall(
            "SELECT data_json FROM events WHERE position_id=? AND event_type='CLOSE_STOP_LOSS'",
            (position_id,),
        )
        assert len(event_rows) == 1
        payload = json.loads(event_rows[0]["data_json"])
        assert payload["threshold_pct"] == 7.0
        assert payload["loss_pct"] == 9.0

    async def test_holding_emits_funding_history_failure_alert(self, store):
        await _insert_active_position(
            store,
            symbol="SOL",
            opened_at=utc_now_iso(),
            entry_price=100.0,
            size=1.0,
        )
        long_adapter = MagicMock(exchange_id="lighter")
        short_adapter = MagicMock(exchange_id="grvt")
        long_adapter.get_open_positions = AsyncMock(
            return_value=[FakePosition(symbol="SOL", size=1.0, entry_price=100.0, unrealized_pnl=0.0)]
        )
        short_adapter.get_open_positions = AsyncMock(
            return_value=[FakePosition(symbol="SOL", size=-1.0, entry_price=100.0, unrealized_pnl=0.0)]
        )
        long_adapter.get_market_details = AsyncMock(return_value=MagicMock(market_id="lighter_SOL"))
        short_adapter.get_market_details = AsyncMock(return_value=MagicMock(market_id="grvt_SOL"))
        long_adapter.get_funding_rate = AsyncMock(return_value=MagicMock(rate=0.0001, apr=12.0))
        short_adapter.get_funding_rate = AsyncMock(return_value=MagicMock(rate=-0.0001, apr=-1.0))
        long_adapter.get_funding_history = AsyncMock(side_effect=RuntimeError("lighter boom"))
        short_adapter.get_funding_history = AsyncMock(side_effect=RuntimeError("grvt boom"))

        config = BotConfig(
            symbols_to_monitor=["SOL"],
            hold_duration_hours=10_000.0,
            enable_stop_loss=False,
            check_interval_seconds=0,
            runtime_failure_alert_threshold=1,
        )
        orch = Orchestrator(
            adapters={"lighter": long_adapter, "grvt": short_adapter},
            bot_config=config,
            store=store,
        )
        orch.state = BotState.HOLDING

        await orch._do_holding()

        event_rows = await store.conn.execute_fetchall(
            "SELECT event_type FROM events WHERE event_type='FUNDING_HISTORY_FAILED' ORDER BY id"
        )
        assert len(event_rows) == 2
