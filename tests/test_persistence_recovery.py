from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from paper_trading.engine import TradingEngine
from paper_trading.execution import (
    BasisPointsTransactionCosts,
    OrderManager,
    PaperBroker,
    SlippageConfig,
)
from paper_trading.models import Candle, Instrument, OrderRequest, OrderStatus, OrderType, Side
from paper_trading.portfolio import Portfolio
from paper_trading.risk import RiskLimits, RiskManager
from paper_trading.storage import (
    DuplicateFillError,
    RecoveryConsistencyError,
    SQLiteDatabase,
    SQLiteStore,
    SessionService,
)

IST = ZoneInfo("Asia/Kolkata")
INSTRUMENT = Instrument("NSE", "TEST", 1)


def candle(minute: int, open=100, high=105, low=95, close=101) -> Candle:
    return Candle(
        INSTRUMENT,
        "1minute",
        datetime(2026, 7, 31, 9, minute, tzinfo=IST),
        open,
        high,
        low,
        close,
    )


def setup_runtime(path: Path, *, costs_bps=0):
    store = SQLiteStore(SQLiteDatabase(path))
    sessions = SessionService(store)
    session = sessions.create(date(2026, 7, 31), "PAPER_REPLAY", 10_000)
    sessions.start(session.session_id)
    broker = PaperBroker(
        slippage=SlippageConfig(), cost_model=BasisPointsTransactionCosts(costs_bps)
    )
    manager = OrderManager(broker)
    portfolio = Portfolio(10_000)
    engine = TradingEngine(
        risk_manager=RiskManager(RiskLimits(5, 20, 100)),
        order_manager=manager,
        portfolio=portfolio,
        persistence=sessions.recorder(session.session_id),
    )
    return store, sessions, session, engine


def submit(engine: TradingEngine, bar: Candle, side=Side.BUY, order_type=OrderType.MARKET, **kwargs):
    return engine.submit_order(
        OrderRequest(
            INSTRUMENT,
            side,
            order_type,
            kwargs.pop("quantity", 10),
            bar.timestamp,
            reference_price=bar.close,
            **kwargs,
        )
    )[1]


def test_mid_session_restart_restores_position_open_order_and_continues(tmp_path: Path) -> None:
    store, sessions, session, engine = setup_runtime(tmp_path / "state.sqlite")
    first = candle(15)
    engine.process_candle(first)
    market = submit(engine, first)
    second = candle(16, open=102, high=104, low=101, close=103)
    engine.process_candle(second)
    pending = submit(engine, second, order_type=OrderType.LIMIT, quantity=5, limit_price=90)
    third = candle(17, open=104, high=105, low=100, close=104)
    engine.process_candle(third)

    cash_before = engine.portfolio.cash
    recovered = sessions.resume(session.session_id, PaperBroker())
    assert recovered.portfolio.cash == cash_before
    assert recovered.portfolio.positions[INSTRUMENT.key].quantity == 10
    assert recovered.portfolio.positions[INSTRUMENT.key].last_price == 104
    assert recovered.order_manager.orders[market.order_id].status is OrderStatus.FILLED
    assert [order.order_id for order in recovered.order_manager.broker.active_orders()] == [pending.order_id]
    assert store.counts(session.session_id)["fills"] == 1

    again = sessions.resume(session.session_id, PaperBroker())
    assert again.portfolio.cash == cash_before
    assert store.counts(session.session_id)["fills"] == 1

    resumed_engine = TradingEngine(
        risk_manager=RiskManager(RiskLimits(5, 20, 100)),
        order_manager=recovered.order_manager,
        portfolio=recovered.portfolio,
        persistence=sessions.recorder(session.session_id),
    )
    resumed_engine.process_candle(candle(18, open=92, high=93, low=89, close=91))
    assert pending.status is not OrderStatus.FILLED  # old discarded object is not reused
    assert recovered.order_manager.orders[pending.order_id].status is OrderStatus.FILLED
    assert recovered.portfolio.positions[INSTRUMENT.key].quantity == 15
    sessions.complete(session.session_id, recovered.portfolio)
    restored_final = sessions.restore_portfolio(session.session_id)
    assert restored_final.cash == recovered.portfolio.cash
    assert restored_final.positions[INSTRUMENT.key].quantity == 15


