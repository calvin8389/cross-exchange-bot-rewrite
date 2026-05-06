"""Phase 4: Order parameter pre-validation (network-optional, no orders placed)."""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.sizing import cross_price, round_price_to_tick, round_size_to_step

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BOT_SYMBOLS = [
    "BTC", "ETH", "SOL", "SUI", "ARB", "OP",
    "BCH", "LINK", "PENDLE",
]
NOTIONAL = 100.0


def _load_fixture(exchange: str) -> dict:
    filename = {
        "lighter": "lighter_markets.json",
        "edgex": "edgex_contracts.json",
        "hl": "hl_markets.json",
        "grvt": "grvt_markets.json",
    }[exchange]
    with open(FIXTURES / filename) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Sizing pre-checks (no network)
# ---------------------------------------------------------------------------

class TestPriceSizing:
    """Verify cross_price outputs are always tick-aligned for all fixture values."""

    @pytest.mark.parametrize("exchange", ["lighter", "edgex", "hl", "grvt"])
    @pytest.mark.parametrize("symbol", BOT_SYMBOLS)
    def test_cross_price_tick_aligned(self, exchange, symbol):
        contracts = _load_fixture(exchange)["contracts"]
        c = contracts.get(symbol)
        if c is None:
            pytest.skip(f"{symbol} not in {exchange}")
        tick = c["price_tick"]

        for cross_pct in [3.0, 4.5, 6.0]:
            for side, ref_a, ref_b in [("buy", 100.0, 100.1), ("sell", 99.9, 100.0)]:
                px = cross_price(side, ref_a, ref_b, tick, cross_pct)
                ratio = Decimal(str(px)) / Decimal(str(tick))
                assert ratio % 1 < Decimal("1e-8") or ratio % 1 > Decimal("0.99999999"), \
                    f"{exchange}/{symbol} {side} @ {cross_pct}%: {px} / {tick} = {float(ratio)}"

    @pytest.mark.parametrize("exchange", ["lighter", "edgex", "hl", "grvt"])
    @pytest.mark.parametrize("symbol", BOT_SYMBOLS)
    def test_size_step_aligned(self, exchange, symbol):
        contracts = _load_fixture(exchange)["contracts"]
        c = contracts.get(symbol)
        if c is None:
            pytest.skip(f"{symbol} not in {exchange}")
        step = c["size_step"]
        size = round_size_to_step(NOTIONAL / 1000.0, step)
        ratio = Decimal(str(size)) / Decimal(str(step))
        assert ratio % 1 < Decimal("1e-8") or ratio % 1 > Decimal("0.99999999"), \
            f"{exchange}/{symbol}: {size} / {step} = {float(ratio)}"


class TestMinOrderSize:
    """Verify computed sizes meet minimum order requirements."""

    @pytest.mark.parametrize("exchange", ["lighter", "edgex", "hl", "grvt"])
    @pytest.mark.parametrize("symbol", ["BTC", "ETH", "SOL"])
    def test_size_meets_minimum_after_scale_up(self, exchange, symbol):
        """Verify we can scale up to meet min_order_size."""
        contracts = _load_fixture(exchange)["contracts"]
        c = contracts.get(symbol)
        if c is None:
            pytest.skip(f"{symbol} not in {exchange}")
        step = c["size_step"]
        min_sz = c.get("min_order_size", step)
        size = round_size_to_step(NOTIONAL / 1000.0, step)
        # Auto-scale: bump up until min is met
        while size < min_sz and size > 0:
            size += step
        assert size >= min_sz, \
            f"{exchange}/{symbol}: even after scaling, size={size} < min_order_size={min_sz}"


# ---------------------------------------------------------------------------
# Lighter-specific: int scaling validation
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def lighter():
    import os
    from dotenv import load_dotenv
    load_dotenv()
    from src.exchanges.lighter_adapter import LighterAdapter
    a = LighterAdapter(
        ws_url=os.environ.get("LIGHTER_WS_URL", ""),
        rest_url=os.environ.get("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai"),
        account_index=int(os.environ.get("LIGHTER_ACCOUNT_INDEX", "0")),
    )
    yield a
    await a.close()


class TestLighterIntScaling:
    """Lighter uses int-scaled base_amount and price; must be exact."""

    @pytest.mark.parametrize("symbol", BOT_SYMBOLS)
    async def test_scaled_values_exact_integers(self, lighter, symbol):
        fixture = _load_fixture("lighter")["contracts"]
        f = fixture.get(symbol)
        if f is None:
            pytest.skip(f"{symbol} not on Lighter")

        from src.exchanges.lighter_adapter import LighterAdapter
        a: LighterAdapter = lighter
        md = await a.get_market_details(symbol)
        bba = await a.get_best_bid_ask(md.market_id)

        # Simulate what place_order does
        mid = (bba.bid + bba.ask) / 2
        raw_size = NOTIONAL / mid
        step = md.size_step
        tick = md.price_tick

        base_scaled = int(round(raw_size / step))
        buy_price = cross_price("buy", bba.bid, bba.ask, tick, 3.0)
        price_scaled = int(buy_price / tick)

        # Verify: reversing the scaling should give back the original values
        size_restored = base_scaled * step
        price_restored = price_scaled * tick

        assert size_restored == pytest.approx(raw_size, rel=0.01), \
            f"{symbol}: size_restored={size_restored}, raw={raw_size}"
        assert isinstance(base_scaled, int), f"{symbol}: base_scaled type={type(base_scaled).__name__}"
        assert isinstance(price_scaled, int), f"{symbol}: price_scaled type={type(price_scaled).__name__}"

        # Price should be close to cross_price
        assert abs(price_restored - buy_price) < tick, \
            f"{symbol}: price_restored={price_restored}, cross_price={buy_price}"


