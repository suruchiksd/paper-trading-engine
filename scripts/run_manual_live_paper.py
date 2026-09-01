#!/usr/bin/env python3
"""Run a durable manual live paper-trading session using read-only Kite ticks."""

from __future__ import annotations

import argparse
import logging
import queue
import sys
import threading
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from paper_trading.config import load_config  # noqa: E402
from paper_trading.engine import TradingEngine  # noqa: E402
from paper_trading.events import CandleEvent, TickEvent  # noqa: E402
from paper_trading.execution import LiveExecutionConfig, LivePaperBroker, OrderManager  # noqa: E402
from paper_trading.manual import (  # noqa: E402
    LiveOrderGate,
    ManualAction,
    ManualCommandError,
    ManualLivePaperRuntime,
    parse_manual_command,
)
from paper_trading.market_data import (  # noqa: E402
    CsvInstrumentRepository,
    KiteCredentials,
    LiveQuoteStore,
    ZerodhaLiveFeed,
)
from paper_trading.models import SessionStatus  # noqa: E402
from paper_trading.execution.costs import BasisPointsTransactionCosts  # noqa: E402
from paper_trading.portfolio import Portfolio  # noqa: E402
from paper_trading.risk import RiskLimits, RiskManager  # noqa: E402
from paper_trading.storage import SQLiteDatabase, SQLiteStore, SessionService  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", action="append", help="repeat for multiple instruments")
    parser.add_argument("--segment", help="optional NSE or INDICES segment filter")
    parser.add_argument("--resume", metavar="SESSION_ID")
    parser.add_argument("--config-dir", type=Path, default=PROJECT_ROOT / "config")
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    parser.add_argument("--database", type=Path)
    return parser.parse_args()


def _print_help() -> None:
    print("Commands: quote SYMBOL | buy/sell SYMBOL QTY market")
    print("          buy/sell SYMBOL QTY limit PRICE | buy/sell SYMBOL QTY stop PRICE")
    print("          orders | positions | trades | pnl | cancel ORDER_ID | health | status | quit")


