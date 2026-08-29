"""Top-level orchestration boundary for the future event-driven engine."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Sequence

from .events import CandleEvent, SignalEvent
from .market_data.interfaces import MarketDataFeed
from .models import Signal
from .strategies import BaseStrategy


@dataclass(slots=True)
class TradingEngine:
    """Minimal composition root; execution behavior is intentionally absent.

    Intended flow:
    market data -> tick/candle -> strategy -> signal -> risk -> order -> broker
    -> fill -> portfolio.

    Strategies are held behind ``BaseStrategy`` and never receive a broker. Future
    phases will inject explicit market-data, risk, broker, portfolio, and storage
    interfaces here.
    """

    strategies: Sequence[BaseStrategy] = field(default_factory=tuple)
    emitted_signals: list[SignalEvent] = field(default_factory=list, init=False)

    def run(self) -> None:
        raise NotImplementedError("engine orchestration is planned for a later phase")

    def replay(self, feed: MarketDataFeed) -> int:
        """Synchronously dispatch one replay feed; no risk or execution occurs."""
        self.emitted_signals.clear()
        dispatched = 0
        feed.start()
        try:
            start = getattr(feed, "session_start", None)
            end = getattr(feed, "session_end", None)
            if start is None or end is None:
                return 0
            self._collect(strategy.on_session_start(start) for strategy in self.strategies)
            for event in feed.events():
                if isinstance(event, CandleEvent):
                    dispatched += 1
                    self._collect(strategy.on_candle(event.candle) for strategy in self.strategies)
            self._collect(strategy.on_session_end(end) for strategy in self.strategies)
            return dispatched
        finally:
            feed.stop()

    def _collect(self, groups: Iterable[Iterable[Signal]]) -> None:
        for signals in groups:
            for signal in signals:
                self.emitted_signals.append(SignalEvent(signal))
