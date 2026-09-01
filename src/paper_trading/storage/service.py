"""Session lifecycle, persistence recording, and fill-ledger recovery."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from math import isclose
from typing import Iterable, Mapping
from uuid import uuid4

from ..execution import Broker, OrderManager
from ..models import Fill, Order, OrderStatus, SessionStatus, Trade, TradingSession
from ..portfolio import Portfolio
from ..risk import RiskDecision
from .repositories import SQLiteStore

logger = logging.getLogger(__name__)


class InvalidSessionTransition(ValueError):
    pass


class RecoveryConsistencyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RecoveredSessionRuntime:
    session: TradingSession
    portfolio: Portfolio
    order_manager: OrderManager


class SessionRecorder:
    """Engine-facing persistence hook bound to one durable session."""

    def __init__(self, store: SQLiteStore, session_id: str) -> None:
        self.store = store
        self.session_id = session_id

    def record_order_decision(
        self, order: Order, decision: RiskDecision, timestamp: datetime
    ) -> None:
        self.store.record_order_decision(self.session_id, order, decision, timestamp)

    def record_order_update(self, order: Order, timestamp: datetime) -> None:
        self.store.record_order_update(self.session_id, order, timestamp)

    def record_execution(
        self,
        orders: Iterable[Order],
        fills: Iterable[Fill],
        trades: Iterable[Trade],
        portfolio: Portfolio,
        timestamp: datetime,
    ) -> None:
        self.store.record_execution(
            self.session_id, orders, fills, trades, portfolio, timestamp
        )

    def checkpoint_positions(self, portfolio: Portfolio, timestamp: datetime) -> None:
        self.store.checkpoint_positions(self.session_id, portfolio, timestamp)

    def record_audit(
        self, event_type: str, timestamp: datetime, metadata: Mapping[str, object] | None = None
    ) -> None:
        self.store.record_journal_event(self.session_id, timestamp, event_type, metadata)


class SessionService:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def create(
        self,
        trading_date: date,
        mode: str,
        starting_capital: float,
        *,
        strategy_context: Mapping[str, object] | None = None,
        timestamp: datetime | None = None,
    ) -> TradingSession:
        now = timestamp or datetime.now(UTC)
        session = TradingSession(
            session_id=uuid4().hex,
            trading_date=trading_date,
            mode=mode,
            started_at=None,
            ended_at=None,
            status=SessionStatus.CREATED,
            starting_capital=starting_capital,
            strategy_context=strategy_context or {},
            created_at=now,
            updated_at=now,
        )
        self.store.insert_session(session)
        logger.info("session created: %s", session.session_id)
        return session

    def start(self, session_id: str, timestamp: datetime | None = None) -> TradingSession:
        session = self.store.get_session(session_id)
        if session.status is not SessionStatus.CREATED:
            raise InvalidSessionTransition(f"cannot start session in status {session.status}")
        now = timestamp or datetime.now(UTC)
        updated = replace(session, status=SessionStatus.RUNNING, started_at=now, updated_at=now)
        self.store.update_session(updated, "SESSION_STARTED")
        logger.info("session started: %s", session_id)
        return updated

    def stop(self, session_id: str, timestamp: datetime | None = None) -> TradingSession:
        return self._finish(session_id, SessionStatus.STOPPED, "SESSION_STOPPED", timestamp)

    def complete(
        self,
        session_id: str,
        portfolio: Portfolio,
        timestamp: datetime | None = None,
    ) -> TradingSession:
        now = timestamp or datetime.now(UTC)
        self.store.record_snapshot(session_id, portfolio, now, "SESSION_END")
        return self._finish(session_id, SessionStatus.COMPLETED, "SESSION_COMPLETED", now)

    def fail(self, session_id: str, timestamp: datetime | None = None) -> TradingSession:
        return self._finish(session_id, SessionStatus.FAILED, "SESSION_FAILED", timestamp)

    def _finish(
        self,
        session_id: str,
        status: SessionStatus,
        event: str,
        timestamp: datetime | None,
    ) -> TradingSession:
        session = self.store.get_session(session_id)
        if session.status is not SessionStatus.RUNNING:
            raise InvalidSessionTransition(f"cannot finish session in status {session.status}")
        now = timestamp or datetime.now(UTC)
        updated = replace(session, status=status, ended_at=now, updated_at=now)
        self.store.update_session(updated, event)
        logger.info("session %s: %s", status.lower(), session_id)
        return updated

    def resume(self, session_id: str, broker: Broker) -> RecoveredSessionRuntime:
        session = self.store.get_session(session_id)
        if session.status in {SessionStatus.COMPLETED, SessionStatus.FAILED}:
            raise InvalidSessionTransition(f"cannot resume terminal session {session.status}")
        orders = self.store.load_orders(session_id)
        fills = self.store.load_fills(session_id)
        self._validate_orders(orders, fills)
        portfolio = self.restore_portfolio(session_id)

        order_manager = OrderManager(broker)
        order_manager.restore(orders, fills)
        now = datetime.now(UTC)
        resumed = replace(
            session,
            status=SessionStatus.RUNNING,
            started_at=session.started_at or now,
            ended_at=None,
            updated_at=now,
        )
        self.store.update_session(resumed, "SESSION_RESUMED")
        logger.info(
            "session resumed: %s (%d fills, %d active orders)",
            session_id,
            len(fills),
            len(broker.active_orders()),
        )
        return RecoveredSessionRuntime(resumed, portfolio, order_manager)

    def restore_portfolio(self, session_id: str) -> Portfolio:
        """Rebuild authoritative accounting without changing session lifecycle."""
        session = self.store.get_session(session_id)
        orders = self.store.load_orders(session_id)
        fills = self.store.load_fills(session_id)
        self._validate_orders(orders, fills)
        stored_trades = self.store.load_trades(session_id)
        portfolio = Portfolio(session.starting_capital)
        for fill in fills:
            portfolio.process_fill(fill)
        self._reconcile_trades(portfolio.closed_trades, stored_trades)
        portfolio.closed_trades = stored_trades
        checkpoints = self.store.load_position_checkpoints(session_id)
        for key, checkpoint in checkpoints.items():
            position = portfolio.positions.get(key)
            if position is None:
                raise RecoveryConsistencyError(f"position checkpoint {key} has no fill-ledger position")
            if position.quantity != checkpoint["quantity"] or not isclose(
                position.average_price, checkpoint["average_price"], rel_tol=1e-12, abs_tol=1e-9
            ):
                raise RecoveryConsistencyError(f"position checkpoint {key} disagrees with fill ledger")
            if checkpoint["last_price"] is not None:
                portfolio.mark(key, checkpoint["last_price"])
        return portfolio

    def recorder(self, session_id: str) -> SessionRecorder:
        self.store.get_session(session_id)
        return SessionRecorder(self.store, session_id)

    @staticmethod
    def _validate_orders(orders: list[Order], fills: list[Fill]) -> None:
        by_id = {order.order_id: order for order in orders}
        quantities: dict[str, int] = {}
        values: dict[str, float] = {}
        for fill in fills:
            order = by_id.get(fill.order_id)
            if order is None:
                raise RecoveryConsistencyError(f"fill {fill.fill_id} has no order")
            if fill.instrument.key != order.instrument.key or fill.side is not order.side:
                raise RecoveryConsistencyError(f"fill {fill.fill_id} mismatches its order")
            quantities[fill.order_id] = quantities.get(fill.order_id, 0) + fill.quantity
            values[fill.order_id] = values.get(fill.order_id, 0.0) + fill.quantity * fill.price
        for order in orders:
            filled = quantities.get(order.order_id, 0)
            if filled != order.filled_quantity:
                raise RecoveryConsistencyError(
                    f"order {order.order_id} records {order.filled_quantity} filled but ledger has {filled}"
                )
            if filled:
                expected_average = values[order.order_id] / filled
                if order.average_fill_price is None or not isclose(
                    order.average_fill_price, expected_average, rel_tol=1e-12
                ):
                    raise RecoveryConsistencyError(f"order {order.order_id} average fill mismatch")
            if order.status is OrderStatus.FILLED and filled != order.quantity:
                raise RecoveryConsistencyError(f"FILLED order {order.order_id} lacks complete fills")
            if order.status in {OrderStatus.OPEN, OrderStatus.NEW} and filled:
                raise RecoveryConsistencyError(f"active order {order.order_id} has unrepresented partial state")

    @staticmethod
    def _reconcile_trades(derived: list[Trade], stored: list[Trade]) -> None:
        if len(derived) != len(stored):
            raise RecoveryConsistencyError(
                f"closed trade count mismatch: derived {len(derived)}, stored {len(stored)}"
            )
        key = lambda trade: (trade.exit_timestamp, trade.exit_fill_ids, trade.instrument.key)
        derived = sorted(derived, key=key)
        stored = sorted(stored, key=key)
        fields = (
            "instrument",
            "side",
            "quantity",
            "entry_timestamp",
            "exit_timestamp",
            "entry_fill_ids",
            "exit_fill_ids",
        )
        for generated, persisted in zip(derived, stored):
            if any(getattr(generated, field) != getattr(persisted, field) for field in fields):
                raise RecoveryConsistencyError("persisted closed trade does not match fill ledger")
            for field in ("entry_price", "exit_price", "gross_realized_pnl", "costs", "realized_pnl"):
                if not isclose(
                    float(getattr(generated, field)),
                    float(getattr(persisted, field)),
                    rel_tol=1e-12,
                    abs_tol=1e-9,
                ):
                    raise RecoveryConsistencyError(
                        f"persisted trade field {field} does not match fill ledger"
                    )
