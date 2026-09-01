from __future__ import annotations

import csv
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from paper_trading.engine import TradingEngine
from paper_trading.execution import LivePaperBroker, OrderManager
from paper_trading.manual import LiveOrderGate, parse_manual_command
from paper_trading.market_data import LiveQuoteStore
from paper_trading.market_data.health import FeedHealthSnapshot, FeedState
from paper_trading.models import Fill, Instrument, OrderRequest, OrderStatus, OrderType, Side, Tick, TradeIntent
from paper_trading.portfolio import Portfolio
from paper_trading.reporting import SessionExporter
from paper_trading.risk import RiskLimits, RiskManager
from paper_trading.session_controls import SessionControlConfig, SessionController
from paper_trading.storage import SQLiteDatabase, SQLiteStore, SessionService

IST = ZoneInfo("Asia/Kolkata")
RELIANCE = Instrument("NSE", "RELIANCE", 1)
SBIN = Instrument("NSE", "SBIN", 2)


def health(state=FeedState.HEALTHY):
    return FeedHealthSnapshot(
        state, state in {FeedState.HEALTHY, FeedState.STALE},
        None, None, 1, 0, 0, 0, 0, 0, 0, None,
    )


def quote_tick(instrument, timestamp, price=100):
    return Tick(instrument, timestamp, timestamp, price, 1000, price - .1, price + .1)


def controls(engine, quotes, instruments, now, state=lambda: health()):
    gate = LiveOrderGate(instruments, quotes, state, lambda: now[0], 5)
    controller = SessionController(
        engine,
        SessionControlConfig(time(15), time(15, 15), time(15, 30)),
        lambda: now[0], gate.check,
    )
    engine.pre_trade_check = controller.check
    return controller


def runtime(now, instruments=(RELIANCE,)):
    quotes = LiveQuoteStore()
    broker = LivePaperBroker(quotes, lambda: health(), lambda: now[0])
    manager = OrderManager(broker)
    portfolio = Portfolio(100_000)
    engine = TradingEngine(
        risk_manager=RiskManager(RiskLimits(5, 50, 100)),
        order_manager=manager, portfolio=portfolio,
    )
    controller = controls(engine, quotes, instruments, now)
    for instrument in instruments:
        quotes.update(quote_tick(instrument, now[0]))
    return engine, controller, quotes


def request(now, instrument=RELIANCE, side=Side.BUY, quantity=10, intent=TradeIntent.INTRADAY):
    return OrderRequest(instrument, side, OrderType.MARKET, quantity, now[0],
                        reference_price=100, intent=intent)


def test_manual_intent_default_and_explicit_syntax():
    assert parse_manual_command("buy RELIANCE 10 market").intent is TradeIntent.INTRADAY
    assert parse_manual_command("buy RELIANCE 10 market swing").intent is TradeIntent.SWING
    assert parse_manual_command("buy RELIANCE 10 limit 100 intraday").intent is TradeIntent.INTRADAY
    assert parse_manual_command("sell RELIANCE 10 stop 99 swing").intent is TradeIntent.SWING
    assert parse_manual_command("squareoff intraday").action.value == "SQUAREOFF"
    assert parse_manual_command("complete").action.value == "COMPLETE"


def test_cutoff_rejects_opening_but_allows_reduction_and_intent_conflict():
    now = [datetime(2026, 9, 1, 14, 59, tzinfo=IST)]
    engine, _, quotes = runtime(now)
    approved, order = engine.submit_order(request(now, intent=TradeIntent.SWING))
    assert approved.approved
    now[0] += timedelta(seconds=1)
    tick = quote_tick(RELIANCE, now[0])
    quotes.update(tick)
    engine.process_live_tick(tick)
    assert engine.portfolio.positions[RELIANCE.key].intent is TradeIntent.SWING

    conflict, _ = engine.submit_order(request(now, quantity=1, intent=TradeIntent.INTRADAY))
    assert conflict.reason == "HOLDING_INTENT_CONFLICT"
    now[0] = datetime(2026, 9, 1, 15, 0, tzinfo=IST)
    quotes.update(quote_tick(RELIANCE, now[0]))
    increasing, _ = engine.submit_order(request(now, quantity=1, intent=TradeIntent.SWING))
    assert increasing.reason == "NEW_ENTRY_CUTOFF_REACHED"
    reducing, _ = engine.submit_order(
        request(now, side=Side.SELL, quantity=5, intent=TradeIntent.INTRADAY)
    )
    assert reducing.approved


