#!/usr/bin/env python3
"""Inspect one durable paper-trading session without changing it."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from paper_trading.config import load_config  # noqa: E402
from paper_trading.models import OrderStatus  # noqa: E402
from paper_trading.storage import SQLiteDatabase, SQLiteStore, SessionService  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--config-dir", type=Path, default=PROJECT_ROOT / "config")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    database_path = args.database or load_config(args.config_dir).persistence.database_path
    store = SQLiteStore(SQLiteDatabase(database_path))
    session = store.get_session(args.session)
    portfolio = SessionService(store).restore_portfolio(session.session_id)
    orders = store.load_orders(session.session_id)
    counts = store.counts(session.session_id)
    active = sum(
        order.status in {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED} for order in orders
    )
    print(f"Session: {session.session_id}")
    print(f"Status: {session.status}")
    print(f"Mode: {session.mode} | Trading date: {session.trading_date}")
    print(f"Starting capital: {session.starting_capital:.2f}")
    print(f"Orders: {counts['orders']} | Active orders: {active} | Fills: {counts['fills']}")
    print(f"Open positions: {len(portfolio.open_positions)} | Closed trades: {len(portfolio.closed_trades)}")
    print(f"Cash: {portfolio.cash:.2f} | Equity: {portfolio.equity:.2f}")
    print(
        f"Gross realized P&L: {portfolio.gross_realized_pnl:.2f} | "
        f"Costs: {portfolio.total_costs:.2f} | Net realized P&L: {portfolio.realized_pnl:.2f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
