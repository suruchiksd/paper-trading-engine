from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from paper_trading.models import Fill, Instrument, Side
from paper_trading.portfolio import Portfolio

NOW = datetime(2026, 7, 31, 9, 15, tzinfo=ZoneInfo("Asia/Kolkata"))
INSTRUMENT = Instrument("NSE", "TEST", 1)


def fill(side: Side, quantity: int, price: float, minutes: int = 0, fees: float = 0) -> Fill:
    return Fill("order", INSTRUMENT, side, quantity, price, NOW + timedelta(minutes=minutes), fees=fees)


def test_long_weighted_average_partial_reduction_and_close() -> None:
    portfolio = Portfolio(10_000)
    portfolio.process_fill(fill(Side.BUY, 10, 100))
    portfolio.process_fill(fill(Side.BUY, 10, 110, 1))
    position = portfolio.positions[INSTRUMENT.key]
    assert position.quantity == 20
    assert position.average_price == 105

    trades = portfolio.process_fill(fill(Side.SELL, 5, 120, 2))
    assert position.quantity == 15
    assert position.average_price == 105
    assert trades[0].gross_realized_pnl == 75
    assert portfolio.realized_pnl == 75

    portfolio.mark(INSTRUMENT.key, 115)
    assert portfolio.unrealized_pnl == 150
    portfolio.process_fill(fill(Side.SELL, 15, 100, 3))
    assert position.quantity == 0
    assert position.average_price == 0
    assert portfolio.gross_realized_pnl == 0


def test_short_add_reduce_and_reverse() -> None:
    portfolio = Portfolio(10_000)
    portfolio.process_fill(fill(Side.SELL, 10, 100))
    portfolio.process_fill(fill(Side.SELL, 10, 90, 1))
    position = portfolio.positions[INSTRUMENT.key]
    assert position.quantity == -20
    assert position.average_price == 95
    portfolio.process_fill(fill(Side.BUY, 5, 80, 2))
    assert portfolio.realized_pnl == 75
    assert position.quantity == -15
    portfolio.process_fill(fill(Side.BUY, 20, 100, 3))
    assert position.quantity == 5
    assert position.average_price == 100
    assert len(portfolio.closed_trades) == 2


def test_costs_are_separate_and_allocated_to_closed_trade() -> None:
    portfolio = Portfolio(10_000)
    portfolio.process_fill(fill(Side.BUY, 10, 100, fees=10))
    trade = portfolio.process_fill(fill(Side.SELL, 10, 110, 1, fees=11))[0]
    assert trade.gross_realized_pnl == 100
    assert trade.costs == 21
    assert trade.realized_pnl == 79
    assert portfolio.total_costs == 21
    assert portfolio.realized_pnl == 79
    assert portfolio.equity == pytest.approx(10_079)

