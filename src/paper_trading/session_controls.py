"""Clock-driven controls for manual LIVE_PAPER sessions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Callable

from .engine import TradingEngine
from .models import Order, OrderRequest, OrderStatus, OrderType, Side, TradeIntent
from .risk import RiskDecision


def parse_hhmm(value: str) -> time:
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise ValueError(f"invalid HH:MM time: {value}") from exc


@dataclass(frozen=True, slots=True)
class SessionControlConfig:
    new_entry_cutoff: time
    intraday_square_off_time: time
    session_end_time: time
    reject_new_entries_after_cutoff: bool = True
    auto_square_off_intraday: bool = False
    cancel_open_intraday_orders_at_eod: bool = True


class SessionController:
    """Validates intent/cutoff and creates local EOD closing requests."""

    def __init__(
        self,
        engine: TradingEngine,
        config: SessionControlConfig,
        clock: Callable[[], datetime],
        upstream_check: Callable[[OrderRequest], RiskDecision | None] | None = None,
    ) -> None:
        self.engine = engine
        self.config = config
        self.clock = clock
        self.upstream_check = upstream_check
        self.square_off_attempted = False
        self.eod_orders_cancelled = False

    def check(self, request: OrderRequest) -> RiskDecision | None:
        if self.upstream_check is not None:
            decision = self.upstream_check(request)
            if decision is not None:
                return decision
        opening = self._opens_exposure(request)
        position = self.engine.portfolio.positions.get(request.instrument.key)
        if opening and position is not None and position.quantity and position.intent is not request.intent:
            return RiskDecision(False, "HOLDING_INTENT_CONFLICT", 0)
        if (
            opening
            and self.config.reject_new_entries_after_cutoff
            and self.clock().time() >= self.config.new_entry_cutoff
        ):
            return RiskDecision(False, "NEW_ENTRY_CUTOFF_REACHED", 0)
        return None

    def _opens_exposure(self, request: OrderRequest) -> bool:
        position = self.engine.portfolio.positions.get(request.instrument.key)
        current = position.quantity if position else 0
        pending = sum(
            (order.quantity - order.filled_quantity) * (1 if order.side is Side.BUY else -1)
            for order in self.engine.order_manager.broker.active_orders()
            if order.instrument.key == request.instrument.key
        )
        effective = current + pending
        signed = request.quantity if request.side is Side.BUY else -request.quantity
        return effective == 0 or (effective > 0) == (signed > 0) or abs(signed) > abs(effective)

    def square_off_intraday(self) -> tuple[tuple[RiskDecision, Order], ...]:
        results = []
        for position in tuple(self.engine.portfolio.open_positions):
            if position.intent is not TradeIntent.INTRADAY:
                continue
            request = OrderRequest(
                position.instrument,
                Side.SELL if position.quantity > 0 else Side.BUY,
                OrderType.MARKET,
                abs(position.quantity),
                self.clock(),
                reference_price=position.last_price or position.average_price,
                metadata={"source": "INTRADAY_SQUARE_OFF"},
                intent=TradeIntent.INTRADAY,
            )
            decision, order = self.engine.submit_order(request)
            results.append((decision, order))
            if not decision.approved and self.engine.persistence is not None:
                self.engine.persistence.record_audit(
                    f"INTRADAY_SQUARE_OFF_FAILED_{decision.reason}", self.clock(),
                    {"instrument": position.instrument.key},
                )
        self.square_off_attempted = True
        return tuple(results)

    def on_clock(self) -> tuple[tuple[RiskDecision, Order], ...]:
        if not self.eod_orders_cancelled and self.clock().time() >= self.config.session_end_time:
            self.cancel_open_intraday_orders()
            self.eod_orders_cancelled = True
        if (
            self.config.auto_square_off_intraday
            and not self.square_off_attempted
            and self.clock().time() >= self.config.intraday_square_off_time
        ):
            return self.square_off_intraday()
        return ()

    def cancel_open_intraday_orders(self) -> tuple[Order, ...]:
        if not self.config.cancel_open_intraday_orders_at_eod:
            return ()
        cancelled = []
        for order in tuple(self.engine.order_manager.broker.active_orders()):
            if order.intent is TradeIntent.INTRADAY and order.status in {
                OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED
            }:
                cancelled.append(self.engine.cancel_order(order.order_id, self.clock(), "intraday EOD"))
        return tuple(cancelled)

    def completion_blockers(self) -> tuple[str, ...]:
        blockers = []
        if any(position.intent is TradeIntent.INTRADAY for position in self.engine.portfolio.open_positions):
            blockers.append("OPEN_INTRADAY_POSITIONS")
        if any(position.intent is TradeIntent.SWING for position in self.engine.portfolio.open_positions):
            blockers.append("OPEN_SWING_POSITIONS_REQUIRE_STOPPED_RESUME")
        if self.engine.order_manager.broker.active_orders():
            blockers.append("OPEN_ORDERS")
        return tuple(blockers)
