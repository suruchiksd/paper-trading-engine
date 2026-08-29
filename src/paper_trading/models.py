"""Domain models shared across engine components."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping
from uuid import uuid4


def _id() -> str:
    return uuid4().hex


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_MARKET = "STOP_MARKET"
    STOP_LIMIT = "STOP_LIMIT"


class OrderStatus(StrEnum):
    NEW = "NEW"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class SignalDirection(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    EXIT = "EXIT"


@dataclass(frozen=True, slots=True)
class Instrument:
    exchange: str
    tradingsymbol: str
    instrument_token: int | None = None
    instrument_type: str = "EQ"
    tick_size: float = 0.05
    lot_size: int = 1

    def __post_init__(self) -> None:
        if not self.exchange or not self.tradingsymbol:
            raise ValueError("exchange and tradingsymbol are required")
        if self.tick_size <= 0 or self.lot_size <= 0:
            raise ValueError("tick_size and lot_size must be positive")

    @property
    def key(self) -> str:
        return f"{self.exchange}:{self.tradingsymbol}"


@dataclass(frozen=True, slots=True)
class Tick:
    instrument: Instrument
    exchange_timestamp: datetime
    received_timestamp: datetime
    last_price: float
    volume: int | None = None
    best_bid: float | None = None
    best_ask: float | None = None

    def __post_init__(self) -> None:
        if self.last_price <= 0:
            raise ValueError("last_price must be positive")
        if self.volume is not None and self.volume < 0:
            raise ValueError("volume cannot be negative")
        if self.best_bid is not None and self.best_ask is not None and self.best_bid > self.best_ask:
            raise ValueError("best_bid cannot exceed best_ask")


@dataclass(frozen=True, slots=True)
class Candle:
    instrument: Instrument
    interval: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    completed: bool = True

    def __post_init__(self) -> None:
        if not self.interval:
            raise ValueError("interval is required")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("OHLC prices must be positive")
        if self.high < max(self.open, self.low, self.close):
            raise ValueError("high must be at least open, low, and close")
        if self.low > min(self.open, self.high, self.close):
            raise ValueError("low must be at most open, high, and close")
        if self.volume < 0:
            raise ValueError("volume cannot be negative")


@dataclass(frozen=True, slots=True)
class Signal:
    strategy_id: str
    strategy_version: str
    instrument: Instrument
    timestamp: datetime
    direction: SignalDirection
    suggested_entry: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    reason: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    signal_id: str = field(default_factory=_id)


@dataclass(slots=True)
class Order:
    instrument: Instrument
    side: Side
    order_type: OrderType
    quantity: int
    created_at: datetime
    signal_id: str | None = None
    limit_price: float | None = None
    stop_price: float | None = None
    status: OrderStatus = OrderStatus.NEW
    filled_quantity: int = 0
    average_fill_price: float | None = None
    order_id: str = field(default_factory=_id)
    broker_order_id: str | None = None
    rejection_reason: str | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if not 0 <= self.filled_quantity <= self.quantity:
            raise ValueError("filled_quantity must be between zero and quantity")
        if self.order_type in {OrderType.LIMIT, OrderType.STOP_LIMIT} and self.limit_price is None:
            raise ValueError("limit_price is required for limit orders")
        if self.order_type in {OrderType.STOP_MARKET, OrderType.STOP_LIMIT} and self.stop_price is None:
            raise ValueError("stop_price is required for stop orders")


@dataclass(frozen=True, slots=True)
class Fill:
    order_id: str
    instrument: Instrument
    side: Side
    quantity: int
    price: float
    timestamp: datetime
    signal_id: str | None = None
    fill_id: str = field(default_factory=_id)
    fees: float = 0.0

    def __post_init__(self) -> None:
        if self.quantity <= 0 or self.price <= 0:
            raise ValueError("fill quantity and price must be positive")
        if self.fees < 0:
            raise ValueError("fees cannot be negative")


@dataclass(slots=True)
class Position:
    instrument: Instrument
    quantity: int = 0
    average_price: float = 0.0
    realized_pnl: float = 0.0
    last_price: float | None = None
    position_id: str = field(default_factory=_id)

    def __post_init__(self) -> None:
        if self.quantity != 0 and self.average_price <= 0:
            raise ValueError("non-flat positions require a positive average_price")
        if self.quantity == 0 and self.average_price != 0:
            raise ValueError("flat positions must have zero average_price")

    @property
    def unrealized_pnl(self) -> float:
        if self.last_price is None or self.quantity == 0:
            return 0.0
        return (self.last_price - self.average_price) * self.quantity


@dataclass(frozen=True, slots=True)
class Trade:
    instrument: Instrument
    side: Side
    quantity: int
    entry_price: float
    entry_timestamp: datetime
    entry_fill_ids: tuple[str, ...]
    signal_id: str | None = None
    trade_id: str = field(default_factory=_id)
    exit_price: float | None = None
    exit_timestamp: datetime | None = None
    exit_fill_ids: tuple[str, ...] = ()
    realized_pnl: float | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0 or self.entry_price <= 0:
            raise ValueError("trade quantity and entry_price must be positive")
        if bool(self.exit_price is None) != bool(self.exit_timestamp is None):
            raise ValueError("exit_price and exit_timestamp must be provided together")

    @property
    def is_open(self) -> bool:
        return self.exit_timestamp is None


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    timestamp: datetime
    cash: float
    equity: float
    realized_pnl: float
    unrealized_pnl: float
    positions: Mapping[str, int] = field(default_factory=dict)

