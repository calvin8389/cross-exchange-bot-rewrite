"""Phase 5: Execution engine unit tests (mock adapters, no network, no cost)."""

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

from src.core.execution import ExecConfig
from src.core.models import ExchangeLeg, Opportunity
from src.exchanges.base import OrderResult


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


def _mock_adapter(name: str):
    a = MagicMock()
    a.exchange_id = name

    # Use simple objects instead of MagicMock for dataclass-like returns
    class FakeBalance:
        total_equity = 1000.0
        available = 1000.0

    class FakeMarket:
        def __init__(self, mid, tick, step, min_order_size=None, min_notional=0.0):
            self.market_id = mid
            self.price_tick = tick
            self.size_step = step
            self.min_order_size = min_order_size if min_order_size is not None else step
            self.min_notional = min_notional
            self.taker_fee_rate = 0.0
            self.maker_fee_rate = 0.0

    class FakeBBA:
        bid = 50000.0
        ask = 50001.0

    a.get_balance = AsyncMock(return_value=FakeBalance())
    a.get_market_details = AsyncMock(return_value=FakeMarket(f"{name}_BTC", 0.1, 0.001))
    a.get_best_bid_ask = AsyncMock(return_value=FakeBBA())
    a.get_open_positions = AsyncMock(return_value=[])
    a.place_order = AsyncMock(return_value=OrderResult(order_id="order_123"))
    a.close_position = AsyncMock(return_value=OrderResult(order_id="close_456"))
    return a


class FakePosition:
    def __init__(
        self,
        symbol="BTC",
        size=0.001,
        entry=50000.0,
        pnl=0.0,
        *,
        entry_price=None,
        unrealized_pnl=None,
    ):
        self.symbol = symbol
        self.size = size
        self.entry_price = entry if entry_price is None else entry_price
        self.unrealized_pnl = pnl if unrealized_pnl is None else unrealized_pnl


def _make_opp() -> Opportunity:
    return Opportunity(
        symbol="BTC",
        long_leg=ExchangeLeg(exchange_id="ex_a", side="long", rate=0.0001, apr=10.0, bid=50000.0, ask=50001.0),
        short_leg=ExchangeLeg(exchange_id="ex_b", side="short", rate=-0.0001, apr=-5.0, bid=49999.0, ask=50000.0),
        net_apr=15.0,
        spread_pct=0.01,
    )


@pytest_asyncio.fixture
def config():
    return ExecConfig(cross_pct=3.0, leverage=3, confirm_timeout_seconds=2, confirm_poll_interval=0.5)


# ---------------------------------------------------------------------------
# open_position tests
# ---------------------------------------------------------------------------

