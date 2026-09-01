from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from paper_trading.execution import LiveExecutionConfig, LivePaperBroker, OrderManager
from paper_trading.engine import TradingEngine
from paper_trading.manual import LiveOrderGate, ManualAction, ManualCommandError, parse_manual_command
from paper_trading.market_data import LiveQuoteStore
from paper_trading.market_data.health import FeedHealthSnapshot, FeedState
from paper_trading.models import Instrument, OrderRequest, OrderStatus, OrderType, Side, Tick
from paper_trading.portfolio import Portfolio
from paper_trading.risk import RiskLimits, RiskManager
from paper_trading.storage import SQLiteDatabase, SQLiteStore, SessionService

IST = ZoneInfo("Asia/Kolkata")


def healthy() -> FeedHealthSnapshot:
    return FeedHealthSnapshot(FeedState.HEALTHY, True, None, None, 1, 0, 0, 0, 0, 0, 0, None)


def tick(at, *, ltp=100.0, bid=99.9, ask=100.1):
    return Tick(Instrument("NSE", "RELIANCE", 1), at, at, ltp, 1000, bid, ask)


def submit(manager, at, side, kind, *, limit=None, stop=None):
    return manager.submit(OrderRequest(
        tick(at).instrument, side, kind, 10, at, reference_price=100,
        limit_price=limit, stop_price=stop,
    ))


def test_live_market_uses_executable_side_and_adverse_slippage():
    now = datetime(2026, 9, 1, 10, 0, tzinfo=IST)
    quotes = LiveQuoteStore()
    broker = LivePaperBroker(quotes, healthy, lambda: now,
                             config=LiveExecutionConfig(market_slippage_bps=10))
    manager = OrderManager(broker, lambda: "PAPER-1")
    order = submit(manager, now, Side.BUY, OrderType.MARKET)
    market_tick = tick(now, bid=99, ask=101)
    quotes.update(market_tick)
    fills = manager.process_tick(market_tick)
    assert fills[0].price == pytest.approx(101.101)
    assert order.status is OrderStatus.FILLED
    assert order.metadata["live_execution"]["fallback"] is None


def test_live_sell_market_and_limit_use_bid():
    now = datetime(2026, 9, 1, 10, 0, tzinfo=IST)
    quotes = LiveQuoteStore()
    broker = LivePaperBroker(quotes, healthy, lambda: now)
    manager = OrderManager(broker)
    market = submit(manager, now, Side.SELL, OrderType.MARKET)
    first = tick(now, bid=99, ask=100)
    quotes.update(first)
    assert manager.process_tick(first)[0].price == 99
    limit = submit(manager, now, Side.SELL, OrderType.LIMIT, limit=101)
    quotes.update(tick(now + timedelta(seconds=1), ltp=100, bid=100, ask=100.1))
    assert manager.process_tick(tick(now + timedelta(seconds=1), ltp=100, bid=100, ask=100.1)) == ()
    crossed = tick(now + timedelta(seconds=2), ltp=102, bid=102, ask=102.1)
    quotes.update(crossed)
    assert manager.process_tick(crossed)[0].order_id == limit.order_id


def test_live_limit_respects_limit_and_stop_uses_jump_quote():
    now = datetime(2026, 9, 1, 10, 0, tzinfo=IST)
    quotes = LiveQuoteStore()
    broker = LivePaperBroker(quotes, healthy, lambda: now,
                             config=LiveExecutionConfig(market_slippage_bps=20,
                                                        stop_market_slippage_bps=10))
    manager = OrderManager(broker)
    limit = submit(manager, now, Side.BUY, OrderType.LIMIT, limit=100)
    first = tick(now, ask=101)
    quotes.update(first)
    assert manager.process_tick(first) == ()
    second = tick(now + timedelta(seconds=1), bid=98.9, ask=99)
    quotes.update(second)
    assert manager.process_tick(second)[0].price <= 100
    stop = submit(manager, now, Side.SELL, OrderType.STOP_MARKET, stop=98)
    jumped = tick(now + timedelta(seconds=2), ltp=95, bid=94, ask=95.1)
    quotes.update(jumped)
    fill = manager.process_tick(jumped)[0]
    assert fill.order_id == stop.order_id
    assert fill.price == pytest.approx(93.906)
    assert limit.status is OrderStatus.FILLED


def test_missing_depth_fallback_is_explicit_and_can_be_disabled():
    now = datetime(2026, 9, 1, 10, 0, tzinfo=IST)
    quotes = LiveQuoteStore()
    broker = LivePaperBroker(quotes, healthy, lambda: now,
                             config=LiveExecutionConfig(allow_ltp_fallback=True))
    manager = OrderManager(broker)
    order = submit(manager, now, Side.SELL, OrderType.MARKET)
    market_tick = tick(now, bid=None, ask=None)
    quotes.update(market_tick)
    assert manager.process_tick(market_tick)
    assert order.metadata["live_execution"]["fallback"] == "LAST_PRICE_NO_DEPTH"


