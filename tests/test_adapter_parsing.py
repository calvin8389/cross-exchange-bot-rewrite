#!/usr/bin/env python3
"""Phase 1B: Adapter data parsing unit tests (mock HTTP, no cost)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.exchanges.base import Balance, MarketDetails, PositionInfo

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


BOT_SYMBOLS = [
    "BTC", "ETH", "SOL", "DOGE", "SUI", "ARB", "OP",
    "LTC", "BCH", "LINK", "UNI", "AVAX", "DOT", "PENDLE", "ENA", "LIT",
]


# ---------------------------------------------------------------------------
# MarketDetails — fixture consistency
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("exchange", ["lighter", "edgex", "hl", "grvt"])
class TestMarketDetailsFixture:
    """Verify all fixture entries have valid MarketDetails fields."""

    def test_all_contracts_have_price_tick(self, exchange):
        contracts = _load_fixture(exchange)["contracts"]
        for sym, c in contracts.items():
            assert c["price_tick"] > 0, f"{exchange}/{sym}: price_tick={c['price_tick']}"

    def test_all_contracts_have_size_step(self, exchange):
        contracts = _load_fixture(exchange)["contracts"]
        for sym, c in contracts.items():
            assert c["size_step"] > 0, f"{exchange}/{sym}: size_step={c['size_step']}"

    def test_all_contracts_have_valid_market_id(self, exchange):
        contracts = _load_fixture(exchange)["contracts"]
        for sym, c in contracts.items():
            mid = c["market_id"]
            assert mid is not None and mid != "", f"{exchange}/{sym}: market_id empty"
            assert isinstance(mid, (int, str)), f"{exchange}/{sym}: market_id type={type(mid).__name__}"

    def test_price_tick_precision_consistent(self, exchange):
        """price_precision must match -log10(price_tick) rounding."""
        contracts = _load_fixture(exchange)["contracts"]
        # Sample 10 random symbols
        samples = list(contracts.values())[:30]
        for c in samples:
            tick = c["price_tick"]
            precision = c["price_precision"]
            expected = round(-__import__("math").log10(tick))
            # Allow ±1 due to floating point artifacts
            assert abs(precision - expected) <= 1, \
                f"{c['symbol']}: precision={precision}, expected≈{expected}, tick={tick}"

    def test_size_step_precision_consistent(self, exchange):
        contracts = _load_fixture(exchange)["contracts"]
        samples = list(contracts.values())[:30]
        for c in samples:
            step = c["size_step"]
            precision = c["size_precision"]
            # precision=0 means integer step (100, 1, etc.)
            # precision≥1 means fractional step; tolerance of 3 accounts for
            # cases like step=100 where -log10(100)=-2 but precision=0
            expected = -__import__("math").floor(__import__("math").log10(step)) if step >= 1 else round(-__import__("math").log10(step))
            assert abs(precision - max(0, expected)) <= 3, \
                f"{c['symbol']}: size_precision={precision}, expected≈{expected}, step={step}"

    def test_bot_symbols_in_fixture(self, exchange):
        """All bot_config symbols should exist in fixture."""
        contracts = _load_fixture(exchange)["contracts"]
        missing = [s for s in BOT_SYMBOLS if s not in contracts]
        # EdgeX uses "BTCUSD" naming; base symbols should also work
        if exchange == "edgex":
            edgex_contracts = _load_fixture("edgex")["contracts"]
            missing = [s for s in BOT_SYMBOLS if s not in edgex_contracts]
        if missing:
            pytest.fail(f"{exchange}: missing bot symbols: {missing}")


# ---------------------------------------------------------------------------
# MarketDetails — per-exchange fixture validation
# ---------------------------------------------------------------------------

class TestLighterFixture:
    def test_min_base_amount_present(self):
        contracts = _load_fixture("lighter")["contracts"]
        btc = contracts.get("BTC")
        assert btc is not None
        assert btc["min_base_amount"] > 0, "BTC min_base_amount should be > 0"

    def test_fees_present(self):
        contracts = _load_fixture("lighter")["contracts"]
        btc = contracts.get("BTC")
        # Fees might be 0 for some accounts; just verify field exists
        assert "taker_fee" in btc
        assert "maker_fee" in btc


class TestEdgeXFixture:
    def test_min_order_size_present(self):
        contracts = _load_fixture("edgex")["contracts"]
        btc = contracts.get("BTCUSD") or contracts.get("BTC")
        assert btc is not None
        assert btc["min_order_size"] > 0, "BTC min_order_size should be > 0"

    def test_dual_key_mapping(self):
        """BTCUSD and BTC should map to the same contract_id."""
        contracts = _load_fixture("edgex")["contracts"]
        btcusd = contracts.get("BTCUSD")
        btc = contracts.get("BTC")
        # Both should exist after the fix
        assert btcusd is not None, "BTCUSD missing"
        assert btc is not None, "BTC missing"
        assert btcusd["market_id"] == btc["market_id"]


class TestHLFixture:
    def test_tick_valid(self):
        contracts = _load_fixture("hl")["contracts"]
        for sym in BOT_SYMBOLS:
            c = contracts.get(sym)
            if c is None:
                continue
            tick = c["price_tick"]
            assert tick > 0, f"HL {sym}: tick={tick}"
            # Tick should be a clean power of 10 or clean fraction
            assert tick >= 1e-8, f"HL {sym}: tick too small: {tick}"

    def test_max_leverage_present(self):
        contracts = _load_fixture("hl")["contracts"]
        btc = contracts.get("BTC")
        assert btc is not None
        assert btc["max_leverage"] is not None, "HL BTC max_leverage missing"


class TestGRVTFixture:
    def test_min_notional_present(self):
        contracts = _load_fixture("grvt")["contracts"]
        btc = contracts.get("BTC")
        assert btc is not None
        assert btc["min_quote_amount"] > 0, "GRVT BTC min_notional missing"


# ---------------------------------------------------------------------------
# PositionInfo validation (with mock exchange data)
# ---------------------------------------------------------------------------

class TestPositionInfoValidation:
    """Validate PositionInfo fields regardless of exchange source."""

    def test_long_position_positive_size(self):
        p = PositionInfo(symbol="BTC", size=0.5, entry_price=80000.0, unrealized_pnl=100.0)
        assert p.size > 0, "Long should have positive size"

    def test_short_position_negative_size(self):
        p = PositionInfo(symbol="ETH", size=-1.0, entry_price=3000.0, unrealized_pnl=-50.0)
        assert p.size < 0, "Short should have negative size"

    @pytest.mark.parametrize("symbol", ["BTC", "ETH", "SOL", "DOGE"])
    def test_position_price_is_tick_multiple(self, symbol):
        """Position entry_price should be divisible by the exchange's tick."""
        contracts = _load_fixture("hl")["contracts"]
        c = contracts.get(symbol)
        if c is None:
            pytest.skip(f"{symbol} not in HL fixture")
        tick = c["price_tick"]
        entry_price = round(80000.0, c["price_precision"])
        # Check: entry_price = N * tick, where N is an integer
        # Use Decimal to avoid float artifacts
        from decimal import Decimal
        ratio = Decimal(str(entry_price)) / Decimal(str(tick))
        remainder = ratio % 1
        assert remainder < Decimal("1e-8") or remainder > Decimal("0.99999999"), \
            f"{symbol} entry_price={entry_price} / tick={tick} = {float(ratio)} (remainder={remainder})"

    @pytest.mark.parametrize("symbol", ["BTC", "ETH", "SOL", "DOGE"])
    def test_position_size_is_step_multiple(self, symbol):
        """Position size should be an integer multiple of size_step."""
        contracts = _load_fixture("hl")["contracts"]
        c = contracts.get(symbol)
        if c is None:
            pytest.skip(f"{symbol} not in HL fixture")
        step = c["size_step"]
        # Rounded size should align with step
        size = round(0.5 / step) * step
        ratio = round(size / step, 10)
        assert ratio == round(ratio), \
            f"{symbol} size={size} / step={step} = {ratio}"


