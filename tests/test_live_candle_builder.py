from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from paper_trading.market_data import (
    OneMinuteCandleBuilder,
    TickDisposition,
    VolumeRegressionPolicy,
)
from paper_trading.models import Candle, Instrument, Tick

IST = ZoneInfo("Asia/Kolkata")
RELIANCE = Instrument("NSE", "RELIANCE", 1)
SBIN = Instrument("NSE", "SBIN", 2)
NIFTY = Instrument("NSE", "NIFTY 50", 3, tick_size=0, lot_size=0, segment="INDICES")


def tick(instrument, minute, second, price, volume=100):
    timestamp = datetime(2026, 8, 31, 9, minute, second, tzinfo=IST)
    return Tick(instrument, timestamp, timestamp, price, volume)


def test_builds_ohlc_and_completed_candle_at_rollover() -> None:
    builder = OneMinuteCandleBuilder()
    assert builder.process(tick(RELIANCE, 15, 1, 100, 100)).completed_candle is None
    builder.process(tick(RELIANCE, 15, 20, 105, 120))
    builder.process(tick(RELIANCE, 15, 40, 98, 140))
    builder.process(tick(RELIANCE, 15, 59, 102, 150))
    completed = builder.process(tick(RELIANCE, 16, 1, 103, 160)).completed_candle
    assert isinstance(completed, Candle)
    assert completed.timestamp == datetime(2026, 8, 31, 9, 15, tzinfo=IST)
    assert (completed.open, completed.high, completed.low, completed.close) == (100, 105, 98, 102)
    assert completed.volume == 50
    assert completed.interval == "1minute"
    assert completed.completed


def test_multiple_instruments_are_independent() -> None:
    builder = OneMinuteCandleBuilder()
    builder.process(tick(RELIANCE, 15, 1, 100))
    assert builder.process(tick(SBIN, 16, 1, 200)).completed_candle is None
    reliance = builder.process(tick(RELIANCE, 16, 2, 101, 110)).completed_candle
    assert reliance is not None and reliance.instrument is RELIANCE


def test_duplicate_and_out_of_order_ticks_are_dropped() -> None:
    builder = OneMinuteCandleBuilder()
    original = tick(RELIANCE, 15, 10, 100)
    builder.process(original)
    assert builder.process(original).disposition is TickDisposition.DUPLICATE
    assert builder.process(tick(RELIANCE, 15, 5, 200)).disposition is TickDisposition.OUT_OF_ORDER
    builder.process(tick(RELIANCE, 16, 0, 101, 110))
    assert builder.process(tick(RELIANCE, 15, 59, 99, 105)).disposition is TickDisposition.OUT_OF_ORDER


def test_missing_minute_is_observable_but_not_fabricated() -> None:
    builder = OneMinuteCandleBuilder()
    builder.process(tick(RELIANCE, 15, 1, 100, 100))
    update = builder.process(tick(RELIANCE, 17, 1, 102, 150))
    assert update.completed_candle.timestamp.minute == 15
    assert update.missing_minutes == 1
    builder.process(tick(RELIANCE, 17, 40, 102.5, 155))
    next_update = builder.process(tick(RELIANCE, 18, 1, 103, 160))
    assert next_update.completed_candle.timestamp.minute == 17
    assert next_update.completed_candle.volume == 5


def test_index_without_volume_produces_zero_volume() -> None:
    builder = OneMinuteCandleBuilder()
    builder.process(tick(NIFTY, 15, 1, 25000, None))
    completed = builder.process(tick(NIFTY, 16, 1, 25001, None)).completed_candle
    assert completed.volume == 0


def test_volume_regression_policy_is_explicit() -> None:
    reset = OneMinuteCandleBuilder(VolumeRegressionPolicy.RESET_BASELINE)
    reset.process(tick(RELIANCE, 15, 1, 100, 1000))
    update = reset.process(tick(RELIANCE, 15, 2, 101, 10))
    assert update.disposition is TickDisposition.VOLUME_RESET
    drop = OneMinuteCandleBuilder(VolumeRegressionPolicy.DROP_TICK)
    drop.process(tick(RELIANCE, 15, 1, 100, 1000))
    assert drop.process(tick(RELIANCE, 15, 2, 101, 10)).disposition is TickDisposition.VOLUME_RESET


def test_timer_finalizes_only_observed_candle_exactly_once() -> None:
    builder = OneMinuteCandleBuilder()
    builder.process(tick(RELIANCE, 15, 10, 100, 100))
    builder.process(tick(RELIANCE, 15, 50, 101, 110))
    due = datetime(2026, 8, 31, 9, 16, 2, tzinfo=IST)
    completed = builder.finalize_due(due, delay_seconds=2)
    assert len(completed) == 1
    assert completed[0].timestamp.minute == 15
    assert builder.finalize_due(due + timedelta(seconds=10), delay_seconds=2) == ()
    assert builder.flush() == ()  # no artificial 09:16 candle