def test_daily_loss_gate_still_allows_position_reduction():
    portfolio = Portfolio(100_000)
    portfolio.process_fill(Fill("entry", RELIANCE, Side.BUY, 10, 100, datetime(2026, 9, 1, 10, tzinfo=IST)))
    portfolio.mark(RELIANCE.key, 50)
    risk = RiskManager(RiskLimits(5, .1, 100))
    exit_request = OrderRequest(
        RELIANCE, Side.SELL, OrderType.MARKET, 10,
        datetime(2026, 9, 1, 15, 1, tzinfo=IST), reference_price=50,
    )
    assert risk.evaluate(exit_request, portfolio).approved
    opening = OrderRequest(
        RELIANCE, Side.BUY, OrderType.MARKET, 1,
        datetime(2026, 9, 1, 15, 1, tzinfo=IST), reference_price=50,
    )
    assert risk.evaluate(opening, portfolio).reason == "maximum daily loss reached"


def test_squareoff_targets_intraday_only_and_completion_rules():
    now = [datetime(2026, 9, 1, 15, 15, tzinfo=IST)]
    engine, controller, _ = runtime(now, (RELIANCE, SBIN))
    engine.portfolio.process_fill(Fill("old-i", RELIANCE, Side.BUY, 10, 100, now[0], intent=TradeIntent.INTRADAY))
    engine.portfolio.process_fill(Fill("old-s", SBIN, Side.BUY, 5, 200, now[0], intent=TradeIntent.SWING))
    results = controller.square_off_intraday()
    assert len(results) == 1
    decision, order = results[0]
    assert decision.approved and order.side is Side.SELL and order.quantity == 10
    assert order.intent is TradeIntent.INTRADAY
    assert engine.portfolio.positions[SBIN.key].quantity == 5
    assert "OPEN_INTRADAY_POSITIONS" in controller.completion_blockers()
    engine.portfolio.positions[RELIANCE.key].quantity = 0
    engine.cancel_order(order.order_id, now[0])
    assert controller.completion_blockers() == ("OPEN_SWING_POSITIONS_REQUIRE_STOPPED_RESUME",)
    engine.portfolio.positions[SBIN.key].quantity = 0
    assert controller.completion_blockers() == ()


def test_short_squareoff_and_eod_intraday_order_cancellation():
    now = [datetime(2026, 9, 1, 15, 15, tzinfo=IST)]
    engine, controller, _ = runtime(now)
    engine.portfolio.process_fill(
        Fill("old-short", RELIANCE, Side.SELL, 4, 100, now[0], intent=TradeIntent.INTRADAY)
    )
    _, closing = controller.square_off_intraday()[0]
    assert closing.side is Side.BUY and closing.quantity == 4
    engine.cancel_order(closing.order_id, now[0])
    pending = engine.submit_order(
        OrderRequest(RELIANCE, Side.BUY, OrderType.LIMIT, 1, now[0],
                     reference_price=90, limit_price=90, intent=TradeIntent.INTRADAY)
    )[1]
    assert pending.status is OrderStatus.OPEN
    now[0] = datetime(2026, 9, 1, 15, 30, tzinfo=IST)
    controller.on_clock()
    assert pending.status is OrderStatus.CANCELLED


