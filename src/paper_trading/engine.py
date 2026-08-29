"""Top-level orchestration boundary for the future event-driven engine."""

from dataclasses import dataclass, field
from typing import Sequence

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

    def run(self) -> None:
        raise NotImplementedError("engine orchestration is planned for a later phase")

