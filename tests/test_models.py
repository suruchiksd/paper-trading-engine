from dataclasses import asdict
from datetime import UTC, datetime

import pytest

from paper_trading.models import Candle, Instrument, Order, OrderType, Position, Side


NOW = datetime(2026, 1, 1, tzinfo=UTC)
RELIANCE = Instrument("NSE", "RELIANCE", instrument_token=738561)


def test_candle_rejects_impossible_ohlc() -> None:
    with pytest.raises(ValueError, match="high"):
        Candle(RELIANCE, "5minute", NOW, open=100, high=99, low=98, close=99, volume=10)


def test_order_rejects_overfill() -> None:
    with pytest.raises(ValueError, match="filled_quantity"):
        Order(RELIANCE, Side.BUY, OrderType.MARKET, 10, NOW, filled_quantity=11)


def test_position_quantity_and_unrealized_pnl_are_signed_consistently() -> None:
    long = Position(RELIANCE, quantity=10, average_price=100, last_price=105)
    short = Position(RELIANCE, quantity=-10, average_price=100, last_price=95)
    assert long.unrealized_pnl == 50
    assert short.unrealized_pnl == 50
    with pytest.raises(ValueError, match="flat"):
        Position(RELIANCE, quantity=0, average_price=100)


def test_models_and_string_enums_serialize_cleanly() -> None:
    order = Order(RELIANCE, Side.BUY, OrderType.LIMIT, 1, NOW, limit_price=100)
    serialized = asdict(order)
    assert serialized["side"] == "BUY"
    assert serialized["instrument"]["tradingsymbol"] == "RELIANCE"