def test_stale_squareoff_never_fabricates_fill():
    now = [datetime(2026, 9, 1, 15, 15, tzinfo=IST)]
    state = [FeedState.STALE]
    quotes = LiveQuoteStore()
    broker = LivePaperBroker(quotes, lambda: health(state[0]), lambda: now[0])
    manager = OrderManager(broker)
    portfolio = Portfolio(100_000)
    portfolio.process_fill(Fill("old", RELIANCE, Side.BUY, 10, 100, now[0]))
    class Recorder:
        def __init__(self): self.audit = []
        def record_order_decision(self, *args): pass
        def record_audit(self, event_type, timestamp, metadata):
            self.audit.append((event_type, metadata))

    recorder = Recorder()
    engine = TradingEngine(risk_manager=RiskManager(RiskLimits(5, 50, 100)),
                           order_manager=manager, portfolio=portfolio, persistence=recorder)
    controller = controls(engine, quotes, (RELIANCE,), now, lambda: health(state[0]))
    decision, order = controller.square_off_intraday()[0]
    assert not decision.approved and decision.reason == "LIVE_FEED_STALE"
    assert order.status is OrderStatus.REJECTED
    assert portfolio.positions[RELIANCE.key].quantity == 10
    assert manager.fills == []
    assert recorder.audit == [
        ("INTRADAY_SQUARE_OFF_FAILED_LIVE_FEED_STALE", {"instrument": RELIANCE.key})
    ]


def test_intent_persists_through_fill_trade_and_restart_and_exports(tmp_path: Path):
    now = [datetime(2026, 9, 1, 10, 0, tzinfo=IST)]
    store = SQLiteStore(SQLiteDatabase(tmp_path / "state.sqlite"))
    sessions = SessionService(store)
    session = sessions.create(date(2026, 9, 1), "LIVE_PAPER", 100_000, timestamp=now[0])
    sessions.start(session.session_id, now[0])
    quotes = LiveQuoteStore()
    broker = LivePaperBroker(quotes, lambda: health(), lambda: now[0])
    manager = OrderManager(broker, iter(("PAPER-BUY", "PAPER-SELL", "PAPER-CANCEL")).__next__)
    portfolio = Portfolio(100_000)
    engine = TradingEngine(
        risk_manager=RiskManager(RiskLimits(5, 50, 100)), order_manager=manager,
        portfolio=portfolio, persistence=sessions.recorder(session.session_id),
    )
    controller = controls(engine, quotes, (RELIANCE,), now)
    quotes.update(quote_tick(RELIANCE, now[0]))
    _, buy = engine.submit_order(request(now, intent=TradeIntent.SWING))
    now[0] += timedelta(seconds=1)
    first = quote_tick(RELIANCE, now[0], 101)
    quotes.update(first); engine.process_live_tick(first)
    _, sell = engine.submit_order(request(now, side=Side.SELL, intent=TradeIntent.SWING))
    now[0] += timedelta(seconds=1)
    second = quote_tick(RELIANCE, now[0], 102)
    quotes.update(second); engine.process_live_tick(second)
    rejected_request = request(now, quantity=10_000, intent=TradeIntent.INTRADAY)
    engine.submit_order(rejected_request)
    sessions.stop(session.session_id, now[0])

    assert buy.intent is sell.intent is TradeIntent.SWING
    assert manager.fills[0].intent is TradeIntent.SWING
    assert portfolio.closed_trades[0].intent is TradeIntent.SWING
    restored = sessions.restore_portfolio(session.session_id)
    assert restored.closed_trades[0].intent is TradeIntent.SWING

    exporter = SessionExporter(store)
    paths = exporter.export(session.session_id, tmp_path / "reports")
    assert {path.name for path in paths} == {
        "session_summary.csv", "orders.csv", "fills.csv", "trades.csv", "positions.csv",
        "risk_events.csv", "event_journal.csv",
    }
    summary = exporter.summary(session.session_id)
    assert (summary.orders, summary.fills, summary.closed_trades, summary.open_positions) == (3, 2, 1, 0)
    assert summary.rejected_requests == 1
    with (tmp_path / "reports" / session.session_id / "trades.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["intent"] == "SWING"
    with (tmp_path / "reports" / session.session_id / "positions.csv").open() as stream:
        assert next(csv.reader(stream)) == [
            "session_id", "symbol", "quantity", "direction", "intent", "average_entry",
            "last_mark", "unrealized_pnl",
        ]
