"""Deterministic candle-based paper broker."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..models import Candle, Fill, Order, OrderStatus, OrderType, Side
from .broker import BrokerUpdate
from .costs import NoTransactionCosts, TransactionCostModel


class IntrabarAmbiguityPolicy(StrEnum):
    CONSERVATIVE = "CONSERVATIVE"
    OPTIMISTIC = "OPTIMISTIC"
    ERROR = "ERROR"


class AmbiguousExecutionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SlippageConfig:
    market_bps: float = 0.0
    stop_market_bps: float = 0.0

    def __post_init__(self) -> None:
        if self.market_bps < 0 or self.stop_market_bps < 0:
            raise ValueError("slippage basis points cannot be negative")


class PaperBroker:
    """Full-fill candle simulator with strict next-timestamp eligibility."""

    def __init__(
        self,
        *,
        slippage: SlippageConfig | None = None,
        ambiguity_policy: IntrabarAmbiguityPolicy = IntrabarAmbiguityPolicy.CONSERVATIVE,
        cost_model: TransactionCostModel | None = None,
    ) -> None:
        self.slippage = slippage or SlippageConfig()
        self.ambiguity_policy = ambiguity_policy
        self.cost_model = cost_model or NoTransactionCosts()
        self._orders: dict[str, Order] = {}

    def submit_order(self, order: Order) -> None:
        if order.status is not OrderStatus.NEW:
            raise ValueError("broker accepts only NEW orders")
        if order.order_type is OrderType.STOP_LIMIT:
            raise NotImplementedError("STOP_LIMIT simulation is deferred")
        self._orders[order.order_id] = order

    def cancel_order(self, order_id: str) -> None:
        if order_id not in self._orders:
            raise KeyError(f"active broker order not found: {order_id}")
        del self._orders[order_id]

    def restore_order(self, order: Order) -> None:
        if order.status not in {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}:
            raise ValueError("only active orders may be restored into the broker")
        if order.order_id in self._orders:
            raise ValueError(f"broker order already restored: {order.order_id}")
        self._orders[order.order_id] = order

    def active_orders(self) -> tuple[Order, ...]:
        return tuple(self._orders.values())

    def process_candle(self, candle: Candle) -> BrokerUpdate:
        eligible = [
            order
            for order in self._orders.values()
            if order.status is OrderStatus.OPEN
            and order.instrument.key == candle.instrument.key
            and (order.eligible_after or order.created_at) < candle.timestamp
        ]
        candidates = [(order, self._fill_price(order, candle)) for order in eligible]
        candidates = [(order, price) for order, price in candidates if price is not None]
        selected, cancelled = self._resolve_oco(candidates)
        fills: list[Fill] = []
        for order, price in selected:
            fills.append(
                Fill(
                    order_id=order.order_id,
                    instrument=order.instrument,
                    side=order.side,
                    quantity=order.quantity - order.filled_quantity,
                    price=price,
                    timestamp=candle.timestamp,
                    signal_id=order.signal_id,
                    strategy_id=order.strategy_id,
                    strategy_version=order.strategy_version,
                    fees=self.cost_model.calculate(
                        order.side, order.quantity - order.filled_quantity, price
                    ),
                )
            )
        for order, _ in selected:
            self._orders.pop(order.order_id, None)
        for order_id in cancelled:
            self._orders.pop(order_id, None)
        return BrokerUpdate(tuple(fills), tuple(cancelled))

    def _fill_price(self, order: Order, candle: Candle) -> float | None:
        if order.order_type is OrderType.MARKET:
            return self._slip(candle.open, order.side, self.slippage.market_bps)
        if order.order_type is OrderType.LIMIT:
            assert order.limit_price is not None
            if order.side is Side.BUY:
                if candle.open <= order.limit_price:
                    return min(self._slip(candle.open, order.side, self.slippage.market_bps), order.limit_price)
                if candle.low <= order.limit_price:
                    return order.limit_price
            else:
                if candle.open >= order.limit_price:
                    return max(self._slip(candle.open, order.side, self.slippage.market_bps), order.limit_price)
                if candle.high >= order.limit_price:
                    return order.limit_price
            return None
        if order.order_type is OrderType.STOP_MARKET:
            assert order.stop_price is not None
            if order.side is Side.SELL and candle.low <= order.stop_price:
                base = candle.open if candle.open <= order.stop_price else order.stop_price
                return self._slip(base, order.side, self.slippage.stop_market_bps)
            if order.side is Side.BUY and candle.high >= order.stop_price:
                base = candle.open if candle.open >= order.stop_price else order.stop_price
                return self._slip(base, order.side, self.slippage.stop_market_bps)
            return None
        raise NotImplementedError(f"unsupported paper order type: {order.order_type}")

    def _resolve_oco(
        self, candidates: list[tuple[Order, float]]
    ) -> tuple[list[tuple[Order, float]], list[str]]:
        independent = [(order, price) for order, price in candidates if order.oco_group_id is None]
        groups: dict[str, list[tuple[Order, float]]] = {}
        for order, price in candidates:
            if order.oco_group_id is not None:
                groups.setdefault(order.oco_group_id, []).append((order, price))
        cancelled: list[str] = []
        for group_id, touched in groups.items():
            all_group = [order for order in self._orders.values() if order.oco_group_id == group_id]
            if len(touched) > 1:
                if self.ambiguity_policy is IntrabarAmbiguityPolicy.ERROR:
                    raise AmbiguousExecutionError(f"multiple OCO orders touched in candle: {group_id}")
                side = touched[0][0].side
                adverse_reverse = side is Side.BUY
                choose_max = (
                    self.ambiguity_policy is IntrabarAmbiguityPolicy.CONSERVATIVE
                ) == adverse_reverse
                chosen = (max if choose_max else min)(touched, key=lambda item: item[1])
            else:
                chosen = touched[0]
            independent.append(chosen)
            cancelled.extend(order.order_id for order in all_group if order.order_id != chosen[0].order_id)
        return independent, cancelled

    @staticmethod
    def _slip(price: float, side: Side, basis_points: float) -> float:
        direction = 1 if side is Side.BUY else -1
        return price * (1 + direction * basis_points / 10_000)