def _show(command, runtime, feed, session) -> bool:
    action = command.action
    if action is ManualAction.HELP:
        _print_help()
    elif action is ManualAction.QUIT:
        return False
    elif action is ManualAction.HEALTH:
        print(feed.health_snapshot())
    elif action is ManualAction.STATUS:
        print(f"Session: {session.session_id}  Mode: LIVE_PAPER  Status: RUNNING")
    elif action is ManualAction.QUOTE:
        quote = runtime.quotes.get(command.symbol or "")
        print(quote if quote is not None else f"No live quote for {command.symbol}")
    elif action is ManualAction.ORDERS:
        for order in runtime.engine.order_manager.orders.values():
            print(order.order_id, order.side, order.instrument.tradingsymbol, order.quantity, order.order_type, order.status)
    elif action is ManualAction.POSITIONS:
        for position in runtime.engine.portfolio.open_positions:
            print(position.instrument.tradingsymbol, position.quantity, position.average_price, position.last_price, position.unrealized_pnl)
    elif action is ManualAction.TRADES:
        for trade in runtime.engine.portfolio.closed_trades:
            print(trade.trade_id, trade.instrument.tradingsymbol, trade.quantity, trade.realized_pnl)
    elif action is ManualAction.PNL:
        portfolio = runtime.engine.portfolio
        print(f"Cash={portfolio.cash:.2f} Realized={portfolio.realized_pnl:.2f} Unrealized={portfolio.unrealized_pnl:.2f} Total={portfolio.total_pnl:.2f}")
    elif action is ManualAction.CANCEL:
        order = runtime.engine.cancel_order(command.order_id or "", datetime.now(IST))
        print(f"PAPER CANCELLED {order.order_id}")
    elif action in {ManualAction.BUY, ManualAction.SELL}:
        print("PAPER ORDER")
        print(f"{command.side} {command.symbol}  Qty: {command.quantity}  Type: {command.order_type}")
        decision, order = runtime.submit(command)
        print(f"{order.order_id} {order.status}: {decision.reason}")
    return True


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config(args.config_dir)
    credentials = KiteCredentials.from_environment(args.env_file)
    symbols = args.symbol or list(config.live_market_data.symbols)
    instrument_repository = CsvInstrumentRepository(config.historical_data.instrument_master)
    instruments = [instrument_repository.resolve(symbol, segment=args.segment) for symbol in symbols]

    database = SQLiteDatabase(args.database or config.persistence.database_path)
    database.initialize()
    store = SQLiteStore(database)
    sessions = SessionService(store)
    quotes = LiveQuoteStore()
    live = config.live_market_data
    feed = ZerodhaLiveFeed(
        credentials.api_key,
        credentials.access_token or "",
        instruments,
        mode=live.mode,
        stale_after_seconds=live.stale_after_seconds,
        reconnect_max_tries=live.reconnect_max_tries,
        reconnect_max_delay=live.reconnect_max_delay,
        candle_finalize_delay_seconds=live.candle_finalize_delay_seconds,
    )
    paper = config.live_paper
    broker = LivePaperBroker(
        quotes,
        feed.health_snapshot,
        config=LiveExecutionConfig(
            max_quote_age_seconds=paper.max_quote_age_seconds,
            allow_ltp_fallback=paper.allow_ltp_fallback,
            market_slippage_bps=paper.market_slippage_bps,
            stop_market_slippage_bps=paper.stop_market_slippage_bps,
        ),
        clock=lambda: datetime.now(IST),
        cost_model=BasisPointsTransactionCosts(config.paper_broker.transaction_cost_bps),
    )
    order_ids = lambda: f"PAPER-{uuid4().hex}"
    if args.resume:
        recovered = sessions.resume(args.resume, broker)
        if recovered.session.mode != "LIVE_PAPER":
            raise ValueError("only LIVE_PAPER sessions may be resumed by this command")
        session, portfolio, order_manager = recovered.session, recovered.portfolio, recovered.order_manager
        order_manager.order_id_factory = order_ids
        recorder = sessions.recorder(session.session_id)
        recorder.record_audit("LIVE_MARKET_DATA_GAP_NOT_REPLAYED", datetime.now(IST), {"reason": "resume evaluates only newly observed ticks"})
    else:
        session = sessions.create(datetime.now(IST).date(), "LIVE_PAPER", config.risk.starting_capital)
        session = sessions.start(session.session_id)
        portfolio = Portfolio(session.starting_capital)
        order_manager = OrderManager(broker, order_id_factory=order_ids)
        recorder = sessions.recorder(session.session_id)
        recorder.checkpoint_positions(portfolio, datetime.now(IST))

    risk = RiskManager(RiskLimits(config.risk.maximum_positions, config.risk.maximum_daily_loss_pct, config.risk.maximum_position_exposure_pct))
    gate = LiveOrderGate(
        instruments, quotes, feed.health_snapshot, lambda: datetime.now(IST),
        paper.max_quote_age_seconds,
    )
    engine = TradingEngine(risk_manager=risk, order_manager=order_manager, portfolio=portfolio, persistence=recorder, pre_trade_check=gate.check)
    runtime = ManualLivePaperRuntime(engine, quotes, instruments, lambda: datetime.now(IST))

    work: queue.Queue[tuple[str, object]] = queue.Queue()
    accepting = True
    market_thread: threading.Thread | None = None

    def consume_market() -> None:
        try:
            for event in feed.events():
                work.put(("market", event))
        except Exception as exc:
            work.put(("fatal", exc))

    def read_commands() -> None:
        while accepting:
            try:
                work.put(("command", input("paper> ")))
            except (EOFError, KeyboardInterrupt):
                work.put(("command", "quit"))
                return

    print("\nPAPER TRADING — NO REAL ORDERS\n")
    print(f"Session: {session.session_id}\nSubscribed: {', '.join(symbols)}")
    _print_help()
    failed = False
    try:
        feed.start()
        market_thread = threading.Thread(target=consume_market, daemon=True, name="paper-market-consumer")
        input_thread = threading.Thread(target=read_commands, daemon=True, name="paper-terminal-input")
        market_thread.start()
        input_thread.start()
        while accepting:
            kind, payload = work.get()
            if kind == "market":
                runtime.handle_market_event(payload)
            elif kind == "fatal":
                raise RuntimeError(f"live market-data processing failed: {payload}")
            else:
                try:
                    accepting = _show(parse_manual_command(str(payload)), runtime, feed, session)
                except (ManualCommandError, ValueError, KeyError) as exc:
                    print(f"ERROR: {exc}")
    except KeyboardInterrupt:
        print("Stopping PAPER session...")
    except Exception:
        failed = True
        logging.exception("manual paper session failed; new orders are disabled")
    finally:
        accepting = False
        feed.stop()
        if market_thread is not None:
            market_thread.join(timeout=5)
        for event in feed.finalize_candles():
            runtime.handle_market_event(event)
        now = datetime.now(IST)
        try:
            recorder.checkpoint_positions(portfolio, now)
            current = store.get_session(session.session_id)
            if current.status is SessionStatus.RUNNING:
                (sessions.fail if failed else sessions.stop)(session.session_id, now)
        except Exception:
            logging.exception("failed to persist final session state")
            failed = True
    print(f"Session {session.session_id} {'FAILED' if failed else 'STOPPED'}; open paper positions were preserved.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