class TestOpenPosition:
    async def test_both_legs_succeed(self, store, config):
        a = _mock_adapter("ex_a")
        b = _mock_adapter("ex_b")
        # Mock positions to confirm successfully
        a.get_open_positions = AsyncMock(return_value=[FakePosition()])
        b.get_open_positions = AsyncMock(return_value=[FakePosition(size=-0.001)])
        opp = _make_opp()

        from src.core.execution import open_position
        result = await open_position(opp, {"ex_a": a, "ex_b": b}, store, config)

        assert result.symbol == "BTC"
        assert result.cycle_id > 0
        a.place_order.assert_called_once()
        b.place_order.assert_called_once()

        rows = await store.conn.execute_fetchall("SELECT * FROM positions WHERE is_active=1")
        assert len(rows) == 1
        assert rows[0]["symbol"] == "BTC"

    async def test_long_leg_fails_rollback_short(self, store, config):
        a = _mock_adapter("ex_a")
        b = _mock_adapter("ex_b")
        a.place_order = AsyncMock(return_value=None)  # fail
        opp = _make_opp()

        from src.core.execution import open_position
        with pytest.raises(RuntimeError, match="Long leg"):
            await open_position(opp, {"ex_a": a, "ex_b": b}, store, config)

        # Short leg should be rolled back
        b.close_position.assert_called()
        a.close_position.assert_called()  # also tries to close long (best effort)

        # Cycle should be ERROR
        cycles = await store.conn.execute_fetchall("SELECT state FROM cycles")
        assert cycles[0]["state"] == "ERROR"

    async def test_short_leg_fails_rollback_long(self, store, config):
        a = _mock_adapter("ex_a")
        b = _mock_adapter("ex_b")
        b.place_order = AsyncMock(return_value=None)  # fail
        opp = _make_opp()

        from src.core.execution import open_position
        with pytest.raises(RuntimeError, match="Short leg"):
            await open_position(opp, {"ex_a": a, "ex_b": b}, store, config)

        a.close_position.assert_called()  # long leg rolled back

    async def test_both_legs_fail(self, store, config):
        a = _mock_adapter("ex_a")
        b = _mock_adapter("ex_b")
        a.place_order = AsyncMock(return_value=None)
        b.place_order = AsyncMock(return_value=None)
        opp = _make_opp()

        from src.core.execution import open_position
        with pytest.raises(RuntimeError, match="Long leg.*returned None"):
            await open_position(opp, {"ex_a": a, "ex_b": b}, store, config)

        # Long returned None triggers rollback on short
        b.close_position.assert_called()

    async def test_confirm_timeout_emergency_close(self, store, config):
        a = _mock_adapter("ex_a")
        b = _mock_adapter("ex_b")
        # Positions never show up → confirmation fails
        a.get_open_positions = AsyncMock(return_value=[])
        b.get_open_positions = AsyncMock(return_value=[])
        opp = _make_opp()

        from src.core.execution import open_position
        with pytest.raises(RuntimeError, match="confirmation"):
            await open_position(opp, {"ex_a": a, "ex_b": b}, store, config)

        # Emergency close should have been called on both
        a.close_position.assert_called()
        b.close_position.assert_called()


# ---------------------------------------------------------------------------
# close_position tests
# ---------------------------------------------------------------------------

async def _setup_position(store, config):
    """Helper: open a position in DB for close testing."""
    from src.core.execution import open_position

    a = _mock_adapter("ex_a")
    b = _mock_adapter("ex_b")
    # Return a matching position for confirm
    a.get_open_positions = AsyncMock(return_value=[FakePosition()])
    b.get_open_positions = AsyncMock(return_value=[FakePosition(size=-0.001)])

    opp = _make_opp()
    result = await open_position(opp, {"ex_a": a, "ex_b": b}, store, config)
    return a, b, result.cycle_id, result


