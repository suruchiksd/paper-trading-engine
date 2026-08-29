from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from paper_trading.execution import (
    AmbiguousExecutionError,
    IntrabarAmbiguityPolicy,
    InvalidOrderTransition,
    OrderManager,
    PaperBroker,
    SlippageConfig,
    transition_order,
)
from paper_trading.models import Candle, Instrument, Order, OrderRequest, OrderStatus, OrderType, Side

IST = ZoneInfo("Asia/Kolkata")
INSTRUMENT = Instrument("NSE", "TEST", 1)
T0 = datetime(2026, 7, 31, 9, 15, tzinfo=IST)
T1 = datetime(2026, 7, 31, 9, 16, tzinfo=IST)


def candle(timestamp=T1, open=100, high=105, low=95, close=101):
    return Candle(INSTRUMENT, "1minute", timestamp, open, high, low, close)


def request(side=Side.BUY, order_type=OrderType.MARKET, **kwargs):
    return OrderRequest(INSTRUMENT, side, order_type, 10, T0, reference_price=100, **kwargs)


def test_market_order_has_mandatory_next_timestamp_fill() -> None:
    manager = OrderManager(PaperBroker(slippage=SlippageConfig(market_bps=2)))
    order = manager.submit(request())
    assert manager.process_candle(candle(T0)) == ()
    fills = manager.process_candle(candle(T1))
    assert fills[0].price == pytest.approx(100.02)
    assert order.status is OrderStatus.FILLED
    assert order.filled_quantity == 10
    assert manager.process_candle(candle(datetime(2026, 7, 31, 9, 17, tzinfo=IST))) == ()


@pytest.mark.parametrize(
    ("side", "bar", "expected"),
    [
        (Side.BUY, candle(open=102, high=103, low=100, close=101), 101),
        (Side.BUY, candle(open=98, high=102, low=97, close=101), 98.0196),
        (Side.SELL, candle(open=98, high=100, low=97, close=99), 99),
        (Side.SELL, candle(open=102, high=103, low=98, close=101), 101.9796),
    ],
)
def test_limit_fill_rules_and_price_improvement(side, bar, expected) -> None:
    manager = OrderManager(PaperBroker(slippage=SlippageConfig(market_bps=2)))
    manager.submit(request(side, OrderType.LIMIT, limit_price=101 if side is Side.BUY else 99))
    fill = manager.process_candle(bar)[0]
    assert fill.price == pytest.approx(expected)
    if side is Side.BUY:
        assert fill.price <= 101
    else:
        assert fill.price >= 99


def test_untouched_limit_remains_open() -> None:
    manager = OrderManager(PaperBroker())
    order = manager.submit(request(order_type=OrderType.LIMIT, limit_price=90))
    assert manager.process_candle(candle()) == ()
    assert order.status is OrderStatus.OPEN


def test_stop_market_normal_trigger_and_gap_use_adverse_slippage() -> None:
    manager = OrderManager(PaperBroker(slippage=SlippageConfig(stop_market_bps=10)))
    manager.submit(request(Side.SELL, OrderType.STOP_MARKET, stop_price=99))
    assert manager.process_candle(candle(open=101, low=98))[0].price == pytest.approx(98.901)

    second = OrderManager(PaperBroker(slippage=SlippageConfig(stop_market_bps=10)))
    second.submit(request(Side.SELL, OrderType.STOP_MARKET, stop_price=99))
    assert second.process_candle(candle(open=95, high=97, low=94, close=96))[0].price == pytest.approx(94.905)


def test_buy_stop_is_symmetric() -> None:
    manager = OrderManager(PaperBroker(slippage=SlippageConfig(stop_market_bps=10)))
    manager.submit(request(Side.BUY, OrderType.STOP_MARKET, stop_price=101))
    assert manager.process_candle(candle(open=105, high=106, low=104, close=105))[0].price == pytest.approx(105.105)


def test_cancellation_and_invalid_terminal_transition() -> None:
    manager = OrderManager(PaperBroker())
    order = manager.submit(request())
    manager.cancel(order.order_id, T1)
    assert order.status is OrderStatus.CANCELLED
    with pytest.raises(InvalidOrderTransition):
        transition_order(order, OrderStatus.FILLED, T1)


def _oco_manager(policy: IntrabarAmbiguityPolicy):
    manager = OrderManager(PaperBroker(ambiguity_policy=policy))
    stop = manager.submit(
        request(Side.SELL, OrderType.STOP_MARKET, stop_price=95, oco_group_id="exit")
    )
    target = manager.submit(request(Side.SELL, OrderType.LIMIT, limit_price=105, oco_group_id="exit"))
    return manager, stop, target


def test_oco_ambiguity_conservative_selects_adverse_exit() -> None:
    manager, stop, target = _oco_manager(IntrabarAmbiguityPolicy.CONSERVATIVE)
    fill = manager.process_candle(candle(open=100, high=106, low=94))[0]
    assert fill.order_id == stop.order_id
    assert fill.price == 95
    assert target.status is OrderStatus.CANCELLED


def test_oco_ambiguity_optimistic_and_error() -> None:
    manager, _, target = _oco_manager(IntrabarAmbiguityPolicy.OPTIMISTIC)
    assert manager.process_candle(candle(open=100, high=106, low=94))[0].order_id == target.order_id
    manager, _, _ = _oco_manager(IntrabarAmbiguityPolicy.ERROR)
    with pytest.raises(AmbiguousExecutionError):
        manager.process_candle(candle(open=100, high=106, low=94))