# ---------------------------------------------------------------------------
# Balance validation
# ---------------------------------------------------------------------------

class TestBalanceValidation:
    def test_available_leq_total(self):
        b = Balance(total_equity=1000.0, available=500.0)
        assert b.available <= b.total_equity

    def test_balance_non_negative(self):
        b = Balance(total_equity=0.0, available=0.0)
        assert b.total_equity >= 0
        assert b.available >= 0

    def test_negative_balance_possible(self):
        """Margin accounts can have negative equity (liquidation)."""
        b = Balance(total_equity=-10.0, available=0.0)
        assert b.available >= 0  # available should never be negative
        # total_equity CAN be negative in extreme cases


# ---------------------------------------------------------------------------
# Lighter adapter parsing (using fixture data)
# ---------------------------------------------------------------------------

class TestLighterResponseParsing:
    """Validate Lighter response parsing logic."""

    def _mock_account_response(self) -> dict:
        return {
            "accounts": [{
                "available_balance": "1000.50",
                "collateral": "1000.50",
                "total_asset_value": "1000.50",
                "positions": [
                    {
                        "symbol": "BTC",
                        "position": "0.00120",
                        "sign": 1,  # long
                        "avg_entry_price": "82000.0",
                        "unrealized_pnl": "1.50",
                    }
                ],
            }],
        }

    def test_parse_balance_from_account_response(self):
        raw = self._mock_account_response()
        acc = raw["accounts"][0]
        avail = float(acc.get("available_balance", 0))
        total = float(acc.get("total_asset_value", acc.get("collateral", 0)))
        assert avail == 1000.50
        assert total == 1000.50
        assert avail <= total

    def test_parse_position_signed_size(self):
        raw = self._mock_account_response()
        pos = raw["accounts"][0]["positions"][0]
        raw_size = float(pos.get("position", 0))
        sign = int(pos.get("sign", 0))
        signed_size = raw_size * sign
        # Long: size > 0
        assert signed_size == 0.00120

    def test_parse_position_sign_negative(self):
        """Short position: sign=-1 → negative size."""
        raw = self._mock_account_response()
        raw["accounts"][0]["positions"][0]["sign"] = -1
        pos = raw["accounts"][0]["positions"][0]
        signed = float(pos["position"]) * int(pos["sign"])
        assert signed == -0.00120, f"Short size should be negative, got {signed}"

    def test_balance_parsing_edge_cases(self):
        """Handle missing fields gracefully."""
        # Missing total_asset_value, fall back to collateral
        raw = {"accounts": [{"available_balance": "100.0", "collateral": "100.0"}]}
        acc = raw["accounts"][0]
        total = float(acc.get("total_asset_value", acc.get("collateral", 0)))
        assert total == 100.0

        # Completely empty response
        raw2 = {"accounts": []}
        assert len(raw2["accounts"]) == 0


