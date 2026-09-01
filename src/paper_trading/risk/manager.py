"""Deterministic portfolio-level pre-trade risk checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..models import Order, OrderRequest, OrderStatus, Side
from ..portfolio import Portfolio


@dataclass(frozen=True, slots=True)
class RiskLimits:
    maximum_positions: int
    maximum_daily_loss_pct: float
    maximum_position_exposure_pct: float


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    reason: str
    approved_quantity: int


class RiskManager:
    def __init__(self, limits: RiskLimits) -> None:
        if limits.maximum_positions <= 0:
            raise ValueError("maximum_positions must be positive")
        self.limits = limits

    def evaluate(
        self,
        request: OrderRequest,
        portfolio: Portfolio,
        active_orders: Sequence[Order] = (),
    ) -> RiskDecision:
        if request.quantity <= 0:
            return RiskDecision(False, "quantity must be positive", 0)
        price = self._reference_price(request, portfolio)
        if price is None:
            return RiskDecision(False, "a positive reference price is required", 0)

        key = request.instrument.key
        position = portfolio.positions.get(key)
        current_quantity = position.quantity if position else 0
        pending = [
            order
            for order in active_orders
            if order.status in {OrderStatus.NEW, OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}
        ]
        pending_keys = {order.instrument.key for order in pending}
        open_keys = {item.instrument.key for item in portfolio.open_positions}
        if current_quantity == 0 and key not in pending_keys and len(open_keys | pending_keys) >= self.limits.maximum_positions:
            return RiskDecision(False, "maximum open positions reached", 0)

        signed = request.quantity if request.side is Side.BUY else -request.quantity
        pending_signed = sum(
            (order.quantity - order.filled_quantity) * (1 if order.side is Side.BUY else -1)
            for order in pending
            if order.instrument.key == key
        )
        effective_quantity = current_quantity + pending_signed
        opens_exposure = (
            effective_quantity == 0
            or (effective_quantity > 0) == (signed > 0)
            or abs(signed) > abs(effective_quantity)
        )
        daily_loss_limit = portfolio.starting_capital * self.limits.maximum_daily_loss_pct / 100
        if opens_exposure and portfolio.total_pnl <= -daily_loss_limit:
            return RiskDecision(False, "maximum daily loss reached", 0)
        projected_quantity = current_quantity + pending_signed + signed
        exposure_limit = portfolio.equity * self.limits.maximum_position_exposure_pct / 100
        if abs(projected_quantity) * price > exposure_limit:
            return RiskDecision(False, "maximum position exposure exceeded", 0)

        short_to_cover = max(0, -current_quantity - max(0, pending_signed))
        opening_buy_quantity = max(0, request.quantity - short_to_cover) if request.side is Side.BUY else 0
        pending_buy_notional = sum(
            (order.quantity - order.filled_quantity)
            * (order.limit_price or order.stop_price or price)
            for order in pending
            if order.side is Side.BUY
        )
        if opening_buy_quantity * price + pending_buy_notional > portfolio.cash:
            return RiskDecision(False, "insufficient available cash", 0)
        return RiskDecision(True, "approved", request.quantity)

    @staticmethod
    def _reference_price(request: OrderRequest, portfolio: Portfolio) -> float | None:
        price = request.reference_price or request.limit_price or request.stop_price
        if price is None:
            position = portfolio.positions.get(request.instrument.key)
            price = position.last_price if position else None
        return price if price is not None and price > 0 else None
