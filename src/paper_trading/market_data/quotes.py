"""Thread-safe transient latest quote state; never persisted as raw ticks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from threading import RLock

from ..models import Instrument, Tick


@dataclass(frozen=True, slots=True)
class LiveQuote:
    instrument: Instrument
    last_price: float
    best_bid: float | None
    best_ask: float | None
    exchange_timestamp: datetime | None
    received_timestamp: datetime
    cumulative_volume: int | None


class LiveQuoteStore:
    def __init__(self) -> None:
        self._quotes: dict[str, LiveQuote] = {}
        self._lock = RLock()

    def update(self, tick: Tick) -> LiveQuote:
        quote = LiveQuote(
            tick.instrument,
            tick.last_price,
            tick.best_bid,
            tick.best_ask,
            tick.exchange_timestamp,
            tick.received_timestamp,
            tick.volume,
        )
        with self._lock:
            current = self._quotes.get(tick.instrument.key)
            if current is None or tick.received_timestamp >= current.received_timestamp:
                self._quotes[tick.instrument.key] = quote
                return quote
            return current

    def get(self, instrument_key: str) -> LiveQuote | None:
        with self._lock:
            return self._quotes.get(instrument_key)

    def snapshot(self) -> dict[str, LiveQuote]:
        with self._lock:
            return dict(self._quotes)
