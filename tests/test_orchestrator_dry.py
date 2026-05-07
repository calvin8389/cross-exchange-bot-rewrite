"""Phase 6: Orchestrator state machine dry-run (mock adapters, no orders)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.orchestrator import BotState, Orchestrator
from src.core.models import ExchangeLeg, Opportunity
from src.exchanges.base import OrderResult

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Real fixture data for 5 common symbols
SYMBOLS = ["BTC", "ETH", "SOL", "ARB", "SUI"]


@pytest_asyncio.fixture
async def store():
    from src.db.store import Store
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    s = Store(path)
    await s.start()
    schema = Path(__file__).parent.parent / "src" / "db" / "schema.sql"
    await s.init_schema(schema.read_text())
    yield s
    await s.close()
    os.unlink(path)


def _fake_market_details(symbol: str, exchange: str, use_fixture: bool = False):
    """Build realistic MarketDetails. If use_fixture, read from real fixture."""
    if use_fixture:
        filename = {"lighter": "lighter_markets.json", "hl": "hl_markets.json",
                     "grvt": "grvt_markets.json"}[exchange]
        with open(FIXTURES / filename) as f:
            contracts = json.load(f)["contracts"]
        c = contracts.get(symbol)
        if c:
            cm = MagicMock()
            cm.market_id = c["market_id"]
            cm.price_tick = c["price_tick"]
            cm.size_step = c["size_step"]
            return cm

    cm = MagicMock()
    cm.market_id = f"{exchange}_{symbol}"
    cm.price_tick = 0.1
    cm.size_step = 0.001
    return cm


class TestStateTransitions:
    """Test state machine transitions with mock adapters."""

    async def test_idle_flat_goes_to_analyzing(self, store):
        adapters = {
            "lighter": MagicMock(exchange_id="lighter", get_open_positions=AsyncMock(return_value=[])),
            "grvt": MagicMock(exchange_id="grvt", get_open_positions=AsyncMock(return_value=[])),
        }
        orch = Orchestrator(adapters=adapters, bot_config=_make_config(), store=store)
        orch.state = BotState.IDLE
        await orch._do_idle()
        assert orch.state == BotState.ANALYZING

    async def test_idle_with_positions_goes_to_error(self, store):
        class FakePos:
            symbol = "BTC"; size = 0.001; entry_price = 80000.0; unrealized_pnl = 0.0
        adapters = {
            "lighter": MagicMock(exchange_id="lighter", get_open_positions=AsyncMock(return_value=[FakePos()])),
            "grvt": MagicMock(exchange_id="grvt", get_open_positions=AsyncMock(return_value=[])),
        }
        orch = Orchestrator(adapters=adapters, bot_config=_make_config(), store=store)
        orch.state = BotState.IDLE
        await orch._do_idle()
        assert orch.state == BotState.ERROR

    async def test_idle_ignores_zero_size_positions(self, store):
        class FakePos:
            symbol = "BTC"; size = 0.0; entry_price = 80000.0; unrealized_pnl = 0.0

        adapters = {
            "lighter": MagicMock(exchange_id="lighter", get_open_positions=AsyncMock(return_value=[FakePos()])),
            "grvt": MagicMock(exchange_id="grvt", get_open_positions=AsyncMock(return_value=[])),
        }
        orch = Orchestrator(adapters=adapters, bot_config=_make_config(), store=store)
        orch.state = BotState.IDLE
        await orch._do_idle()
        assert orch.state == BotState.ANALYZING

    async def test_analyzing_scan_failure_stays_analyzing(self, store):
        adapters = {
            "lighter": MagicMock(exchange_id="lighter"),
            "grvt": MagicMock(exchange_id="grvt"),
        }
        orch = Orchestrator(adapters=adapters, bot_config=_make_config(), store=store)
        orch.state = BotState.ANALYZING
        with patch("src.core.orchestrator.scan_all", new=AsyncMock(side_effect=RuntimeError("scan boom"))):
            await orch._do_analyzing()
        assert orch.state == BotState.ANALYZING

    async def test_waiting_timeout_goes_to_idle(self, store):
        adapters = {"lighter": MagicMock(exchange_id="lighter"), "grvt": MagicMock(exchange_id="grvt")}
        orch = Orchestrator(adapters=adapters, bot_config=_make_config(), store=store)
        orch.state = BotState.WAITING
        orch._waiting_start = 0
        await orch._do_waiting()
        assert orch.state == BotState.IDLE


class TestOpeningClosing:
    """Test opening and closing with patched order functions."""

    async def test_opening_with_batch(self, store):
        """Orchestrator should call open_position when _batch is set."""
        a = MagicMock(exchange_id="lighter",
                      get_balance=AsyncMock(return_value=MagicMock(total_equity=1000, available=1000)),
                      get_market_details=AsyncMock(return_value=_fake_market_details("BTC", "lighter", use_fixture=True)),
                      get_best_bid_ask=AsyncMock(return_value=MagicMock(bid=80000.0, ask=80001.0)),
                      get_open_positions=AsyncMock(return_value=[MagicMock(symbol="BTC", size=0.001, entry_price=80000.0, unrealized_pnl=0.0)]),
                      place_order=AsyncMock(return_value=OrderResult(order_id="order_ok")),
                      close_position=AsyncMock(return_value=OrderResult(order_id="close_ok")))
        b = MagicMock(exchange_id="grvt",
                      get_balance=AsyncMock(return_value=MagicMock(total_equity=1000, available=1000)),
                      get_market_details=AsyncMock(return_value=_fake_market_details("BTC", "grvt", use_fixture=True)),
                      get_best_bid_ask=AsyncMock(return_value=MagicMock(bid=80000.0, ask=80001.0)),
                      get_open_positions=AsyncMock(return_value=[MagicMock(symbol="BTC", size=-0.001, entry_price=80000.0, unrealized_pnl=0.0)]),
                      place_order=AsyncMock(return_value=OrderResult(order_id="order_ok")),
                      close_position=AsyncMock(return_value=OrderResult(order_id="close_ok")))

        from src.core.orchestrator import Orchestrator
        orch = Orchestrator(adapters={"lighter": a, "grvt": b}, bot_config=_make_config(), store=store)
        orch.state = BotState.OPENING
        orch._batch = [
            Opportunity(symbol="BTC",
                        long_leg=ExchangeLeg(exchange_id="lighter", side="long", rate=0.0002, apr=20.0, bid=80000.0, ask=80001.0),
                        short_leg=ExchangeLeg(exchange_id="grvt", side="short", rate=-0.0001, apr=-10.0, bid=80000.0, ask=80001.0),
                        net_apr=30.0, spread_pct=0.01),
        ]

        await orch._do_opening()

        # Should have placed an order on the long leg
        a.place_order.assert_called()
        assert orch.state == BotState.HOLDING

    async def test_opening_with_empty_batch_returns_to_analyzing(self, store):
        a = MagicMock(exchange_id="lighter")
        b = MagicMock(exchange_id="grvt")
        orch = Orchestrator(adapters={"lighter": a, "grvt": b}, bot_config=_make_config(), store=store)
        orch.state = BotState.OPENING
        orch._batch = []

        await orch._do_opening()

        assert orch.state == BotState.ANALYZING

    async def test_closing_with_active_positions(self, store):
        """_do_closing should close all active positions."""
        c = await store.conn.execute(
            """INSERT INTO cycles(symbol, state, direction, exchange_long, exchange_short, leverage, created_at, updated_at)
               VALUES('BTC','HOLDING','long/short','lighter','grvt',3,'2025-01-01','2025-01-01') RETURNING id"""
        )
        cycle_id = (await c.fetchone())[0]
        p = await store.conn.execute(
            """INSERT INTO positions(cycle_id, symbol, is_active, exchange_long, exchange_short, opened_at, updated_at)
               VALUES(?,?,1,'lighter','grvt','2025-01-01','2025-01-01') RETURNING id""",
            (cycle_id, "BTC"),
        )
        pos_id = (await p.fetchone())[0]
        await store.conn.execute(
            """INSERT INTO position_legs(position_id, exchange_id, side, size, entry_price, market_id, opened_at, updated_at)
               VALUES(?,'lighter','long',0.001,80000.0,'lighter_BTC','2025-01-01','2025-01-01')""",
            (pos_id,),
        )
        await store.conn.execute(
            """INSERT INTO position_legs(position_id, exchange_id, side, size, entry_price, market_id, opened_at, updated_at)
               VALUES(?,'grvt','short',0.001,80000.0,'grvt_BTC','2025-01-01','2025-01-01')""",
            (pos_id,),
        )
        await store.conn.commit()

        class FakePos:
            symbol = "BTC"; size = 0.001; entry_price = 80000.0; unrealized_pnl = 0.0

        a = MagicMock(exchange_id="lighter",
                      get_market_details=AsyncMock(return_value=_fake_market_details("BTC", "lighter", use_fixture=True)),
                      get_best_bid_ask=AsyncMock(return_value=MagicMock(bid=80000.0, ask=80001.0)),
                      get_open_positions=AsyncMock(side_effect=[[FakePos()], *([[]] * 20)]),
                      close_position=AsyncMock(return_value=OrderResult(order_id="close_ok")))
        b = MagicMock(exchange_id="grvt",
                      get_market_details=AsyncMock(return_value=_fake_market_details("BTC", "grvt", use_fixture=True)),
                      get_best_bid_ask=AsyncMock(return_value=MagicMock(bid=80000.0, ask=80001.0)),
                      get_open_positions=AsyncMock(side_effect=[[FakePos()], *([[]] * 20)]),
                      close_position=AsyncMock(return_value=OrderResult(order_id="close_ok")))

        orch = Orchestrator(adapters={"lighter": a, "grvt": b}, bot_config=_make_config(), store=store)
        orch.state = BotState.CLOSING

        await orch._do_closing()

        a.close_position.assert_called()
        b.close_position.assert_called()
        assert orch.state == BotState.WAITING


def _make_config():
    from src.config import BotConfig
    return BotConfig(
        symbols_to_monitor=SYMBOLS,
        max_concurrent_positions=2,
        check_interval_seconds=1,
        wait_between_cycles_minutes=0.01,
        min_net_apr_threshold=5.0,
        max_spread_pct=0.3,
    )
