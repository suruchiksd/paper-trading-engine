from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from paper_trading.engine import TradingEngine
from paper_trading.execution import OrderManager, PaperBroker, SlippageConfig
from paper_trading.market_data import HistoricalReplayFeed
from paper_trading.models import Candle, Instrument, OrderRequest, OrderStatus, OrderType, Side
from paper_trading.portfolio import Portfolio
from paper_trading.risk import RiskLimits, RiskManager

IST = ZoneInfo("Asia/Kolkata")
INSTRUMENT = Instrument("NSE", "TEST", 1)


class MemoryRepository:
    def load_candles(self, instrument, start_date, end_date=None):
        return [
            Candle(instrument, "1minute", datetime(2026, 7, 31, 9, 15, tzinfo=IST), 100, 102, 99, 101),
            Candle(instrument, "1minute", datetime(2026, 7, 31, 9, 16, tzinfo=IST), 102, 104, 101, 103),
        ]


def test_replay_manual_order_uses_next_bar_then_marks_close() -> None:
    broker = PaperBroker(slippage=SlippageConfig(market_bps=10))
    orders = OrderManager(broker)
    portfolio = Portfolio(10_000)
    engine = TradingEngine(
        risk_manager=RiskManager(RiskLimits(5, 10, 100)),
        order_manager=orders,
        portfolio=portfolio,
    )
    submitted = []

    def manual_control(candle, current_engine):
        if candle.timestamp.minute == 15:
            decision, order = current_engine.submit_order(
                OrderRequest(
                    INSTRUMENT,
                    Side.BUY,
                    OrderType.MARKET,
                    10,
                    candle.timestamp,
                    reference_price=candle.close,
                )
            )
            assert decision.approved
            submitted.append(order)
            assert not current_engine.fill_events

    count = engine.replay(
        HistoricalReplayFeed(MemoryRepository(), [INSTRUMENT], date(2026, 7, 31)),
        after_candle=manual_control,
    )
    assert count == 2
    assert submitted[0].status is OrderStatus.FILLED
    assert engine.fill_events[0].fill.timestamp.minute == 16
    assert engine.fill_events[0].fill.price == pytest.approx(102.102)
    assert portfolio.positions[INSTRUMENT.key].quantity == 10
    assert portfolio.positions[INSTRUMENT.key].last_price == 103
    assert portfolio.unrealized_pnl == pytest.approx(8.98)

