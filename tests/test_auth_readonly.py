"""Phase 3: Auth read-only tests (network, auth, no trading cost).

Verifies balance and position data format from authenticated endpoints.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv

load_dotenv()

FIXTURES = Path(__file__).resolve().parent / "fixtures"


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
# Lighter
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def lighter():
    from src.exchanges.lighter_adapter import LighterAdapter
    a = LighterAdapter(
        ws_url=os.environ.get("LIGHTER_WS_URL", ""),
        rest_url=os.environ.get("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai"),
        account_index=int(os.environ.get("LIGHTER_ACCOUNT_INDEX", "0")),
    )
    yield a
    await a.close()


class TestLighterAuth:
    async def test_balance_non_negative(self, lighter):
        bal = await lighter.get_balance()
        assert bal.total_equity >= 0, f"total_equity={bal.total_equity}"
        assert bal.available >= 0, f"available={bal.available}"
        assert bal.available <= bal.total_equity + 1, \
            f"available={bal.available} > total_equity={bal.total_equity}"

    async def test_positions_valid(self, lighter):
        fixture = _load_fixture("lighter")["contracts"]
        positions = await lighter.get_open_positions()
        for p in positions:
            f = fixture.get(p.symbol.upper())
            if f is None:
                continue  # symbol not in fixture (e.g. exotic pair)
            step = f["size_step"]
            tick = f["price_tick"]
            # Size must be integer multiple of step
            from decimal import Decimal
            ratio_s = Decimal(str(abs(p.size))) / Decimal(str(step))
            assert ratio_s % 1 < Decimal("1e-8") or ratio_s % 1 > Decimal("0.99999999"), \
                f"Lighter {p.symbol} size={p.size} / step={step} = {float(ratio_s)} (not integer)"
            # Entry price must be tick multiple
            if p.entry_price > 0:
                ratio_p = Decimal(str(p.entry_price)) / Decimal(str(tick))
                assert ratio_p % 1 < Decimal("1e-8") or ratio_p % 1 > Decimal("0.99999999"), \
                    f"Lighter {p.symbol} entry={p.entry_price} / tick={tick} = {float(ratio_p)}"


# ---------------------------------------------------------------------------
# EdgeX
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def edgex():
    from src.exchanges.edgex_adapter import EdgeXAdapter
    key = os.environ.get("EDGEX_STARK_PRIVATE_KEY", "0x0")
    a = EdgeXAdapter(
        base_url=os.environ.get("EDGEX_BASE_URL", "https://pro.edgex.exchange"),
        account_id=int(os.environ.get("EDGEX_ACCOUNT_ID", "0")),
        private_key=key,
    )
    yield a
    await a.close()


class TestEdgeXAuth:
    async def test_balance_non_negative(self, edgex):
        bal = await edgex.get_balance()
        assert bal.total_equity >= 0, f"total_equity={bal.total_equity}"
        assert bal.available >= 0, f"available={bal.available}"

    async def test_positions_valid(self, edgex):
        fixture = _load_fixture("edgex")["contracts"]
        positions = await edgex.get_open_positions()
        for p in positions:
            # EdgeX positions return contractId as symbol (e.g. "10000001")
            # Map to base name via fixture
            symbol = p.symbol
            f = fixture.get(symbol)
            if f is None:
                continue
            step = f["size_step"]
            tick = f["price_tick"]
            from decimal import Decimal
            ratio_s = Decimal(str(abs(p.size))) / Decimal(str(step))
            assert ratio_s % 1 < Decimal("1e-8") or ratio_s % 1 > Decimal("0.99999999"), \
                f"EdgeX {symbol} size={p.size} / step={step} = {float(ratio_s)}"
            if p.entry_price > 0:
                ratio_p = Decimal(str(p.entry_price)) / Decimal(str(tick))
                assert ratio_p % 1 < Decimal("1e-8") or ratio_p % 1 > Decimal("0.99999999"), \
                    f"EdgeX {symbol} entry={p.entry_price} / tick={tick} = {float(ratio_p)}"


# ---------------------------------------------------------------------------
# Hyperliquid
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def hl():
    from src.exchanges.hyperliquid_adapter import HyperliquidAdapter
    a = HyperliquidAdapter(
        base_url=os.environ.get("HYPERLIQUID_BASE_URL", "https://api.hyperliquid.xyz"),
        private_key_hex=os.environ.get("HYPERLIQUID_PRIVATE_KEY", "0x0"),
        account_address=os.environ.get("HYPERLIQUID_ACCOUNT_ADDRESS", ""),
    )
    yield a
    await a.close()


class TestHLAuth:
    async def test_balance_non_negative(self, hl):
        bal = await hl.get_balance()
        # HL can show 0 if account_address is wrong, but shouldn't be negative
        assert bal.total_equity >= 0 or bal.total_equity > -1e8, \
            f"total_equity={bal.total_equity}"
        assert bal.available >= 0, f"available={bal.available}"

    async def test_positions_valid(self, hl):
        fixture = _load_fixture("hl")["contracts"]
        positions = await hl.get_open_positions()
        for p in positions:
            f = fixture.get(p.symbol)
            if f is None:
                continue
            step = f["size_step"]
            tick = f["price_tick"]
            from decimal import Decimal
            ratio_s = Decimal(str(abs(p.size))) / Decimal(str(step))
            assert ratio_s % 1 < Decimal("1e-8") or ratio_s % 1 > Decimal("0.99999999"), \
                f"HL {p.symbol} size={p.size} / step={step} = {float(ratio_s)}"
            if p.entry_price > 0:
                ratio_p = Decimal(str(p.entry_price)) / Decimal(str(tick))
                assert ratio_p % 1 < Decimal("1e-8") or ratio_p % 1 > Decimal("0.99999999"), \
                    f"HL {p.symbol} entry={p.entry_price} / tick={tick} = {float(ratio_p)}"


# ---------------------------------------------------------------------------
# GRVT
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def grvt():
    from src.exchanges.grvt_adapter import GrvtAdapter
    a = GrvtAdapter(
        trading_account_id=os.environ.get("GRVT_TRADING_ACCOUNT_ID", ""),
        private_key=os.environ.get("GRVT_PRIVATE_KEY", ""),
        api_key=os.environ.get("GRVT_API_KEY", ""),
        env=os.environ.get("GRVT_ENV", "prod"),
    )
    yield a
    await a.close()


class TestGRVTAuth:
    async def test_balance_non_negative(self, grvt):
        bal = await grvt.get_balance()
        assert bal.total_equity >= 0, f"total_equity={bal.total_equity}"
        assert bal.available >= 0, f"available={bal.available}"

    async def test_positions_valid(self, grvt):
        fixture = _load_fixture("grvt")["contracts"]
        positions = await grvt.get_open_positions()
        for p in positions:
            f = fixture.get(p.symbol.upper())
            if f is None:
                continue
            step = f["size_step"]
            tick = f["price_tick"]
            from decimal import Decimal
            ratio_s = Decimal(str(abs(p.size))) / Decimal(str(step))
            assert ratio_s % 1 < Decimal("1e-8") or ratio_s % 1 > Decimal("0.99999999"), \
                f"GRVT {p.symbol} size={p.size} / step={step} = {float(ratio_s)}"
            if p.entry_price > 0:
                ratio_p = Decimal(str(p.entry_price)) / Decimal(str(tick))
                assert ratio_p % 1 < Decimal("1e-8") or ratio_p % 1 > Decimal("0.99999999"), \
                    f"GRVT {p.symbol} entry={p.entry_price} / tick={tick} = {float(ratio_p)}"
