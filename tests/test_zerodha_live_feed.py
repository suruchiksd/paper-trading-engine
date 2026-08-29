from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from paper_trading.engine import TradingEngine
from paper_trading.events import CandleEvent, TickEvent
from paper_trading.market_data import FeedState, LiveFeedConnectionError, ZerodhaLiveFeed
from paper_trading.models import Instrument
from paper_trading.strategies import BaseStrategy

IST = ZoneInfo("Asia/Kolkata")
RELIANCE = Instrument("NSE", "RELIANCE", 738561)


class FakeTicker:
    MODE_FULL = "full"
    MODE_QUOTE = "quote"
    MODE_LTP = "ltp"

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.subscriptions = []
        self.modes = []
        self.connected = False
        self.closed = False
        self.retry_stopped = False
        self.on_ticks = None
        self.on_connect = None
        self.on_close = None
        self.on_error = None
        self.on_reconnect = None
        self.on_noreconnect = None

    def connect(self, threaded=False):
        self.connected = threaded
        self.on_connect(self, {})

    def subscribe(self, tokens):
        self.subscriptions.append(list(tokens))

    def set_mode(self, mode, tokens):
        self.modes.append((mode, list(tokens)))

    def close(self):
        self.closed = True

    def stop_retry(self):
        self.retry_stopped = True

    def emit(self, ticks):
        self.on_ticks(self, ticks)


def raw(second, price, volume):
    return {
        "instrument_token": 738561,
        "last_price": price,
        "volume_traded": volume,
        "exchange_timestamp": datetime(2026, 8, 31, 9, 15 + second // 60, second % 60, tzinfo=IST),
        "depth": {"buy": [{"price": price - 0.05}], "sell": [{"price": price + 0.05}]},
    }


def make_feed(clock=None):
    fake = FakeTicker()
    feed = ZerodhaLiveFeed(
        "key",
        "token",
        [RELIANCE],
        ticker_factory=lambda *args, **kwargs: fake,
        clock=clock,
    )
    return feed, fake


def test_connect_subscribe_mode_reconnect_and_stop() -> None:
    feed, fake = make_feed()
    feed.start()
    assert fake.connected
    assert fake.subscriptions == [[738561]]
    assert fake.modes == [("full", [738561])]
    fake.on_close(fake, 1006, "temporary")
    assert feed.health_snapshot().state is FeedState.DISCONNECTED
    fake.on_reconnect(fake, 1)
    fake.on_connect(fake, {})
    assert fake.subscriptions == [[738561], [738561]]
    assert feed.health_snapshot().reconnect_count == 1
    feed.stop()
    assert fake.retry_stopped and fake.closed
    assert feed.health_snapshot().state is FeedState.STOPPED


def test_callbacks_normalize_queue_and_build_completed_candle() -> None:
    feed, fake = make_feed()
    feed.start()
    fake.emit([raw(1, 100, 100), raw(59, 102, 150), raw(60, 103, 160)])
    events = iter(feed.events())
    assert isinstance(next(events), TickEvent)
    assert isinstance(next(events), TickEvent)
    completed = next(events)
    assert isinstance(completed, CandleEvent)
    assert isinstance(next(events), TickEvent)
    assert completed.candle.close == 102
    assert completed.candle.volume == 50
    assert feed.health_snapshot().ticks_received == 3
    feed.stop()


def test_malformed_tick_is_dropped_and_authentication_error_surfaces() -> None:
    feed, fake = make_feed()
    feed.start()
    fake.emit([{"instrument_token": 738561}])
    assert feed.health_snapshot().malformed_ticks == 1
    fake.on_error(fake, 403, "invalid token")
    with pytest.raises(LiveFeedConnectionError, match="refresh KITE_ACCESS_TOKEN"):
        list(feed.events())
    assert fake.retry_stopped
    assert feed.health_snapshot().state is FeedState.ERROR


class RecordingStrategy(BaseStrategy):
    strategy_id = "live-recorder"
    version = "test"

    def __init__(self):
        self.ticks = []
        self.candles = []

    def on_tick(self, tick):
        self.ticks.append(tick)
        return ()

    def on_candle(self, candle):
        self.candles.append(candle)
        return ()


def test_synthetic_live_ticks_reach_read_only_engine_once() -> None:
    feed, fake = make_feed()
    strategy = RecordingStrategy()
    engine = TradingEngine([strategy])
    assert engine.order_manager is None and engine.portfolio is None
    feed.start()
    fake.emit([raw(1, 100, 100), raw(60, 101, 110)])
    events = iter(feed.events())
    for _ in range(3):
        engine.dispatch_market_event(next(events))
    feed.stop()
    assert len(strategy.ticks) == 2
    assert len(strategy.candles) == 1
    assert strategy.candles[0].timestamp == datetime(2026, 8, 31, 9, 15, tzinfo=IST)
    assert strategy.candles[0].interval == "1minute"