def test_stale_health_leaves_existing_order_open_until_fresh_tick():
    now = datetime(2026, 9, 1, 10, 0, tzinfo=IST)
    state = [FeedState.STALE]
    from dataclasses import replace
    health = lambda: replace(healthy(), state=state[0])
    quotes = LiveQuoteStore()
    broker = LivePaperBroker(quotes, health, lambda: now)
    manager = OrderManager(broker)
    order = submit(manager, now, Side.BUY, OrderType.MARKET)
    market_tick = tick(now)
    quotes.update(market_tick)
    assert manager.process_tick(market_tick) == ()
    assert order.status is OrderStatus.OPEN
    state[0] = FeedState.HEALTHY
    assert manager.process_tick(market_tick)


def test_live_order_gate_rejects_stale_and_unsubscribed_symbols():
    from dataclasses import replace
    now = datetime(2026, 9, 1, 10, 0, tzinfo=IST)
    instrument = tick(now).instrument
    quotes = LiveQuoteStore()
    quotes.update(tick(now))
    stale = lambda: replace(healthy(), state=FeedState.STALE)
    gate = LiveOrderGate([instrument], quotes, stale, lambda: now, 5)
    request = OrderRequest(instrument, Side.BUY, OrderType.MARKET, 1, now, reference_price=100)
    assert gate.check(request).reason == "LIVE_FEED_STALE"
    other = Instrument("NSE", "TCS", 3)
    rejected = gate.check(OrderRequest(other, Side.BUY, OrderType.MARKET, 1, now,
                                       reference_price=100))
    assert rejected.reason == "SYMBOL_NOT_SUBSCRIBED"


def test_live_session_restart_restores_ledger_and_does_not_infer_offline_fills(tmp_path):
    now = datetime(2026, 9, 1, 10, 0, tzinfo=IST)
    database = SQLiteDatabase(tmp_path / "live.sqlite")
    store = SQLiteStore(database)
    sessions = SessionService(store)
    session = sessions.create(date(2026, 9, 1), "LIVE_PAPER", 100_000, timestamp=now)
    sessions.start(session.session_id, now)
    quotes = LiveQuoteStore()
    broker = LivePaperBroker(quotes, healthy, lambda: now)
    ids = iter(("PAPER-BUY", "PAPER-LIMIT"))
    manager = OrderManager(broker, lambda: next(ids))
    portfolio = Portfolio(100_000)
    engine = TradingEngine(
        risk_manager=RiskManager(RiskLimits(5, 50, 100)), order_manager=manager,
        portfolio=portfolio, persistence=sessions.recorder(session.session_id),
    )
    request = OrderRequest(tick(now).instrument, Side.BUY, OrderType.MARKET, 10, now,
                           reference_price=100)
    decision, opened = engine.submit_order(request)
    assert decision.approved
    live_tick = tick(now, ltp=101, bid=100.9, ask=101.1)
    quotes.update(live_tick)
    engine.process_live_tick(live_tick)
    pending_request = OrderRequest(tick(now).instrument, Side.BUY, OrderType.LIMIT, 2, now,
                                   reference_price=101, limit_price=90)
    _, pending = engine.submit_order(pending_request)
    assert store.counts(session.session_id)["fills"] == 1

    fresh_quotes = LiveQuoteStore()
    recovered_broker = LivePaperBroker(fresh_quotes, healthy, lambda: now + timedelta(minutes=10))
    recovered = sessions.resume(session.session_id, recovered_broker)
    assert recovered.portfolio.cash == portfolio.cash
    assert recovered.order_manager.orders[opened.order_id].status is OrderStatus.FILLED
    assert recovered.order_manager.orders[pending.order_id].status is OrderStatus.OPEN
    assert len(recovered_broker.active_orders()) == 1
    assert store.counts(session.session_id)["fills"] == 1
    # No offline market path is consulted. Only a newly injected post-resume tick can fill.
    assert recovered.order_manager.fills == manager.fills
    resumed_tick = tick(now + timedelta(minutes=10), ltp=89, bid=88.9, ask=89)
    fresh_quotes.update(resumed_tick)
    assert recovered.order_manager.process_tick(resumed_tick)


@pytest.mark.parametrize("text,action", [
    ("buy RELIANCE 10 market", ManualAction.BUY),
    ("sell SBIN 2 limit 812.5", ManualAction.SELL),
    ("sell RELIANCE 1 stop 98", ManualAction.SELL),
    ("cancel PAPER-1", ManualAction.CANCEL),
    ("positions", ManualAction.POSITIONS), ("orders", ManualAction.ORDERS),
    ("pnl", ManualAction.PNL), ("health", ManualAction.HEALTH),
])
def test_manual_command_parser(text, action):
    assert parse_manual_command(text).action is action


@pytest.mark.parametrize("text", ["buy X 0 market", "buy X 1 limit nope", "wat"])
def test_manual_command_errors(text):
    with pytest.raises(ManualCommandError):
        parse_manual_command(text)
