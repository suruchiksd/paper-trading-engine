from datetime import datetime
from zoneinfo import ZoneInfo

from paper_trading.models import Fill, Instrument, OrderRequest, OrderType, Side
from paper_trading.portfolio import Portfolio
from paper_trading.risk import RiskLimits, RiskManager

NOW = datetime(2026, 7, 31, 9, 15, tzinfo=ZoneInfo("Asia/Kolkata"))
ONE = Instrument("NSE", "ONE", 1)
TWO = Instrument("NSE", "TWO", 2)


def request(instrument=ONE, quantity=10, price=100):
    return OrderRequest(instrument, Side.BUY, OrderType.MARKET, quantity, NOW, reference_price=price)


def manager(maximum_positions=5, exposure=20, loss=3):
    return RiskManager(RiskLimits(maximum_positions, loss, exposure))


def test_approves_valid_order() -> None:
    assert manager().evaluate(request(), Portfolio(10_000)).approved


def test_rejects_exposure_and_insufficient_cash() -> None:
    assert "exposure" in manager(exposure=5).evaluate(request(quantity=10), Portfolio(1_000)).reason
    assert "cash" in manager(exposure=1000).evaluate(request(quantity=11), Portfolio(1_000)).reason


def test_rejects_maximum_positions() -> None:
    portfolio = Portfolio(10_000)
    portfolio.process_fill(Fill("o", ONE, Side.BUY, 1, 100, NOW))
    decision = manager(maximum_positions=1, exposure=100).evaluate(request(TWO), portfolio)
    assert not decision.approved
    assert "positions" in decision.reason


def test_rejects_daily_loss() -> None:
    portfolio = Portfolio(10_000)
    portfolio.cash = 9_600
    decision = manager(loss=3, exposure=100).evaluate(request(), portfolio)
    assert not decision.approved
    assert "daily loss" in decision.reason