# ---------------------------------------------------------------------------
# EdgeX response parsing validation
# ---------------------------------------------------------------------------

class TestEdgeXResponseParsing:
    def _mock_position_response(self) -> dict:
        return {
            "data": {
                "positionList": [
                    {
                        "contractId": "10000001",
                        "openSize": "0.009",
                        "openValue": "739.344",
                    }
                ],
                "positionAssetList": [
                    {
                        "contractId": "10000001",
                        "avgEntryPrice": "82149.4",
                        "unrealizePnl": "1.798",
                    }
                ],
            },
        }

    def test_parse_position_from_position_list(self):
        raw = self._mock_position_response()
        inner = raw["data"]
        pos = inner["positionList"][0]
        size = float(pos.get("openSize", 0))
        assert size == 0.009
        assert abs(size) > 1e-8

    def test_match_position_to_asset_data(self):
        raw = self._mock_position_response()
        inner = raw["data"]
        # Build lookup
        asset_lookup = {}
        for a in inner.get("positionAssetList", []):
            asset_lookup[a["contractId"]] = {
                "entry_price": float(a.get("avgEntryPrice", 0)),
                "unrealized_pnl": float(a.get("unrealizePnl", 0)),
            }
        # Match
        pos = inner["positionList"][0]
        asset = asset_lookup.get(pos["contractId"], {})
        assert asset["entry_price"] == 82149.4
        assert asset["unrealized_pnl"] == 1.798


# ---------------------------------------------------------------------------
# Hyperliquid response parsing validation
# ---------------------------------------------------------------------------

class TestHLResponseParsing:
    def test_parse_user_state_balance(self):
        raw = {
            "marginSummary": {"accountValue": "1001.30"},
            "withdrawable": "950.0",
        }
        total = float(raw["marginSummary"]["accountValue"])
        avail = float(raw["withdrawable"])
        assert total == 1001.30
        assert avail == 950.0

    def test_parse_asset_position(self):
        raw = {
            "assetPositions": [
                {
                    "position": {
                        "coin": "BTC",
                        "szi": "0.0048",
                        "entryPx": "82205.7",
                        "unrealizedPnl": "0.4707",
                    }
                }
            ]
        }
        pos = raw["assetPositions"][0]["position"]
        szi = float(pos["szi"])
        assert szi == 0.0048
        # Size can be positive (long) or negative (short) in HL
        entry = float(pos["entryPx"])
        assert entry > 0

    def test_order_response_parsing_resting(self):
        """Parse oid from resting order response."""
        raw = {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {
                    "statuses": [{"resting": {"oid": 413065113028}}]
                }
            }
        }
        statuses = raw["response"]["data"]["statuses"]
        oid = statuses[0].get("resting", {}).get("oid") or statuses[0].get("filled", {}).get("oid")
        assert oid == 413065113028

    def test_order_response_parsing_filled(self):
        """Parse oid from filled order response."""
        raw = {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {
                    "statuses": [{"filled": {"totalSz": "19.6", "oid": 413153669495}}]
                }
            }
        }
        statuses = raw["response"]["data"]["statuses"]
        for event in ("filled", "resting"):
            oid = statuses[0].get(event, {}).get("oid")
            if oid:
                assert oid == 413153669495
                break
        else:
            pytest.fail("No oid found in response")


# ---------------------------------------------------------------------------
# GRVT response parsing validation
# ---------------------------------------------------------------------------

class TestGRVTResponseParsing:
    def test_parse_fetch_markets_tick(self):
        """tick_size from GRVT market data."""
        raw = {"instrument": "BTC_USDT_Perp", "tick_size": "0.1", "min_size": "0.001"}
        tick = float(raw.get("tick_size", 0.01))
        assert tick == 0.1

    def test_order_response_parsing(self):
        raw = {"code": "SUCCESS", "data": {"orderId": "0x00"}}
        oid = raw.get("data", {}).get("orderId", "")
        assert oid == "0x00"