def test_restart_preserves_first_eligible_candle(tmp_path: Path) -> None:
    _, sessions, session, engine = setup_runtime(tmp_path / "state.sqlite")
    generating = candle(15)
    engine.process_candle(generating)
    order = submit(engine, generating)
    recovered = sessions.resume(session.session_id, PaperBroker())
    resumed = TradingEngine(
        risk_manager=RiskManager(RiskLimits(5, 20, 100)),
        order_manager=recovered.order_manager,
        portfolio=recovered.portfolio,
        persistence=sessions.recorder(session.session_id),
    )
    resumed.process_candle(generating)
    assert recovered.order_manager.orders[order.order_id].status is OrderStatus.OPEN
    resumed.process_candle(candle(16, open=102, high=103, low=101, close=102))
    assert recovered.order_manager.orders[order.order_id].status is OrderStatus.FILLED


def test_terminal_orders_are_not_reactivated(tmp_path: Path) -> None:
    _, sessions, session, engine = setup_runtime(tmp_path / "state.sqlite")
    first = candle(15)
    engine.process_candle(first)
    filled = submit(engine, first)
    cancelled = submit(engine, first, order_type=OrderType.LIMIT, limit_price=80)
    engine.cancel_order(cancelled.order_id, first.timestamp)
    rejected = submit(engine, first, quantity=1000)
    engine.process_candle(candle(16))
    recovered = sessions.resume(session.session_id, PaperBroker())
    assert filled.status is OrderStatus.FILLED
    assert rejected.status is OrderStatus.REJECTED
    assert not recovered.order_manager.broker.active_orders()


def test_duplicate_fill_is_rejected_without_duplicate_economics(tmp_path: Path) -> None:
    store, sessions, session, engine = setup_runtime(tmp_path / "state.sqlite")
    first = candle(15)
    engine.process_candle(first)
    submit(engine, first)
    engine.process_candle(candle(16))
    fill = engine.order_manager.fills[0]
    order = engine.order_manager.orders[fill.order_id]
    with pytest.raises(DuplicateFillError):
        store.record_execution(
            session.session_id, [order], [fill], [], engine.portfolio, fill.timestamp
        )
    assert store.counts(session.session_id)["fills"] == 1
    assert sessions.restore_portfolio(session.session_id).cash == engine.portfolio.cash


def test_recovery_detects_filled_order_without_ledger_fill(tmp_path: Path) -> None:
    store, sessions, session, engine = setup_runtime(tmp_path / "state.sqlite")
    first = candle(15)
    engine.process_candle(first)
    order = submit(engine, first, order_type=OrderType.LIMIT, limit_price=80)
    with store.database.connect() as connection:
        connection.execute(
            "UPDATE orders SET status='FILLED', filled_quantity=10, average_fill_price=80 "
            "WHERE order_id=?",
            (order.order_id,),
        )
    with pytest.raises(RecoveryConsistencyError, match="ledger"):
        sessions.resume(session.session_id, PaperBroker())


def test_fill_ledger_reconstructs_short_reversal_costs_and_trade(tmp_path: Path) -> None:
    _, sessions, session, engine = setup_runtime(tmp_path / "state.sqlite", costs_bps=10)
    first = candle(15)
    engine.process_candle(first)
    submit(engine, first, side=Side.SELL, quantity=10)
    engine.process_candle(candle(16, open=100, close=99))
    submit(engine, candle(16, open=100, close=99), side=Side.BUY, quantity=15)
    engine.process_candle(candle(17, open=90, high=92, low=89, close=91))
    restored = sessions.restore_portfolio(session.session_id)
    assert restored.positions[INSTRUMENT.key].quantity == 5
    assert restored.cash == pytest.approx(engine.portfolio.cash)
    assert restored.realized_pnl == pytest.approx(engine.portfolio.realized_pnl)
    assert restored.total_costs == pytest.approx(engine.portfolio.total_costs)
    assert len(restored.closed_trades) == 1
