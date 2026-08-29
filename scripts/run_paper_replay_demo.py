#!/usr/bin/env python3
"""Demonstrate one synthetic next-bar paper order; this is not a strategy."""

from __future__ import annotations

import argparse
import sys
from datetime import date, time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from paper_trading.config import load_config  # noqa: E402
from paper_trading.engine import TradingEngine  # noqa: E402
from paper_trading.execution import BasisPointsTransactionCosts, OrderManager, PaperBroker  # noqa: E402
from paper_trading.market_data import (  # noqa: E402
    CsvInstrumentRepository,
    HistoricalReplayFeed,
    ParquetHistoricalCandleRepository,
)
from paper_trading.models import OrderRequest, OrderType, Side  # noqa: E402
from paper_trading.portfolio import Portfolio  # noqa: E402
from paper_trading.risk import RiskLimits, RiskManager  # noqa: E402
from paper_trading.storage import SQLiteDatabase, SQLiteStore, SessionService  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--submit-after", default="10:00", type=time.fromisoformat)
    parser.add_argument("--quantity", default=10, type=int)
    parser.add_argument("--segment")
    parser.add_argument("--config-dir", type=Path, default=PROJECT_ROOT / "config")
    parser.add_argument("--persist", action="store_true", help="store a durable PAPER_REPLAY session")
    parser.add_argument("--database", type=Path, help="override configured SQLite path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config_dir)
    historical = config.historical_data
    instrument = CsvInstrumentRepository(historical.instrument_master).resolve(
        args.symbol, segment=args.segment
    )
    repository = ParquetHistoricalCandleRepository(
        historical.root,
        equities_subdir=historical.equities_subdir,
        indices_subdir=historical.indices_subdir,
        quality_policy=historical.quality,
    )
    feed = HistoricalReplayFeed(repository, [instrument], args.date)
    broker = PaperBroker(
        slippage=config.paper_broker.slippage,
        ambiguity_policy=config.paper_broker.intrabar_ambiguity,
        cost_model=BasisPointsTransactionCosts(config.paper_broker.transaction_cost_bps),
    )
    order_manager = OrderManager(broker)
    portfolio = Portfolio(config.risk.starting_capital)
    session = None
    sessions = None
    recorder = None
    if args.persist:
        database_path = args.database or config.persistence.database_path
        sessions = SessionService(SQLiteStore(SQLiteDatabase(database_path)))
        session = sessions.create(args.date, "PAPER_REPLAY", config.risk.starting_capital)
        session = sessions.start(session.session_id)
        sessions.store.record_snapshot(
            session.session_id, portfolio, session.started_at, "SESSION_START"
        )
        recorder = sessions.recorder(session.session_id)
    engine = TradingEngine(
        risk_manager=RiskManager(
            RiskLimits(
                config.risk.maximum_positions,
                config.risk.maximum_daily_loss_pct,
                config.risk.maximum_position_exposure_pct,
            )
        ),
        order_manager=order_manager,
        portfolio=portfolio,
        persistence=recorder,
    )
    submitted = []

    def manual_submission(candle, current_engine) -> None:
        if not submitted and candle.timestamp.time().replace(tzinfo=None) == args.submit_after:
            decision, order = current_engine.submit_order(
                OrderRequest(
                    instrument=instrument,
                    side=Side.BUY,
                    order_type=OrderType.MARKET,
                    quantity=args.quantity,
                    timestamp=candle.timestamp,
                    reference_price=candle.close,
                    metadata={"source": "manual replay demonstration"},
                )
            )
            submitted.append((decision, order))

    event_count = engine.replay(feed, after_candle=manual_submission)
    print("Paper execution replay demonstration (synthetic order; not a strategy)")
    print(f"Date: {args.date} | Symbol: {instrument.tradingsymbol} | Candles: {event_count}")
    if not submitted:
        print(f"No candle matched submission time {args.submit_after}")
        if sessions is not None and session is not None:
            sessions.fail(session.session_id, feed.session_end)
            print(f"Session ID: {session.session_id}")
        return 1
    decision, order = submitted[0]
    print(f"Risk: {decision.reason} | Order: {order.order_id} | Status: {order.status}")
    if order.average_fill_price is not None:
        fill = order_manager.fills[0]
        print(f"Fill: {fill.timestamp.isoformat()} @ {fill.price:.4f} | Quantity: {fill.quantity}")
    position = portfolio.positions.get(instrument.key)
    if position is not None:
        print(
            f"Position: {position.quantity} @ {position.average_price:.4f} | "
            f"Mark: {position.last_price:.4f} | Unrealized P&L: {portfolio.unrealized_pnl:.2f}"
        )
    print(f"Cash: {portfolio.cash:.2f} | Equity: {portfolio.equity:.2f}")
    if sessions is not None and session is not None:
        sessions.complete(session.session_id, portfolio, feed.session_end)
        print(f"Session ID: {session.session_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
