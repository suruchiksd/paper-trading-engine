"""Centralized order lifecycle validation."""

from datetime import datetime

from ..models import Order, OrderStatus


class InvalidOrderTransition(ValueError):
    pass


VALID_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.NEW: frozenset({OrderStatus.OPEN, OrderStatus.REJECTED, OrderStatus.CANCELLED}),
    OrderStatus.OPEN: frozenset(
        {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset({OrderStatus.FILLED, OrderStatus.CANCELLED}),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
}


def transition_order(
    order: Order,
    status: OrderStatus,
    timestamp: datetime,
    *,
    reason: str | None = None,
) -> None:
    if status not in VALID_TRANSITIONS[order.status]:
        raise InvalidOrderTransition(f"cannot transition order from {order.status} to {status}")
    order.status = status
    order.updated_at = timestamp
    if status is OrderStatus.REJECTED:
        order.rejection_reason = reason
    elif status is OrderStatus.CANCELLED:
        order.cancellation_reason = reason

