"""Order ownership and broker-fill reconciliation."""

from __future__ import annotations

from datetime import datetime
from typing import Callable
from uuid import uuid4

from ..models import Fill, Order, OrderRequest, OrderStatus
from .broker import Broker
from .orders import transition_order


class OrderManager:
    def __init__(self, broker: Broker, order_id_factory: Callable[[], str] | None = None) -> None:
        self.broker = broker
        self.order_id_factory = order_id_factory or (lambda: uuid4().hex)
        self.orders: dict[str, Order] = {}
        self.fills: list[Fill] = []

    def restore(self, orders: list[Order], fills: list[Fill]) -> None:
        if self.orders or self.fills or self.broker.active_orders():
            raise ValueError("order manager must be empty before recovery")
        self.orders = {order.order_id: order for order in orders}
        self.fills = list(fills)
        for order in orders:
            if order.status in {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}:
                self.broker.restore_order(order)

    def submit(self, request: OrderRequest) -> Order:
        order = Order(
            instrument=request.instrument,
            side=request.side,
            order_type=request.order_type,
            quantity=request.quantity,
            created_at=request.timestamp,
            eligible_after=request.eligible_after or request.timestamp,
            signal_id=request.signal_id,
            strategy_id=request.strategy_id,
            strategy_version=request.strategy_version,
            limit_price=request.limit_price,
            stop_price=request.stop_price,
            oco_group_id=request.oco_group_id,
            metadata=request.metadata,
            order_id=self.order_id_factory(),
        )
        self.orders[order.order_id] = order
        try:
            self.broker.submit_order(order)
        except Exception as exc:
            transition_order(order, OrderStatus.REJECTED, request.timestamp, reason=str(exc))
            return order
        transition_order(order, OrderStatus.OPEN, request.timestamp)
        return order

    def register_rejection(self, request: OrderRequest, reason: str) -> Order:
        order = Order(
            request.instrument,
            request.side,
            request.order_type,
            request.quantity,
            request.timestamp,
            eligible_after=request.eligible_after or request.timestamp,
            signal_id=request.signal_id,
            strategy_id=request.strategy_id,
            strategy_version=request.strategy_version,
            limit_price=request.limit_price,
            stop_price=request.stop_price,
            oco_group_id=request.oco_group_id,
            metadata=request.metadata,
            order_id=self.order_id_factory(),
        )
        self.orders[order.order_id] = order
        transition_order(order, OrderStatus.REJECTED, request.timestamp, reason=reason)
        return order

    def cancel(self, order_id: str, timestamp: datetime, reason: str = "cancelled by user") -> Order:
        order = self.orders[order_id]
        self.broker.cancel_order(order_id)
        transition_order(order, OrderStatus.CANCELLED, timestamp, reason=reason)
        return order

    def process_candle(self, candle) -> tuple[Fill, ...]:
        update = self.broker.process_candle(candle)
        return self._apply_update(update, candle.timestamp)

    def process_tick(self, tick) -> tuple[Fill, ...]:
        process_tick = getattr(self.broker, "process_tick", None)
        if process_tick is None:
            return ()
        update = process_tick(tick)
        return self._apply_update(update, tick.received_timestamp)

    def _apply_update(self, update, timestamp: datetime) -> tuple[Fill, ...]:
        for fill in update.fills:
            order = self.orders[fill.order_id]
            if order.status not in {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}:
                raise ValueError(f"cannot apply fill to order in status {order.status}")
            new_quantity = order.filled_quantity + fill.quantity
            if new_quantity > order.quantity:
                raise ValueError("fill would exceed order quantity")
            previous_value = (order.average_fill_price or 0.0) * order.filled_quantity
            order.filled_quantity = new_quantity
            order.average_fill_price = (previous_value + fill.price * fill.quantity) / new_quantity
            status = OrderStatus.FILLED if new_quantity == order.quantity else OrderStatus.PARTIALLY_FILLED
            transition_order(order, status, fill.timestamp)
            self.fills.append(fill)
        for order_id in update.cancelled_order_ids:
            order = self.orders[order_id]
            transition_order(order, OrderStatus.CANCELLED, timestamp, reason="OCO sibling filled")
        return update.fills
