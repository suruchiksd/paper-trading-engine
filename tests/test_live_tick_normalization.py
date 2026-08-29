from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from paper_trading.market_data import LiveTickError, UnknownInstrumentTokenError, ZerodhaTickNormalizer
from paper_trading.models import Instrument

IST = ZoneInfo("Asia/Kolkata")
RELIANCE = Instrument("NSE", "RELIANCE", 738561)


def test_normalizes_equity_tick_timestamp_volume_and_depth() -> None:
    received = datetime(2026, 8, 31, 3, 45, 1, tzinfo=UTC)
    normalizer = ZerodhaTickNormalizer([RELIANCE])
    tick = normalizer.normalize(
        {
            "instrument_token": 738561,
            "last_price": 1300.5,
            "volume_traded": 12345,
            "exchange_timestamp": datetime(2026, 8, 31, 9, 15),
            "depth": {
                "buy": [{"price": 1300.45, "quantity": 10}],
                "sell": [{"price": 1300.55, "quantity": 12}],
            },
        },
        received,
    )
    assert tick.instrument is RELIANCE
    assert tick.exchange_timestamp == datetime(2026, 8, 31, 9, 15, tzinfo=IST)
    assert tick.received_timestamp == datetime(2026, 8, 31, 9, 15, 1, tzinfo=IST)
    assert tick.volume == 12345
    assert tick.best_bid == 1300.45
    assert tick.best_ask == 1300.55


def test_missing_optional_timestamp_depth_and_volume_remain_explicit() -> None:
    tick = ZerodhaTickNormalizer([RELIANCE]).normalize(
        {"instrument_token": 738561, "last_price": 1300},
        datetime(2026, 8, 31, 9, 15, tzinfo=IST),
    )
    assert tick.exchange_timestamp is None
    assert tick.volume is None
    assert tick.best_bid is None
    assert tick.best_ask is None


@pytest.mark.parametrize(
    "raw",
    [
        {"instrument_token": 738561},
        {"last_price": 100},
        {"instrument_token": 738561, "last_price": "bad"},
        {"instrument_token": 738561, "last_price": 100, "volume_traded": "bad"},
    ],
)
def test_malformed_required_fields_raise(raw) -> None:
    with pytest.raises(LiveTickError):
        ZerodhaTickNormalizer([RELIANCE]).normalize(raw)


def test_unknown_token_is_rejected() -> None:
    with pytest.raises(UnknownInstrumentTokenError):
        ZerodhaTickNormalizer([RELIANCE]).normalize(
            {"instrument_token": 999, "last_price": 100}
        )