class TestClosePosition:
    async def test_close_both_legs_success(self, store, config):
        a, b, cycle_id, _ = await _setup_position(store, config)

        # Pre-flight shows open → closes → confirm shows flat
        pos_open = [FakePosition(symbol="BTC", size=0.001)]
        a.get_open_positions = AsyncMock(side_effect=[
            pos_open,  # pre-flight check: open
            [],         # after close: flat (confirm)
        ])
        b.get_open_positions = AsyncMock(side_effect=[
            [FakePosition(symbol="BTC", size=-0.001)],  # pre-flight: open
            [],                                          # confirm: flat
        ])

        from src.core.execution import close_position
        await close_position({"ex_a": a, "ex_b": b}, store, config)

        a.close_position.assert_called()
        b.close_position.assert_called()

        cycles = await store.conn.execute_fetchall("SELECT state FROM cycles WHERE id=?", (cycle_id,))
        assert cycles[0]["state"] == "CLOSED"

    async def test_close_retries_and_succeeds(self, store, config):
        a, b, cycle_id, _ = await _setup_position(store, config)

        # First check: still open → close → confirm: flat
        pos_open = [FakePosition(symbol="BTC", size=0.001)]
        a.get_open_positions = AsyncMock(side_effect=[
            pos_open,  # pre-flight: still open
            *([[]] * 20),  # all subsequent calls: flat
        ])
        b.get_open_positions = AsyncMock(side_effect=[
            [FakePosition(size=-0.001)],  # pre-flight: still open
            *([[]] * 20),  # all subsequent: flat
        ])

        from src.core.execution import close_position
        await close_position({"ex_a": a, "ex_b": b}, store, config)

        a.close_position.assert_called()
        b.close_position.assert_called()
        cycles = await store.conn.execute_fetchall("SELECT state FROM cycles WHERE id=?", (cycle_id,))
        assert cycles[0]["state"] == "CLOSED"

    async def test_three_failures_escalates(self, store, config):
        a, b, cycle_id, _ = await _setup_position(store, config)

        # Long side never closes
        pos_open = [FakePosition(symbol="BTC", size=0.001)]
        a.get_open_positions = AsyncMock(return_value=pos_open)
        b.get_open_positions = AsyncMock(return_value=[])

        from src.core.execution import close_position
        with pytest.raises(RuntimeError, match="ESCALATE"):
            await close_position({"ex_a": a, "ex_b": b}, store, config)

        cycles = await store.conn.execute_fetchall("SELECT state FROM cycles WHERE id=?", (cycle_id,))
        assert cycles[0]["state"] == "ERROR"

    async def test_close_uses_fill_price_for_realized_pnl_and_order_fill(self, store, config):
        a, b, cycle_id, _ = await _setup_position(store, config)

        a.close_position = AsyncMock(return_value=OrderResult(order_id="close_a", fill_price=50100.0))
        b.close_position = AsyncMock(return_value=OrderResult(order_id="close_b", fill_price=49900.0))
        a.get_open_positions = AsyncMock(side_effect=[
            [FakePosition(symbol="BTC", size=0.001)],
            [],
        ])
        b.get_open_positions = AsyncMock(side_effect=[
            [FakePosition(symbol="BTC", size=-0.001)],
            [],
        ])

        from src.core.execution import close_position
        await close_position({"ex_a": a, "ex_b": b}, store, config)

        cycle_rows = await store.conn.execute_fetchall("SELECT * FROM cycles WHERE id=?", (cycle_id,))
        assert cycle_rows[0]["state"] == "CLOSED"

        leg_rows = await store.conn.execute_fetchall(
            "SELECT exchange_id, side, size, entry_price, close_price FROM position_legs WHERE position_id IN "
            "(SELECT id FROM positions WHERE cycle_id=?) ORDER BY exchange_id",
            (cycle_id,),
        )
        assert leg_rows[0]["close_price"] == pytest.approx(50100.0)
        assert leg_rows[1]["close_price"] == pytest.approx(49900.0)

        long_leg = next(l for l in leg_rows if l["side"] == "long")
        short_leg = next(l for l in leg_rows if l["side"] == "short")
        expected_long = (50100.0 - long_leg["entry_price"]) * long_leg["size"]
        expected_short = (short_leg["entry_price"] - 49900.0) * short_leg["size"]
        assert cycle_rows[0]["long_close_pnl"] == pytest.approx(expected_long)
        assert cycle_rows[0]["short_close_pnl"] == pytest.approx(expected_short)

        close_orders = await store.conn.execute_fetchall(
            "SELECT exchange_id, action, fill_price FROM orders WHERE cycle_id=? AND action='CLOSE' ORDER BY exchange_id",
            (cycle_id,),
        )
        assert len(close_orders) == 2
        assert close_orders[0]["fill_price"] == pytest.approx(50100.0)
        assert close_orders[1]["fill_price"] == pytest.approx(49900.0)

    async def test_close_when_already_flat_still_completes(self, store, config):
        a, b, cycle_id, _ = await _setup_position(store, config)

        a.get_open_positions = AsyncMock(return_value=[])
        b.get_open_positions = AsyncMock(return_value=[])

        from src.core.execution import close_position
        await close_position({"ex_a": a, "ex_b": b}, store, config)

        cycles = await store.conn.execute_fetchall("SELECT state FROM cycles WHERE id=?", (cycle_id,))
        assert cycles[0]["state"] == "CLOSED"


