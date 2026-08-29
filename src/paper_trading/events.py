"""Small event envelopes; dispatch infrastructure is intentionally deferred."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from .models import Candle, Fill, Order, Signal, Tick


@dataclass(frozen=True, slots=True)
class TickEvent:
    tick: Tick


@dataclass(frozen=True, slots=True)
class CandleEvent:
    candle: Candle


@dataclass(frozen=True, slots=True)
class SignalEvent:
    signal: Signal


@dataclass(frozen=True, slots=True)
class OrderEvent:
    order: Order


@dataclass(frozen=True, slots=True)
class FillEvent:
    fill: Fill


@dataclass(frozen=True, slots=True)
class RiskEvent:
    timestamp: datetime
    approved: bool
    reason: str
    signal_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