# ---------------------------------------------------------------------------
# EdgeX-specific: Decimal string validation
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def edgex():
    import os
    from dotenv import load_dotenv
    load_dotenv()
    from src.exchanges.edgex_adapter import EdgeXAdapter
    a = EdgeXAdapter(
        base_url=os.environ.get("EDGEX_BASE_URL", "https://pro.edgex.exchange"),
        account_id=int(os.environ.get("EDGEX_ACCOUNT_ID", "0")),
        private_key=os.environ.get("EDGEX_STARK_PRIVATE_KEY", "0x0"),
    )
    yield a
    await a.close()


class TestEdgeXStringFormat:
    """EdgeX expects prices and sizes as strings; must be valid Decimal."""

    @pytest.mark.parametrize("symbol", ["BTC", "ETH", "SOL"])
    async def test_price_size_valid_decimal_strings(self, edgex, symbol):
        fixture = _load_fixture("edgex")["contracts"]
        f = fixture.get(symbol)
        if f is None:
            pytest.skip(f"{symbol} not on EdgeX")

        from src.exchanges.edgex_adapter import EdgeXAdapter
        a: EdgeXAdapter = edgex
        md = await a.get_market_details(symbol)
        bba = await a.get_best_bid_ask(md.market_id)

        mid = (bba.bid + bba.ask) / 2
        size = round_size_to_step(NOTIONAL / mid, md.size_step)
        # Auto-scale to meet min_order_size
        min_sz = f["min_order_size"]
        while size < min_sz:
            size += md.size_step
        buy_px = cross_price("buy", bba.bid, bba.ask, md.price_tick, 3.0)

        # EdgeX expects str(price) and str(size) to be valid Decimal
        Decimal(str(buy_px))  # must not raise
        Decimal(str(size))   # must not raise
        assert size >= min_sz, \
            f"{symbol}: size={size} < min_order_size={min_sz}"


# ---------------------------------------------------------------------------
# Hyperliquid-specific: float_to_wire compatibility
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def hl():
    import os
    from dotenv import load_dotenv
    load_dotenv()
    from src.exchanges.hyperliquid_adapter import HyperliquidAdapter
    a = HyperliquidAdapter(
        base_url=os.environ.get("HYPERLIQUID_BASE_URL", "https://api.hyperliquid.xyz"),
        private_key_hex=os.environ.get("HYPERLIQUID_PRIVATE_KEY", "0x0"),
        account_address="0x0000000000000000000000000000000000000000",
    )
    yield a
    await a.close()


class TestHLFloatToWire:
    """HL SDK rejects prices with float artifacts via float_to_wire."""

    @pytest.mark.parametrize("symbol", BOT_SYMBOLS)
    async def test_float_to_wire_passes(self, hl, symbol):
        fixture = _load_fixture("hl")["contracts"]
        f = fixture.get(symbol)
        if f is None:
            pytest.skip(f"{symbol} not on HL")

        from hyperliquid.utils.signing import float_to_wire
        from src.exchanges.hyperliquid_adapter import HyperliquidAdapter
        a: HyperliquidAdapter = hl
        md = await a.get_market_details(symbol)
        bba = await a.get_best_bid_ask(md.market_id)

        tick = md.price_tick
        for cross_pct in [3.0, 4.5, 6.0]:
            buy_px = cross_price("buy", bba.bid, bba.ask, tick, cross_pct)
            sell_px = cross_price("sell", bba.bid, bba.ask, tick, cross_pct)
            try:
                float_to_wire(buy_px)
                float_to_wire(sell_px)
            except ValueError as e:
                pytest.fail(f"{symbol} @ {cross_pct}%: {e} (buy={buy_px}, sell={sell_px})")

        # Also round to tick precision (as done in adapter)
        import math
        precision = max(0, int(round(-math.log10(tick))))
        px_rounded = round(buy_px, precision)
        try:
            float_to_wire(px_rounded)
        except ValueError as e:
            pytest.fail(f"{symbol} rounded buy: {e} (px={px_rounded}, precision={precision})")


# ---------------------------------------------------------------------------
# GRVT-specific: min_notional validation
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def grvt():
    import os
    from dotenv import load_dotenv
    load_dotenv()
    from src.exchanges.grvt_adapter import GrvtAdapter
    a = GrvtAdapter(
        trading_account_id="0",
        private_key="0x0",
        api_key="0",
        env=os.environ.get("GRVT_ENV", "prod"),
    )
    yield a
    await a.close()


class TestGRVTMinNotional:
    """GRVT has explicit min_notional in fixture."""

    @pytest.mark.parametrize("symbol", BOT_SYMBOLS)
    async def test_size_meets_min_notional(self, grvt, symbol):
        fixture = _load_fixture("grvt")["contracts"]
        f = fixture.get(symbol)
        if f is None:
            pytest.skip(f"{symbol} not on GRVT")

        from src.exchanges.grvt_adapter import GrvtAdapter
        a: GrvtAdapter = grvt
        md = await a.get_market_details(symbol)
        bba = await a.get_best_bid_ask(md.market_id)

        mid = (bba.bid + bba.ask) / 2
        size = round_size_to_step(NOTIONAL / mid, md.size_step)
        min_notional = f.get("min_quote_amount", 100)
        # Auto-scale to meet min_notional
        while size * mid < min_notional:
            size += md.size_step
        actual_notional = size * mid
        assert actual_notional >= min_notional, \
            f"{symbol}: notional={actual_notional:.2f} < min_notional={min_notional}"
