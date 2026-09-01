"""Compact SQLite repository layer for sessions and execution records."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from typing import Any, Iterable, Mapping
from uuid import uuid4

from ..models import (
    Fill,
    Instrument,
    Order,
    OrderStatus,
    OrderType,
    PortfolioSnapshot,
    SessionStatus,
    Side,
    Trade,
    TradeIntent,
    TradingSession,
)
from ..portfolio import Portfolio
from ..risk import RiskDecision
from .database import SQLiteDatabase


class DuplicateFillError(RuntimeError):
    pass


class SessionNotFoundError(LookupError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _instrument(row: Mapping[str, Any]) -> Instrument:
    return Instrument(
        exchange=row["exchange"],
        tradingsymbol=row["tradingsymbol"],
        instrument_token=row["instrument_token"],
        instrument_type=row["instrument_type"],
        tick_size=row["tick_size"],
        lot_size=row["lot_size"],
        segment=row["segment"],
    )


class SQLiteStore:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database
        self.database.initialize()

    def insert_session(self, session: TradingSession) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session.session_id,
                    session.trading_date.isoformat(),
                    session.mode,
                    session.started_at.isoformat() if session.started_at else None,
                    session.ended_at.isoformat() if session.ended_at else None,
                    session.status,
                    session.starting_capital,
                    _json(session.strategy_context),
                    session.created_at.isoformat(),
                    session.updated_at.isoformat(),
                ),
            )
            self._journal(connection, session.session_id, session.created_at, "SESSION_CREATED")

    def update_session(self, session: TradingSession, event_type: str) -> None:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """UPDATE sessions SET started_at=?, ended_at=?, status=?, updated_at=?
                   WHERE session_id=?""",
                (
                    session.started_at.isoformat() if session.started_at else None,
                    session.ended_at.isoformat() if session.ended_at else None,
                    session.status,
                    session.updated_at.isoformat(),
                    session.session_id,
                ),
            )
            if cursor.rowcount != 1:
                raise SessionNotFoundError(session.session_id)
            self._journal(connection, session.session_id, session.updated_at, event_type)

    def get_session(self, session_id: str) -> TradingSession:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        if row is None:
            raise SessionNotFoundError(f"session not found: {session_id}")
        return TradingSession(
            session_id=row["session_id"],
            trading_date=date.fromisoformat(row["trading_date"]),
            mode=row["mode"],
            started_at=_dt(row["started_at"]),
            ended_at=_dt(row["ended_at"]),
            status=SessionStatus(row["status"]),
            starting_capital=row["starting_capital"],
            strategy_context=json.loads(row["strategy_context_json"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def list_sessions(self) -> list[TradingSession]:
        with self.database.connect() as connection:
            ids = [row[0] for row in connection.execute("SELECT session_id FROM sessions ORDER BY created_at")]
        return [self.get_session(session_id) for session_id in ids]

    def record_order_decision(
        self,
        session_id: str,
        order: Order,
        decision: RiskDecision,
        timestamp: datetime,
    ) -> None:
        with self.database.transaction() as connection:
            self._upsert_order(connection, session_id, order)
            connection.execute(
                """INSERT INTO risk_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    uuid4().hex,
                    session_id,
                    timestamp.isoformat(),
                    int(decision.approved),
                    decision.reason,
                    decision.approved_quantity,
                    order.signal_id,
                    order.order_id,
                    "{}",
                ),
            )
            event = "RISK_APPROVED" if decision.approved else "RISK_REJECTED"
            self._journal(connection, session_id, timestamp, event, "order", order.order_id)
            self._journal(connection, session_id, timestamp, f"ORDER_{order.status}", "order", order.order_id)

    def record_order_update(self, session_id: str, order: Order, timestamp: datetime) -> None:
        with self.database.transaction() as connection:
            self._upsert_order(connection, session_id, order)
            self._journal(
                connection, session_id, timestamp, f"ORDER_{order.status}", "order", order.order_id
            )

    def record_execution(
        self,
        session_id: str,
        orders: Iterable[Order],
        fills: Iterable[Fill],
        trades: Iterable[Trade],
        portfolio: Portfolio,
        timestamp: datetime,
    ) -> None:
        orders = tuple(orders)
        fills = tuple(fills)
        trades = tuple(trades)
        with self.database.transaction() as connection:
            for order in orders:
                self._upsert_order(connection, session_id, order)
            for fill in fills:
                try:
                    self._insert_fill(connection, session_id, fill)
                except sqlite3.IntegrityError as exc:
                    if "fills.fill_id" in str(exc) or "UNIQUE constraint failed" in str(exc):
                        raise DuplicateFillError(f"fill already persisted: {fill.fill_id}") from exc
                    raise
                self._journal(
                    connection,
                    session_id,
                    fill.timestamp,
                    "FILL_APPLIED",
                    "fill",
                    fill.fill_id,
                    {
                        "order_id": fill.order_id,
                        "execution": next(
                            (
                                order.metadata.get("live_execution")
                                for order in orders
                                if order.order_id == fill.order_id
                            ),
                            None,
                        ),
                    },
                )
            for trade in trades:
                self._insert_trade(connection, session_id, trade)
                self._journal(
                    connection, session_id, timestamp, "POSITION_REDUCED", "trade", trade.trade_id
                )
            self._insert_snapshot(connection, session_id, portfolio, timestamp, "AFTER_FILL")
            self._checkpoint_positions(connection, session_id, portfolio, timestamp)

    def checkpoint_positions(self, session_id: str, portfolio: Portfolio, timestamp: datetime) -> None:
        with self.database.transaction() as connection:
            self._checkpoint_positions(connection, session_id, portfolio, timestamp)

    def record_snapshot(
        self, session_id: str, portfolio: Portfolio, timestamp: datetime, reason: str
    ) -> None:
        with self.database.transaction() as connection:
            self._insert_snapshot(connection, session_id, portfolio, timestamp, reason)
            self._checkpoint_positions(connection, session_id, portfolio, timestamp)

    def load_orders(self, session_id: str) -> list[Order]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM orders WHERE session_id=? ORDER BY created_at, order_id", (session_id,)
            ).fetchall()
        return [
            Order(
                instrument=_instrument(row),
                side=Side(row["side"]),
                order_type=OrderType(row["order_type"]),
                quantity=row["quantity"],
                created_at=datetime.fromisoformat(row["created_at"]),
                eligible_after=datetime.fromisoformat(row["eligible_after"]),
                signal_id=row["signal_id"],
                strategy_id=row["strategy_id"],
                strategy_version=row["strategy_version"],
                limit_price=row["limit_price"],
                stop_price=row["stop_price"],
                status=OrderStatus(row["status"]),
                filled_quantity=row["filled_quantity"],
                average_fill_price=row["average_fill_price"],
                order_id=row["order_id"],
                broker_order_id=row["broker_order_id"],
                rejection_reason=row["rejection_reason"],
                cancellation_reason=row["cancellation_reason"],
                updated_at=_dt(row["updated_at"]),
                oco_group_id=row["oco_group_id"],
                metadata=json.loads(row["metadata_json"]),
                intent=TradeIntent(json.loads(row["metadata_json"]).get("intent", "INTRADAY")),
            )
            for row in rows
        ]

    def load_fills(self, session_id: str) -> list[Fill]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM fills WHERE session_id=? ORDER BY sequence", (session_id,)
            ).fetchall()
        intents = {order.order_id: order.intent for order in self.load_orders(session_id)}
        return [
            Fill(
                order_id=row["order_id"],
                instrument=_instrument(row),
                side=Side(row["side"]),
                quantity=row["quantity"],
                price=row["price"],
                timestamp=datetime.fromisoformat(row["timestamp"]),
                signal_id=row["signal_id"],
                strategy_id=row["strategy_id"],
                strategy_version=row["strategy_version"],
                fill_id=row["fill_id"],
                fees=row["fees"],
                intent=intents[row["order_id"]],
            )
            for row in rows
        ]

    def load_trades(self, session_id: str) -> list[Trade]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM trades WHERE session_id=? ORDER BY exit_timestamp, trade_id",
                (session_id,),
            ).fetchall()
        fills = {fill.fill_id: fill for fill in self.load_fills(session_id)}
        return [
            Trade(
                instrument=_instrument(row),
                side=Side(row["side"]),
                quantity=row["quantity"],
                entry_price=row["entry_price"],
                entry_timestamp=datetime.fromisoformat(row["entry_timestamp"]),
                entry_fill_ids=tuple(json.loads(row["entry_fill_ids_json"])),
                signal_id=row["signal_id"],
                strategy_id=row["strategy_id"],
                strategy_version=row["strategy_version"],
                trade_id=row["trade_id"],
                exit_price=row["exit_price"],
                exit_timestamp=datetime.fromisoformat(row["exit_timestamp"]),
                exit_fill_ids=tuple(json.loads(row["exit_fill_ids_json"])),
                realized_pnl=row["net_realized_pnl"],
                gross_realized_pnl=row["gross_realized_pnl"],
                costs=row["costs"],
                intent=fills[json.loads(row["entry_fill_ids_json"])[0]].intent,
            )
            for row in rows
        ]

    def load_marks(self, session_id: str) -> dict[str, float]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT instrument_key, last_price FROM position_checkpoints "
                "WHERE session_id=? AND last_price IS NOT NULL",
                (session_id,),
            ).fetchall()
        return {row["instrument_key"]: row["last_price"] for row in rows}

    def load_position_checkpoints(self, session_id: str) -> dict[str, sqlite3.Row]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM position_checkpoints WHERE session_id=?", (session_id,)
            ).fetchall()
        return {row["instrument_key"]: row for row in rows}

    def counts(self, session_id: str) -> dict[str, int]:
        with self.database.connect() as connection:
            return {
                table: connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE session_id=?", (session_id,)
                ).fetchone()[0]
                for table in ("orders", "fills", "trades", "risk_events", "event_journal")
            }

    def load_risk_events(self, session_id: str) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM risk_events WHERE session_id=? ORDER BY timestamp, risk_event_id",
                (session_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def load_event_journal(self, session_id: str) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM event_journal WHERE session_id=? ORDER BY event_id",
                (session_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_journal_event(
        self,
        session_id: str,
        timestamp: datetime,
        event_type: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        with self.database.transaction() as connection:
            self._journal(connection, session_id, timestamp, event_type, metadata=metadata)

    @staticmethod
    def _upsert_order(connection: sqlite3.Connection, session_id: str, order: Order) -> None:
        instrument = order.instrument
        values = (
            order.order_id, session_id, order.signal_id, order.strategy_id, order.strategy_version,
            instrument.exchange, instrument.tradingsymbol, instrument.instrument_token,
            instrument.instrument_type, instrument.tick_size, instrument.lot_size, instrument.segment,
            order.side, order.order_type, order.quantity, order.limit_price, order.stop_price,
            order.filled_quantity, order.average_fill_price, order.status, order.created_at.isoformat(),
            (order.eligible_after or order.created_at).isoformat(),
            order.updated_at.isoformat() if order.updated_at else None,
            order.broker_order_id, order.oco_group_id, order.rejection_reason,
            order.cancellation_reason, _json({**order.metadata, "intent": order.intent}),
        )
        connection.execute(
            """INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(order_id) DO UPDATE SET
               filled_quantity=excluded.filled_quantity,
               average_fill_price=excluded.average_fill_price,
               status=excluded.status, updated_at=excluded.updated_at,
               rejection_reason=excluded.rejection_reason,
               cancellation_reason=excluded.cancellation_reason,
               metadata_json=excluded.metadata_json""",
            values,
        )

    @staticmethod
    def _insert_fill(connection: sqlite3.Connection, session_id: str, fill: Fill) -> None:
        instrument = fill.instrument
        connection.execute(
            """INSERT INTO fills
               (fill_id,session_id,order_id,exchange,tradingsymbol,instrument_token,
                instrument_type,tick_size,lot_size,segment,side,quantity,price,timestamp,
                fees,signal_id,strategy_id,strategy_version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                fill.fill_id, session_id, fill.order_id, instrument.exchange,
                instrument.tradingsymbol, instrument.instrument_token, instrument.instrument_type,
                instrument.tick_size, instrument.lot_size, instrument.segment, fill.side,
                fill.quantity, fill.price, fill.timestamp.isoformat(), fill.fees, fill.signal_id,
                fill.strategy_id, fill.strategy_version,
            ),
        )

    @staticmethod
    def _insert_trade(connection: sqlite3.Connection, session_id: str, trade: Trade) -> None:
        instrument = trade.instrument
        connection.execute(
            """INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                trade.trade_id, session_id, instrument.exchange, instrument.tradingsymbol,
                instrument.instrument_token, instrument.instrument_type, instrument.tick_size,
                instrument.lot_size, instrument.segment, trade.side, trade.quantity,
                trade.entry_price, trade.entry_timestamp.isoformat(), trade.exit_price,
                trade.exit_timestamp.isoformat() if trade.exit_timestamp else None,
                trade.gross_realized_pnl, trade.costs, trade.realized_pnl, trade.signal_id,
                trade.strategy_id, trade.strategy_version, _json(trade.entry_fill_ids),
                _json(trade.exit_fill_ids),
            ),
        )

    @staticmethod
    def _insert_snapshot(
        connection: sqlite3.Connection,
        session_id: str,
        portfolio: Portfolio,
        timestamp: datetime,
        reason: str,
    ) -> None:
        connection.execute(
            """INSERT INTO portfolio_snapshots
               (session_id,timestamp,cash,market_value,equity,realized_pnl,
                unrealized_pnl,costs,open_positions,reason) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                session_id, timestamp.isoformat(), portfolio.cash, portfolio.market_value,
                portfolio.equity, portfolio.realized_pnl, portfolio.unrealized_pnl,
                portfolio.total_costs, len(portfolio.open_positions), reason,
            ),
        )

    @staticmethod
    def _checkpoint_positions(
        connection: sqlite3.Connection,
        session_id: str,
        portfolio: Portfolio,
        timestamp: datetime,
    ) -> None:
        for position in portfolio.positions.values():
            connection.execute(
                """INSERT INTO position_checkpoints VALUES (?,?,?,?,?,?)
                   ON CONFLICT(session_id,instrument_key) DO UPDATE SET
                   quantity=excluded.quantity, average_price=excluded.average_price,
                   last_price=excluded.last_price, updated_at=excluded.updated_at""",
                (
                    session_id, position.instrument.key, position.quantity,
                    position.average_price, position.last_price, timestamp.isoformat(),
                ),
            )

    @staticmethod
    def _journal(
        connection: sqlite3.Connection,
        session_id: str,
        timestamp: datetime,
        event_type: str,
        entity_type: str | None = None,
        entity_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """INSERT INTO event_journal
               (session_id,timestamp,event_type,entity_type,entity_id,metadata_json)
               VALUES (?,?,?,?,?,?)""",
            (
                session_id, timestamp.isoformat(), event_type, entity_type, entity_id,
                _json(metadata or {}),
            ),
        )
