"""Local tick-driven paper execution. This module has no Zerodha client imports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Sequence
from uuid import uuid4

from ..market_data.health import FeedHealthSnapshot, FeedState
from ..market_data.quotes import LiveQuote, LiveQuoteStore
from ..models import Candle, Fill, Order, OrderStatus, OrderType, Side, Tick
from .broker import BrokerUpdate
from .costs import NoTransactionCosts, TransactionCostModel


@dataclass(frozen=True, slots=True)
class LiveExecutionConfig:
    max_quote_age_seconds: float = 5
    allow_ltp_fallback: bool = True
    market_slippage_bps: float = 0
    stop_market_slippage_bps: float = 0


class LivePaperBroker:
    """Full-fill local paper broker driven only by newly observed normalized ticks."""

    def __init__(
        self,
        quote_store: LiveQuoteStore,
        health_provider: Callable[[], FeedHealthSnapshot],
        clock: Callable[[], datetime],
        *,
        config: LiveExecutionConfig | None = None,
        cost_model: TransactionCostModel | None = None,
    ) -> None:
        self.quote_store = quote_store
        self.health_provider = health_provider
        self.clock = clock
        self.config = config or LiveExecutionConfig()
        self.cost_model = cost_model or NoTransactionCosts()
        self._orders: dict[str, Order] = {}

    def submit_order(self, order: Order) -> None:
        if order.status is not OrderStatus.NEW:
            raise ValueError("live paper broker accepts only NEW orders")
        if order.order_type is OrderType.STOP_LIMIT:
            raise NotImplementedError("STOP_LIMIT live paper simulation is deferred")
        self._orders[order.order_id] = order

    def restore_order(self, order: Order) -> None:
        if order.status not in {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}:
            raise ValueError("only active local paper orders may be restored")
        if order.order_id in self._orders:
            raise ValueError(f"paper order already restored: {order.order_id}")
        self._orders[order.order_id] = order

    def cancel_order(self, order_id: str) -> None:
        if order_id not in self._orders:
            raise KeyError(f"active local paper order not found: {order_id}")
        del self._orders[order_id]

    def active_orders(self) -> Sequence[Order]:
        return tuple(self._orders.values())

    def process_candle(self, candle: Candle) -> BrokerUpdate:
        return BrokerUpdate()

    def process_tick(self, tick: Tick) -> BrokerUpdate:
        if not self._tick_is_fresh(tick):
            return BrokerUpdate()
        quote = self.quote_store.get(tick.instrument.key)
        if quote is None or quote.received_timestamp != tick.received_timestamp:
            return BrokerUpdate()
        fills: list[Fill] = []
        for order in tuple(self._orders.values()):
            if order.instrument.key != tick.instrument.key:
                continue
            result = self._fill_reference(order, quote)
            if result is None:
                continue
            reference, fallback, slippage_bps = result
            price = self._slip(reference, order.side, slippage_bps)
            if order.order_type is OrderType.LIMIT:
                assert order.limit_price is not None
                price = min(price, order.limit_price) if order.side is Side.BUY else max(
                    price, order.limit_price
                )
            quantity = order.quantity - order.filled_quantity
            execution = {
                "source": "LIVE_PAPER",
                "observed_last": quote.last_price,
                "observed_bid": quote.best_bid,
                "observed_ask": quote.best_ask,
                "quote_exchange_timestamp": (
                    quote.exchange_timestamp.isoformat() if quote.exchange_timestamp else None
                ),
                "quote_received_timestamp": quote.received_timestamp.isoformat(),
                "slippage_bps": slippage_bps,
                "fallback": fallback,
            }
            order.metadata = {**order.metadata, "live_execution": execution}
            fills.append(
                Fill(
                    order.order_id,
                    order.instrument,
                    order.side,
                    quantity,
                    price,
                    quote.received_timestamp,
                    signal_id=order.signal_id,
                    strategy_id=order.strategy_id,
                    strategy_version=order.strategy_version,
                    fill_id=f"PAPER-FILL-{uuid4().hex}",
                    fees=self.cost_model.calculate(order.side, quantity, price),
                )
            )
            self._orders.pop(order.order_id)
        return BrokerUpdate(tuple(fills))

    def _tick_is_fresh(self, tick: Tick) -> bool:
        health = self.health_provider()
        if not health.connected or health.state is not FeedState.HEALTHY:
            return False
        return (self.clock() - tick.received_timestamp).total_seconds() <= self.config.max_quote_age_seconds

    def _fill_reference(
        self, order: Order, quote: LiveQuote
    ) -> tuple[float, str | None, float] | None:
        executable = quote.best_ask if order.side is Side.BUY else quote.best_bid
        fallback: str | None = None
        if executable is None:
            if not self.config.allow_ltp_fallback:
                return None
            executable = quote.last_price
            fallback = "LAST_PRICE_NO_DEPTH"
        if order.order_type is OrderType.MARKET:
            return executable, fallback, self.config.market_slippage_bps
        if order.order_type is OrderType.LIMIT:
            assert order.limit_price is not None
            touched = executable <= order.limit_price if order.side is Side.BUY else executable >= order.limit_price
            return (executable, fallback, self.config.market_slippage_bps) if touched else None
        if order.order_type is OrderType.STOP_MARKET:
            assert order.stop_price is not None
            triggered = quote.last_price >= order.stop_price if order.side is Side.BUY else quote.last_price <= order.stop_price
            return (executable, fallback, self.config.stop_market_slippage_bps) if triggered else None
        return None

    @staticmethod
    def _slip(price: float, side: Side, basis_points: float) -> float:
        return price * (1 + (1 if side is Side.BUY else -1) * basis_points / 10_000)
