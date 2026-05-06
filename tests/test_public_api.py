"""Phase 2: Public API integration tests (network, no auth, no cost).

Verifies live exchange data matches fixture expectations.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIXTURES = Path(__file__).resolve().parent / "fixtures"

BOT_SYMBOLS = [
    "BTC", "ETH", "SOL", "DOGE", "SUI", "ARB", "OP",
    "LTC", "BCH", "LINK", "UNI", "AVAX", "DOT", "PENDLE", "ENA", "LIT",
]


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
# Lighter — public API tests
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def lighter_adapter():
    import os
    from dotenv import load_dotenv
    load_dotenv()
    from src.exchanges.lighter_adapter import LighterAdapter
    a = LighterAdapter(
        ws_url=os.environ.get("LIGHTER_WS_URL", ""),
        rest_url=os.environ.get("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai"),
        account_index=0,
    )
    yield a
    await a.close()


class TestLighterPublic:
    @pytest.mark.parametrize("symbol", BOT_SYMBOLS)
    async def test_market_details_match_fixture(self, lighter_adapter, symbol):
        fixture = _load_fixture("lighter")["contracts"]
        f = fixture.get(symbol)
        if f is None:
            pytest.skip(f"{symbol} not in Lighter fixture")

        from src.exchanges.lighter_adapter import LighterAdapter
        a: LighterAdapter = lighter_adapter
        md = await a.get_market_details(symbol)
        assert md.price_tick == f["price_tick"], \
            f"{symbol}: price_tick={md.price_tick}, fixture={f['price_tick']}"
        assert md.size_step == f["size_step"], \
            f"{symbol}: size_step={md.size_step}, fixture={f['size_step']}"

    @pytest.mark.parametrize("symbol", BOT_SYMBOLS)
    async def test_bba_valid(self, lighter_adapter, symbol):
        fixture = _load_fixture("lighter")["contracts"]
        f = fixture.get(symbol)
        if f is None:
            pytest.skip(f"{symbol} not in Lighter fixture")

        from src.exchanges.lighter_adapter import LighterAdapter
        a: LighterAdapter = lighter_adapter
        md = await a.get_market_details(symbol)
        bba = await a.get_best_bid_ask(md.market_id)
        assert bba.bid > 0, f"{symbol}: bid={bba.bid} <= 0"
        assert bba.ask > 0, f"{symbol}: ask={bba.ask} <= 0"
        assert bba.bid < bba.ask, f"{symbol}: bid={bba.bid} >= ask={bba.ask}"
        spread = (bba.ask - bba.bid) / bba.bid
        assert spread < 0.03, f"{symbol}: spread={spread:.4f} > 3% (abnormal)"

    @pytest.mark.parametrize("symbol", BOT_SYMBOLS)
    async def test_funding_rate_valid(self, lighter_adapter, symbol):
        fixture = _load_fixture("lighter")["contracts"]
        f = fixture.get(symbol)
        if f is None:
            pytest.skip(f"{symbol} not in Lighter fixture")

        from src.exchanges.lighter_adapter import LighterAdapter
        a: LighterAdapter = lighter_adapter
        md = await a.get_market_details(symbol)
        fr = await a.get_funding_rate(md.market_id)
        if fr is None:
            return  # some symbols may not have funding on Lighter
        # Lighter funding is 8h interval → 3 periods per day; APR can be signed
        expected_apr = fr.rate * 365 * 3 * 100
        assert abs(fr.apr - expected_apr) < 0.01, \
            f"{symbol}: apr={fr.apr}, expected≈{expected_apr} (rate={fr.rate})"

    @pytest.mark.parametrize("symbol", ["BTC", "ETH", "SOL"])
    async def test_bba_is_tick_multiple(self, lighter_adapter, symbol):
        """Bid/ask prices must be integer multiples of tick."""
        from decimal import Decimal
        a = lighter_adapter
        md = await a.get_market_details(symbol)
        bba = await a.get_best_bid_ask(md.market_id)
        tick = md.price_tick
        for name, px in [("bid", bba.bid), ("ask", bba.ask)]:
            ratio = Decimal(str(px)) / Decimal(str(tick))
            assert ratio % 1 < Decimal("1e-8") or ratio % 1 > Decimal("0.99999999"), \
                f"{symbol} {name}={px} / tick={tick} = {float(ratio)} (not integer)"


# ---------------------------------------------------------------------------
# EdgeX — public API tests
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def edgex_adapter():
    import os
    from dotenv import load_dotenv
    load_dotenv()
    from src.exchanges.edgex_adapter import EdgeXAdapter
    a = EdgeXAdapter(
        base_url=os.environ.get("EDGEX_BASE_URL", "https://pro.edgex.exchange"),
        account_id=0,
        private_key="0x0",
    )
    yield a
    await a.close()


class TestEdgeXPublic:
    @pytest.mark.parametrize("symbol", BOT_SYMBOLS)
    async def test_market_details_match_fixture(self, edgex_adapter, symbol):
        fixture = _load_fixture("edgex")["contracts"]
        f = fixture.get(symbol)
        if f is None:
            pytest.skip(f"{symbol} not in EdgeX fixture")

        from src.exchanges.edgex_adapter import EdgeXAdapter
        a: EdgeXAdapter = edgex_adapter
        md = await a.get_market_details(symbol)
        assert md.price_tick == f["price_tick"], \
            f"{symbol}: price_tick={md.price_tick}, fixture={f['price_tick']}"
        assert md.size_step == f["size_step"], \
            f"{symbol}: size_step={md.size_step}, fixture={f['size_step']}"

    @pytest.mark.parametrize("symbol", ["BTC", "ETH", "SOL", "DOGE"])
    async def test_bba_valid(self, edgex_adapter, symbol):
        fixture = _load_fixture("edgex")["contracts"]
        f = fixture.get(symbol)
        if f is None:
            pytest.skip(f"{symbol} not in EdgeX fixture")

        from src.exchanges.edgex_adapter import EdgeXAdapter
        a: EdgeXAdapter = edgex_adapter
        md = await a.get_market_details(symbol)
        # EdgeX BBA expects market_id as str (contractId)
        bba = await a.get_best_bid_ask(md.market_id)
        assert bba.bid > 0, f"{symbol}: bid={bba.bid}"
        assert bba.ask > 0, f"{symbol}: ask={bba.ask}"
        assert bba.bid < bba.ask, f"{symbol}: bid={bba.bid} >= ask={bba.ask}"
        spread = (bba.ask - bba.bid) / bba.bid
        assert spread < 0.03, f"{symbol}: spread={spread:.4f} > 3%"

    @pytest.mark.parametrize("symbol", ["BTC", "ETH", "SOL"])
    async def test_funding_rate_valid(self, edgex_adapter, symbol):
        from src.exchanges.edgex_adapter import EdgeXAdapter
        a: EdgeXAdapter = edgex_adapter
        md = await a.get_market_details(symbol)
        fr = await a.get_funding_rate(md.market_id)
        assert fr is not None, f"{symbol}: funding rate is None"
        if fr:
            # EdgeX funding is 4 hours → 6 periods per day
            expected_apr = fr.rate * 365 * 6 * 100
            assert abs(fr.apr - expected_apr) < 0.01, \
                f"{symbol}: apr={fr.apr}, expected≈{expected_apr}"


# ---------------------------------------------------------------------------
# Hyperliquid — public API tests
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def hl_adapter():
    import os
    from dotenv import load_dotenv
    load_dotenv()
    from src.exchanges.hyperliquid_adapter import HyperliquidAdapter
    # Public-only: no real private key needed for info/bba/funding
    a = HyperliquidAdapter(
        base_url=os.environ.get("HYPERLIQUID_BASE_URL", "https://api.hyperliquid.xyz"),
        private_key_hex="0x0",
        account_address="0x0000000000000000000000000000000000000000",
    )
    yield a
    await a.close()


class TestHLPublic:
    @pytest.mark.parametrize("symbol", BOT_SYMBOLS)
    async def test_market_details_valid(self, hl_adapter, symbol):
        """HL tick derived from L2 gaps; verify structural validity."""
        fixture = _load_fixture("hl")["contracts"]
        f = fixture.get(symbol)
        if f is None:
            pytest.skip(f"{symbol} not in HL fixture")

        from src.exchanges.hyperliquid_adapter import HyperliquidAdapter
        a: HyperliquidAdapter = hl_adapter
        md = await a.get_market_details(symbol)
        # Structural checks (L2-derived tick varies between runs)
        assert md.price_tick > 0, f"{symbol}: tick={md.price_tick} <= 0"
        assert 1e-8 <= md.price_tick <= 100, \
            f"{symbol}: tick={md.price_tick} out of reasonable range"
        assert md.size_step == f["size_step"], \
            f"{symbol}: size_step={md.size_step}, fixture={f['size_step']}"
        assert isinstance(md.market_id, str), f"{symbol}: market_id not str"

    @pytest.mark.parametrize("symbol", BOT_SYMBOLS)
    async def test_bba_valid(self, hl_adapter, symbol):
        fixture = _load_fixture("hl")["contracts"]
        f = fixture.get(symbol)
        if f is None:
            pytest.skip(f"{symbol} not in HL fixture")

        from src.exchanges.hyperliquid_adapter import HyperliquidAdapter
        a: HyperliquidAdapter = hl_adapter
        bba = await a.get_best_bid_ask(f["market_id"])
        assert bba.bid > 0
        assert bba.ask > 0
        assert bba.bid < bba.ask, f"{symbol}: bid={bba.bid} >= ask={bba.ask}"
        spread = (bba.ask - bba.bid) / bba.bid
        assert spread < 0.03, f"{symbol}: spread={spread:.4f} > 3%"

    @pytest.mark.parametrize("symbol", BOT_SYMBOLS)
    async def test_funding_rate_valid(self, hl_adapter, symbol):
        fixture = _load_fixture("hl")["contracts"]
        f = fixture.get(symbol)
        if f is None:
            pytest.skip(f"{symbol} not in HL fixture")

        from src.exchanges.hyperliquid_adapter import HyperliquidAdapter
        a: HyperliquidAdapter = hl_adapter
        fr = await a.get_funding_rate(f["market_id"])
        assert fr is not None, f"{symbol}: funding rate is None"
        if fr:
            # HL rate is hourly → 24 periods per day
            expected_apr = fr.rate * 365 * 24 * 100
            assert abs(fr.apr - expected_apr) < 0.01, \
                f"{symbol}: apr={fr.apr}, expected≈{expected_apr} (rate={fr.rate})"

    @pytest.mark.parametrize("symbol", ["BTC", "ETH", "LINK", "BCH"])
    async def test_bba_is_tick_multiple(self, hl_adapter, symbol):
        """HL BBA must be valid per derived tick."""
        from decimal import Decimal
        a = hl_adapter
        md = await a.get_market_details(symbol)
        bba = await a.get_best_bid_ask(md.market_id)
        tick = md.price_tick
        for name, px in [("bid", bba.bid), ("ask", bba.ask)]:
            ratio = Decimal(str(px)) / Decimal(str(tick))
            assert ratio % 1 < Decimal("1e-8") or ratio % 1 > Decimal("0.99999999"), \
                f"{symbol} {name}={px} / tick={tick} = {float(ratio)} (not integer)"


# ---------------------------------------------------------------------------
# GRVT — public API tests
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def grvt_adapter():
    import os
    from dotenv import load_dotenv
    load_dotenv()
    from src.exchanges.grvt_adapter import GrvtAdapter
    # Public-only: dummy credentials for market data
    a = GrvtAdapter(
        trading_account_id="0",
        private_key="0x0",
        api_key="0",
        env=os.environ.get("GRVT_ENV", "prod"),
    )
    yield a
    await a.close()


class TestGRVTPublic:
    @pytest.mark.parametrize("symbol", BOT_SYMBOLS)
    async def test_market_details_match_fixture(self, grvt_adapter, symbol):
        fixture = _load_fixture("grvt")["contracts"]
        f = fixture.get(symbol)
        if f is None:
            pytest.skip(f"{symbol} not in GRVT fixture")

        from src.exchanges.grvt_adapter import GrvtAdapter
        a: GrvtAdapter = grvt_adapter
        md = await a.get_market_details(symbol)
        assert md.price_tick == f["price_tick"], \
            f"{symbol}: price_tick={md.price_tick}, fixture={f['price_tick']}"
        assert md.size_step == f["size_step"], \
            f"{symbol}: size_step={md.size_step}, fixture={f['size_step']}"

    @pytest.mark.parametrize("symbol", BOT_SYMBOLS)
    async def test_bba_valid(self, grvt_adapter, symbol):
        fixture = _load_fixture("grvt")["contracts"]
        f = fixture.get(symbol)
        if f is None:
            pytest.skip(f"{symbol} not in GRVT fixture")

        from src.exchanges.grvt_adapter import GrvtAdapter
        a: GrvtAdapter = grvt_adapter
        bba = await a.get_best_bid_ask(f["market_id"])
        assert bba.bid > 0
        assert bba.ask > 0
        assert bba.bid < bba.ask, f"{symbol}: bid={bba.bid} >= ask={bba.ask}"
        spread = (bba.ask - bba.bid) / bba.bid
        assert spread < 0.03, f"{symbol}: spread={spread:.4f} > 3%"

    @pytest.mark.parametrize("symbol", BOT_SYMBOLS)
    async def test_funding_rate_valid(self, grvt_adapter, symbol):
        fixture = _load_fixture("grvt")["contracts"]
        f = fixture.get(symbol)
        if f is None:
            pytest.skip(f"{symbol} not in GRVT fixture")

        from src.exchanges.grvt_adapter import GrvtAdapter
        a: GrvtAdapter = grvt_adapter
        fr = await a.get_funding_rate(f["market_id"])
        if fr is None:
            # Some GRVT instruments may not have funding rate in ticker
            return
        # GRVT funding from ticker is a percentage rate
        assert not (-1 < fr.rate < 1) or abs(fr.rate) <= 0.1, \
            f"{symbol}: rate={fr.rate} seems too extreme"
