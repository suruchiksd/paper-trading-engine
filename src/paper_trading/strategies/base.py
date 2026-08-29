"""Broker-independent strategy contract."""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Iterable

from ..models import Candle, Fill, Signal, Tick


class BaseStrategy(ABC):
    """A strategy observes domain data and emits intent as signals only."""

    @property
    @abstractmethod
    def strategy_id(self) -> str:
        """Stable identifier used to attribute signals and results."""

    @property
    @abstractmethod
    def version(self) -> str:
        """Version of the strategy rules and parameters."""

    def on_session_start(self, timestamp: datetime) -> Iterable[Signal]:
        return ()

    def on_tick(self, tick: Tick) -> Iterable[Signal]:
        return ()

    def on_candle(self, candle: Candle) -> Iterable[Signal]:
        return ()

    def on_fill(self, fill: Fill) -> Iterable[Signal]:
        return ()

    def on_session_end(self, timestamp: datetime) -> Iterable[Signal]:
        return ()
