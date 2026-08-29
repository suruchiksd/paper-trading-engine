"""Narrow synchronous boundaries shared by replay and future live feeds."""

from datetime import date
from typing import Iterable, Protocol, TypeAlias, runtime_checkable

from ..events import CandleEvent, TickEvent
from ..models import Candle, Instrument

MarketDataEvent: TypeAlias = TickEvent | CandleEvent


@runtime_checkable
class MarketDataFeed(Protocol):
    def start(self) -> None: ...

    def events(self) -> Iterable[MarketDataEvent]: ...

    def stop(self) -> None: ...


@runtime_checkable
class HistoricalCandleRepository(Protocol):
    def load_candles(
        self, instrument: Instrument, start_date: date, end_date: date | None = None
    ) -> list[Candle]: ...


@runtime_checkable
class InstrumentRepository(Protocol):
    def resolve(self, tradingsymbol: str, *, segment: str | None = None) -> Instrument: ...

