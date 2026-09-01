#!/usr/bin/env python3
"""Export a durable paper session to stable CSV reports without modifying it."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from paper_trading.config import load_config  # noqa: E402
from paper_trading.reporting import SessionExporter  # noqa: E402
from paper_trading.storage import SQLiteDatabase, SQLiteStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--config-dir", type=Path, default=PROJECT_ROOT / "config")
    args = parser.parse_args()
    config = load_config(args.config_dir)
    store = SQLiteStore(SQLiteDatabase(args.database or config.persistence.database_path))
    exporter = SessionExporter(store)
    summary = exporter.summary(args.session)
    print(f"Session: {summary.session_id} | Status: {summary.status}")
    print(f"Orders: {summary.orders} | Fills: {summary.fills} | Trades: {summary.closed_trades}")
    print(f"Equity: {summary.ending_equity:.2f} | Total P&L: {summary.total_pnl:.2f}")
    for path in exporter.export(args.session, args.output or config.session_controls.reports_path):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
