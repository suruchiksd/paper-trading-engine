from datetime import date, datetime
from zoneinfo import ZoneInfo

from paper_trading.engine import TradingEngine
from paper_trading.market_data import HistoricalReplayFeed
from paper_trading.models import Candle, Instrument, Signal, SignalDirection
from paper_trading.strategies import BaseStrategy

IST = ZoneInfo("Asia/Kolkata")


class MemoryRepository:
    def __init__(self, candles: dict[str, list[Candle]]) -> None:
        self.candles = candles

    def load_candles(self, instrument: Instrument, start_date: date, end_date: date | None = None) -> list[Candle]:
        return self.candles[instrument.tradingsymbol]


class RecordingStrategy(BaseStrategy):
    strategy_id = "recording"
    version = "test"

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def on_session_start(self, timestamp: datetime):
        self.calls.append(("start", timestamp))
        return ()

    def on_candle(self, candle: Candle):
        self.calls.append(("candle", (candle.timestamp, candle.instrument.tradingsymbol)))
        if len([call for call in self.calls if call[0] == "candle"]) == 1:
            return (
                Signal(
                    self.strategy_id,
                    self.version,
                    candle.instrument,
                    candle.timestamp,
                    SignalDirection.LONG,
                    reason="dispatch test only",
                ),
            )
        return ()

    def on_session_end(self, timestamp: datetime):
        self.calls.append(("end", timestamp))
        return ()


def _candle(instrument: Instrument, minute: int) -> Candle:
    return Candle(
        instrument,
        "1minute",
        datetime(2026, 7, 31, 9, minute, tzinfo=IST),
        100,
        101,
        99,
        100,
    )


def test_multi_symbol_replay_and_engine_lifecycle_are_deterministic() -> None:
    reliance = Instrument("NSE", "RELIANCE", 1)
    sbin = Instrument("NSE", "SBIN", 2)
    repository = MemoryRepository(
        {"RELIANCE": [_candle(reliance, 16), _candle(reliance, 15)], "SBIN": [_candle(sbin, 15)]}
    )
    feed = HistoricalReplayFeed(repository, [sbin, reliance], date(2026, 7, 31))
    strategy = RecordingStrategy()
    engine = TradingEngine([strategy])

    assert engine.replay(feed) == 3
    assert strategy.calls == [
        ("start", datetime(2026, 7, 31, 9, 15, tzinfo=IST)),
        ("candle", (datetime(2026, 7, 31, 9, 15, tzinfo=IST), "RELIANCE")),
        ("candle", (datetime(2026, 7, 31, 9, 15, tzinfo=IST), "SBIN")),
        ("candle", (datetime(2026, 7, 31, 9, 16, tzinfo=IST), "RELIANCE")),
        ("end", datetime(2026, 7, 31, 9, 16, tzinfo=IST)),
    ]
    assert len(engine.emitted_signals) == 1

