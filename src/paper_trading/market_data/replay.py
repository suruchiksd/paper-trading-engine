"""Deterministic, non-wall-clock historical replay feed."""

from __future__ import annotations

from datetime import date, datetime
from typing import Iterable, Sequence

from ..events import CandleEvent
from ..models import Instrument
from .interfaces import HistoricalCandleRepository


class HistoricalReplayFeed:
    def __init__(
        self,
        repository: HistoricalCandleRepository,
        instruments: Sequence[Instrument],
        start_date: date,
        end_date: date | None = None,
    ) -> None:
        if not instruments:
            raise ValueError("at least one instrument is required")
        self.repository = repository
        self.instruments = tuple(instruments)
        self.start_date = start_date
        self.end_date = end_date or start_date
        self._events: list[CandleEvent] = []
        self._running = False

    @property
    def session_start(self) -> datetime | None:
        return self._events[0].candle.timestamp if self._events else None

    @property
    def session_end(self) -> datetime | None:
        return self._events[-1].candle.timestamp if self._events else None

    def start(self) -> None:
        candles = [
            candle
            for instrument in self.instruments
            for candle in self.repository.load_candles(instrument, self.start_date, self.end_date)
        ]
        candles.sort(key=lambda candle: (candle.timestamp, candle.instrument.key))
        self._events = [CandleEvent(candle) for candle in candles]
        self._running = True

    def events(self) -> Iterable[CandleEvent]:
        if not self._running:
            raise RuntimeError("feed must be started before reading events")
        yield from self._events

    def stop(self) -> None:
        self._running = False

