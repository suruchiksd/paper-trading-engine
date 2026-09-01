"""Read-only CSV reporting derived from durable session state."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from .models import Side, TradeIntent
from .storage import SQLiteStore, SessionService


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: str
    trading_date: str
    mode: str
    status: str
    started_at: str | None
    ended_at: str | None
    starting_capital: float
    ending_equity: float
    cash: float
    gross_realized_pnl: float
    costs: float
    net_realized_pnl: float
    unrealized_pnl: float
    total_pnl: float
    orders: int
    fills: int
    closed_trades: int
    winning_trades: int
    losing_trades: int
    open_positions: int
    intraday_positions_remaining: int
    swing_positions_remaining: int
    rejected_requests: int
    feed_reconnect_count: int | None
    feed_dropped_ticks: int | None
    feed_duplicate_ticks: int | None
    feed_out_of_order_ticks: int | None


class SessionExporter:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def summary(self, session_id: str) -> SessionSummary:
        session = self.store.get_session(session_id)
        portfolio = SessionService(self.store).restore_portfolio(session_id)
        counts = self.store.counts(session_id)
        risk = self.store.load_risk_events(session_id)
        health = self._last_health(session_id)
        trades = portfolio.closed_trades
        positions = portfolio.open_positions
        return SessionSummary(
            session.session_id, session.trading_date.isoformat(), session.mode,
            str(session.status), session.started_at.isoformat() if session.started_at else None,
            session.ended_at.isoformat() if session.ended_at else None,
            session.starting_capital, portfolio.equity, portfolio.cash,
            portfolio.gross_realized_pnl, portfolio.total_costs, portfolio.realized_pnl,
            portfolio.unrealized_pnl, portfolio.total_pnl, counts["orders"], counts["fills"],
            counts["trades"], sum((trade.realized_pnl or 0) > 0 for trade in trades),
            sum((trade.realized_pnl or 0) < 0 for trade in trades), len(positions),
            sum(item.intent is TradeIntent.INTRADAY for item in positions),
            sum(item.intent is TradeIntent.SWING for item in positions),
            sum(not row["approved"] for row in risk),
            health.get("reconnect_count"), health.get("dropped_ticks"),
            health.get("duplicate_ticks"), health.get("out_of_order_ticks"),
        )

    def export(self, session_id: str, root: Path) -> tuple[Path, ...]:
        destination = root / session_id
        destination.mkdir(parents=True, exist_ok=True)
        session = self.store.get_session(session_id)
        portfolio = SessionService(self.store).restore_portfolio(session_id)
        orders = self.store.load_orders(session_id)
        fills = self.store.load_fills(session_id)
        trades = self.store.load_trades(session_id)
        order_by_id = {order.order_id: order for order in orders}
        paths = []
        paths.append(self._write(destination / "session_summary.csv", [asdict(self.summary(session_id))]))
        order_fields = (
            "session_id", "order_id", "created_at", "updated_at", "symbol", "side",
            "quantity", "order_type", "intent", "limit_price", "trigger_price", "status",
            "filled_quantity", "average_fill_price", "strategy_id", "signal_id", "reason",
            "oco_group_id",
        )
        paths.append(self._write(destination / "orders.csv", ({
            "session_id": session_id, "order_id": o.order_id, "created_at": o.created_at,
            "updated_at": o.updated_at, "symbol": o.instrument.tradingsymbol, "side": o.side,
            "quantity": o.quantity, "order_type": o.order_type, "intent": o.intent,
            "limit_price": o.limit_price, "trigger_price": o.stop_price, "status": o.status,
            "filled_quantity": o.filled_quantity, "average_fill_price": o.average_fill_price,
            "strategy_id": o.strategy_id, "signal_id": o.signal_id,
            "reason": o.rejection_reason or o.cancellation_reason, "oco_group_id": o.oco_group_id,
        } for o in orders), order_fields))
        fill_fields = (
            "session_id", "fill_id", "order_id", "timestamp", "symbol", "side", "quantity",
            "fill_price", "transaction_cost", "intent", "execution_source", "observed_ltp",
            "observed_bid", "observed_ask", "quote_exchange_timestamp",
            "quote_received_timestamp", "fallback",
        )
        paths.append(self._write(
            destination / "fills.csv",
            (self._fill_row(session_id, f, order_by_id[f.order_id]) for f in fills), fill_fields,
        ))
        trade_fields = (
            "session_id", "trade_id", "symbol", "direction", "intent", "quantity",
            "entry_timestamp", "entry_price", "exit_timestamp", "exit_price", "gross_pnl",
            "costs", "net_pnl", "strategy_id", "strategy_version", "signal_id",
            "entry_fill_ids", "exit_fill_ids",
        )
        paths.append(self._write(destination / "trades.csv", ({
            "session_id": session_id, "trade_id": t.trade_id, "symbol": t.instrument.tradingsymbol,
            "direction": "LONG" if t.side is Side.BUY else "SHORT", "intent": t.intent,
            "quantity": t.quantity, "entry_timestamp": t.entry_timestamp,
            "entry_price": t.entry_price, "exit_timestamp": t.exit_timestamp,
            "exit_price": t.exit_price, "gross_pnl": t.gross_realized_pnl, "costs": t.costs,
            "net_pnl": t.realized_pnl, "strategy_id": t.strategy_id,
            "strategy_version": t.strategy_version, "signal_id": t.signal_id,
            "entry_fill_ids": "|".join(t.entry_fill_ids), "exit_fill_ids": "|".join(t.exit_fill_ids),
        } for t in trades), trade_fields))
        paths.append(self._write(destination / "positions.csv", ({
            "session_id": session_id, "symbol": p.instrument.tradingsymbol,
            "quantity": p.quantity, "direction": "LONG" if p.quantity > 0 else "SHORT",
            "intent": p.intent, "average_entry": p.average_price,
            "last_mark": p.last_price, "unrealized_pnl": p.unrealized_pnl,
        } for p in portfolio.open_positions), fieldnames=(
            "session_id", "symbol", "quantity", "direction", "intent", "average_entry",
            "last_mark", "unrealized_pnl",
        )))
        paths.append(self._write(
            destination / "risk_events.csv", self.store.load_risk_events(session_id),
            ("risk_event_id", "session_id", "timestamp", "approved", "reason",
             "approved_quantity", "signal_id", "order_id", "metadata_json"),
        ))
        paths.append(self._write(
            destination / "event_journal.csv", self.store.load_event_journal(session_id),
            ("event_id", "session_id", "timestamp", "event_type", "entity_type", "entity_id",
             "metadata_json"),
        ))
        return tuple(paths)

    @staticmethod
    def _fill_row(session_id, fill, order) -> dict[str, Any]:
        execution = order.metadata.get("live_execution") or {}
        return {
            "session_id": session_id, "fill_id": fill.fill_id, "order_id": fill.order_id,
            "timestamp": fill.timestamp, "symbol": fill.instrument.tradingsymbol,
            "side": fill.side, "quantity": fill.quantity, "fill_price": fill.price,
            "transaction_cost": fill.fees, "intent": fill.intent,
            "execution_source": execution.get("source"), "observed_ltp": execution.get("observed_last"),
            "observed_bid": execution.get("observed_bid"), "observed_ask": execution.get("observed_ask"),
            "quote_exchange_timestamp": execution.get("quote_exchange_timestamp"),
            "quote_received_timestamp": execution.get("quote_received_timestamp"),
            "fallback": execution.get("fallback"),
        }

    def _last_health(self, session_id: str) -> dict[str, Any]:
        for row in reversed(self.store.load_event_journal(session_id)):
            if row["event_type"] == "LIVE_FEED_FINAL_HEALTH":
                return json.loads(row["metadata_json"])
        return {}

    @staticmethod
    def _write(path: Path, rows: Iterable[dict[str, Any]], fieldnames=None) -> Path:
        rows = list(rows)
        names = list(fieldnames or (rows[0].keys() if rows else ()))
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=names)
            writer.writeheader()
            writer.writerows(rows)
        return path