# ---------------------------------------------------------------------------
# Size calculation edge cases
# ---------------------------------------------------------------------------

class TestSizingInExecution:
    async def test_tier_notional_override(self, store, config):
        """Notional override in config should work."""
        a = _mock_adapter("ex_a")
        b = _mock_adapter("ex_b")
        pos = [FakePosition()]
        a.get_open_positions = AsyncMock(return_value=pos)
        b.get_open_positions = AsyncMock(return_value=pos)

        config.notional_override = 200.0
        opp = _make_opp()

        from src.core.execution import open_position
        await open_position(opp, {"ex_a": a, "ex_b": b}, store, config)
        # Size should be 200 / mid_price = 200 / 50000.5 ≈ 0.004
        call_args = a.place_order.call_args
        size = call_args[1]["size_base"]
        expected = 200.0 / 50000.5
        assert size == pytest.approx(0.004, rel=0.05), f"size={size}, expected≈{expected}"

    async def test_rejects_below_min_notional(self, store, config):
        a = _mock_adapter("ex_a")
        b = _mock_adapter("ex_b")
        a.get_market_details = AsyncMock(return_value=type("M", (), {
            "market_id": "ex_a_BTC", "price_tick": 0.1, "size_step": 0.001,
            "min_order_size": 0.001, "min_notional": 500.0,
            "taker_fee_rate": 0.0, "maker_fee_rate": 0.0,
        })())
        b.get_market_details = AsyncMock(return_value=type("M", (), {
            "market_id": "ex_b_BTC", "price_tick": 0.1, "size_step": 0.001,
            "min_order_size": 0.001, "min_notional": 500.0,
            "taker_fee_rate": 0.0, "maker_fee_rate": 0.0,
        })())
        config.notional_override = 100.0

        from src.core.execution import open_position
        with pytest.raises(ValueError, match="min_notional"):
            await open_position(_make_opp(), {"ex_a": a, "ex_b": b}, store, config)

    async def test_rejects_when_total_exposure_limit_exceeded(self, store, config):
        a = _mock_adapter("ex_a")
        b = _mock_adapter("ex_b")
        config.max_total_exposure_usd = 50.0

        from src.core.execution import open_position
        with pytest.raises(ValueError, match="Total exposure"):
            await open_position(_make_opp(), {"ex_a": a, "ex_b": b}, store, config)

    async def test_close_failure_records_residual_positions_and_reason(self, store, config):
        a, b, cycle_id, _ = await _setup_position(store, config)
        pos_open = [FakePosition(symbol="BTC", size=0.001, entry=50000.0, pnl=-1.0)]
        a.get_open_positions = AsyncMock(return_value=pos_open)
        b.get_open_positions = AsyncMock(
            return_value=[FakePosition(symbol="BTC", size=-0.001, entry=50000.0, pnl=1.0)]
        )

        from src.core.execution import close_position
        with pytest.raises(RuntimeError, match="ESCALATE"):
            await close_position({"ex_a": a, "ex_b": b}, store, config, close_reason="STOP_LOSS")

        cycle_rows = await store.conn.execute_fetchall("SELECT state, close_reason FROM cycles WHERE id=?", (cycle_id,))
        assert cycle_rows[0]["state"] == "ERROR"
        assert cycle_rows[0]["close_reason"] == "STOP_LOSS"

        event_rows = await store.conn.execute_fetchall(
            "SELECT data_json FROM events WHERE cycle_id=? AND event_type='CLOSING_FAILED'",
            (cycle_id,),
        )
        assert len(event_rows) == 1
        payload = json.loads(event_rows[0]["data_json"])
        assert payload["close_reason"] == "STOP_LOSS"
        assert len(payload["residual_positions"]) == 2
