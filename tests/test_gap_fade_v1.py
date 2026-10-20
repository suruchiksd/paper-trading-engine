from __future__ import annotations

import csv
import math
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq
import pytest

from paper_trading.adapters.gap_fade_v1 import (
    GapFadeDecision,
    OpeningWindow,
    PreviousClose,
    evaluate_symbol,
    load_gap_fade_config,
    position_size,
    select_portfolio_candidates,
    verify_frozen_research_identity,
)
from paper_trading.adapters.gap_fade_v1.adapter import apply_entry_geometry
from paper_trading.adapters.gap_fade_v1.live import (
    GapFadePaperRuntime,
    fetch_previous_valid_closes,
)
from paper_trading.adapters.gap_fade_v1.replay import replay_frozen_trade
from paper_trading.engine import TradingEngine
from paper_trading.events import CandleEvent, TickEvent
from paper_trading.execution import (
    LiveExecutionConfig,
    LivePaperBroker,
    OrderManager,
    ZerodhaNseCashIntradayCosts,
)
from paper_trading.market_data import LiveQuoteStore
from paper_trading.market_data.health import FeedHealthSnapshot, FeedState
from paper_trading.models import Candle, Instrument, OrderRequest, OrderStatus, OrderType, Side, Tick
from paper_trading.portfolio import Portfolio
from paper_trading.risk import RiskLimits, RiskManager
from scripts.run_gap_fade_v1_live_paper import acquire_session_lock, parse_args, resolve_instruments

ROOT = Path(__file__).resolve().parents[1]
RESEARCH = Path("/home/suruchi-pandey/Projects/sector-intraday-strategy")
DATA = Path("/home/suruchi-pandey/market-data-platform-gpt/data")
IST = ZoneInfo("Asia/Kolkata")
CONFIG = load_gap_fade_config(ROOT / "config/gap_fade_v1.yaml")


def instrument(symbol="HINDZINC", token=1):
    return Instrument("NSE", symbol, token)


def opening_bars(symbol="HINDZINC", *, day=date(2026, 10, 19), open_=102.0,
                 close_0929=100.8):
    result = []
    for offset in range(15):
        value = close_0929 if offset == 14 else open_
        result.append(Candle(
            instrument(symbol), "1minute",
            datetime.combine(day, datetime.min.time(), IST).replace(hour=9, minute=15) + timedelta(minutes=offset),
            open_ if offset == 0 else value, max(open_, value), min(open_, value), value, 100,
        ))
    return result


def opening_snapshot(symbol="HINDZINC", **kwargs):
    bars = opening_bars(symbol, **kwargs)
    window = OpeningWindow()
    for bar in bars:
        window.ingest(bar)
    return window.snapshot(symbol, bars[0].timestamp.date())


def healthy():
    return FeedHealthSnapshot(FeedState.HEALTHY, True, None, None, 1, 0, 0, 0, 0, 0, 0, None)


def test_frozen_hashes_and_universe_are_exact():
    identity = verify_frozen_research_identity(CONFIG)
    assert identity["selected_strategy_sha256"] == "a54e28b50089567f78fd1b33c12048ee21eedfefb6282c88e5abd131adfc8bc8"
    assert identity["phase7b_config_sha256"] == "594f3ff8964f7b637b6f282d0f5475be4c9a6a4a1a17c46c25da6594a77a2ce4"
    assert len(CONFIG.symbols) == len(set(CONFIG.symbols)) == 115


def test_positive_gap_and_retention_boundaries_are_frozen():
    day = date(2026, 10, 19)
    row = evaluate_symbol(
        config=CONFIG, symbol="HINDZINC", trading_date=day,
        opening=opening_snapshot(open_=101.6, close_0929=100.7),
        previous_close=PreviousClose(100, day - timedelta(days=3)),
    )
    assert row.eligible
    assert row.gap_pct == pytest.approx(0.016)
    assert row.retention == pytest.approx(0.7 / 1.6)
    rejected = evaluate_symbol(
        config=CONFIG, symbol="HINDZINC", trading_date=day,
        opening=opening_snapshot(open_=102, close_0929=101),
        previous_close=PreviousClose(100, day - timedelta(days=3)),
    )
    assert not rejected.eligible
    assert rejected.retention == pytest.approx(0.5)


def test_previous_close_must_be_strictly_prior_valid_session():
    day = date(2026, 10, 19)
    row = evaluate_symbol(
        config=CONFIG, symbol="HINDZINC", trading_date=day,
        opening=opening_snapshot(), previous_close=PreviousClose(100, day),
    )
    assert row.rejection_reason == "PREVIOUS_VALID_CLOSE_UNAVAILABLE"


def test_previous_close_read_is_prior_only_and_failures_are_isolated():
    class Kite:
        def historical_data(self, token, *_args, **_kwargs):
            if token == 2:
                raise RuntimeError("isolated")
            return [
                {"date": datetime(2026, 10, 16, tzinfo=IST), "close": 99},
                {"date": datetime(2026, 10, 19, tzinfo=IST), "close": 101},
            ]
    instruments = {"HINDZINC": instrument(token=1), "TCS": instrument("TCS", 2)}
    found, failed = fetch_previous_valid_closes(
        Kite(), instruments, date(2026, 10, 19), throttle_seconds=0, sleeper=lambda _: None
    )
    assert found["HINDZINC"] == PreviousClose(99, date(2026, 10, 16))
    assert "TCS" in failed


def test_0929_completed_bar_cutoff_requires_all_fifteen_bars_and_deduplicates():
    bars = opening_bars()
    window = OpeningWindow()
    for bar in bars[:-1]:
        assert window.ingest(bar)
    assert window.snapshot("HINDZINC", bars[0].timestamp.date()) is None
    assert window.ingest(bars[-1])
    assert not window.ingest(bars[-1])
    snapshot = window.snapshot("HINDZINC", bars[0].timestamp.date())
    assert snapshot.source_end.time() == time(9, 29)
    future = Candle(instrument(), "1minute", bars[-1].timestamp + timedelta(minutes=1),
                    999, 999, 999, 999)
    assert not window.ingest(future)
    assert window.snapshot("HINDZINC", bars[0].timestamp.date()).price_0929 != 999


def test_signal_is_short_only_and_stop_is_open_plus_two_bps():
    row = evaluate_symbol(
        config=CONFIG, symbol="HINDZINC", trading_date=date(2026, 10, 19),
        opening=opening_snapshot(), previous_close=PreviousClose(100, date(2026, 10, 16)),
    )
    apply_entry_geometry(row, CONFIG, raw_entry_reference=101, available_notional=400_000)
    assert row.stop == pytest.approx(102 * 1.0002)
    assert CONFIG.raw["signal"]["direction"] == "SHORT"


def test_risk_geometry_is_inclusive_and_position_size_has_all_caps():
    assert CONFIG.risk_pct_minimum == 0.002
    assert CONFIG.risk_pct_maximum == 0.0125
    assert position_size(
        risk_rupees=500, entry_price=100, stop_price=101,
        maximum_position_notional=15_000, available_notional=9_900,
    ) == 99
    assert position_size(
        risk_rupees=500, entry_price=101, stop_price=100,
        maximum_position_notional=150_000, available_notional=400_000,
    ) == 0


def _decision(symbol, sector, score):
    row = GapFadeDecision(symbol, sector, "2026-10-19T09:30:00+05:30",
                          "2026-10-19T09:30:00+05:30", eligible=True,
                          priority_score=score, quantity=10)
    return row


def test_ranking_max_two_and_sector_concurrency():
    rows = [_decision("AXISBANK", "banks_BN", 3),
            _decision("SBIN", "banks_BN", 2),
            _decision("TCS", "software_SW", 1)]
    selected = select_portfolio_candidates(
        rows, maximum_positions=2, maximum_positions_per_sector=1
    )
    assert [row.symbol for row in selected] == ["AXISBANK", "TCS"]
    assert rows[1].rejection_reason == "SECTOR_CONCURRENCY_LIMIT"


def test_priority_tie_breaker_is_symbol_ascending_and_one_decision_per_symbol():
    rows = [_decision("TCS", "software_SW", 1), _decision("INFY", "software_SW2", 1)]
    selected = select_portfolio_candidates(rows, maximum_positions=2, maximum_positions_per_sector=1)
    assert [row.symbol for row in selected] == ["INFY", "TCS"]


def test_duplicate_symbol_can_never_be_selected_twice():
    rows = [_decision("TCS", "software_SW", 2), _decision("TCS", "software_SW", 1)]
    selected = select_portfolio_candidates(rows, maximum_positions=2, maximum_positions_per_sector=2)
    assert len(selected) == 1
    assert rows[1].rejection_reason == "ONE_TRADE_PER_SYMBOL_SESSION"


def _read_day(symbol: str, day: date):
    path = DATA / "equities" / f"symbol={symbol}" / f"year={day.year}" / "candles.parquet"
    table = pq.read_table(path, columns=["timestamp", "open", "high", "low", "close", "volume"])
    result = []
    inst = instrument(symbol)
    for row in table.to_pylist():
        timestamp = row["timestamp"]
        if timestamp.date() == day:
            result.append(Candle(inst, "1minute", timestamp, float(row["open"]), float(row["high"]),
                                 float(row["low"]), float(row["close"]), int(row["volume"])))
    return result


@pytest.mark.parametrize("symbol,day", [
    ("HINDZINC", date(2020, 1, 9)),
    ("WIPRO", date(2020, 3, 2)),
    ("TCS", date(2020, 3, 27)),
])
def test_research_paper_parity_on_known_frozen_trades(symbol, day):
    ledger = pq.read_table(
        RESEARCH / "outputs/backtests/phase7b/development/phase7b_trade_ledger.parquet"
    ).to_pylist()
    source = next(row for row in ledger if row["cell_id"] == "BASE_0930_OPEN_EOD"
                  and row["universe_scope"] == "ALL" and row["symbol"] == symbol
                  and row["trade_date"] == day)
    result = replay_frozen_trade(
        CONFIG, symbol=symbol, candles=_read_day(symbol, day),
        previous_close=PreviousClose(float(source["feature_previous_close"]),
                                     source["feature_previous_close_source_end"].date()),
    )
    assert result.eligible
    assert result.gap_pct == pytest.approx(source["feature_gap_pct"])
    assert result.retention == pytest.approx(source["feature_gap_retention"])
    assert result.entry_price_raw == pytest.approx(source["entry_price_raw"])
    assert result.stop == pytest.approx(source["stop"])
    assert result.risk_pct_raw == pytest.approx(source["risk_pct_raw"])
    assert result.exit_price_raw == pytest.approx(source["exit_price_raw"])
    assert result.exit_reason == source["exit_reason"]
    assert result.quantity == min(
        math.floor(500 / (result.stop - result.entry_price_slipped)),
        math.floor(150_000 / result.entry_price_slipped),
        math.floor(400_000 / result.entry_price_slipped),
    )


def test_replay_entry_is_exact_next_bar_and_eod_is_1500():
    day = date(2026, 10, 19)
    bars = opening_bars()
    bars.append(Candle(instrument(), "1minute", datetime(2026, 10, 19, 9, 30, tzinfo=IST),
                       101, 101.2, 100.5, 100.8))
    bars.append(Candle(instrument(), "1minute", datetime(2026, 10, 19, 15, 0, tzinfo=IST),
                       99, 99.2, 98.8, 99))
    result = replay_frozen_trade(
        CONFIG, symbol="HINDZINC", candles=bars,
        previous_close=PreviousClose(100, day - timedelta(days=3)),
    )
    assert result.entry_timestamp.time() == time(9, 30)
    assert result.exit_timestamp.time() == time(15, 0)
    assert result.exit_reason == "EOD"


def test_cost_accounting_has_every_required_component():
    costs = ZerodhaNseCashIntradayCosts().round_trip(
        side="SHORT", quantity=100, entry_price=100, exit_price=99
    )
    assert costs.statutory_broker_cost > 0
    for field in ("brokerage", "stt", "nse_transaction_charge", "sebi_charge",
                  "stamp_duty", "gst", "ipft"):
        assert getattr(costs, field) >= 0


def _runtime(tmp_path, now, *, resumed_decision=True):
    quotes = LiveQuoteStore()
    broker = LivePaperBroker(
        quotes, healthy, lambda: now[0],
        config=LiveExecutionConfig(allow_ltp_fallback=False, market_slippage_bps=5,
                                   stop_market_slippage_bps=5),
        cost_model=ZerodhaNseCashIntradayCosts(),
    )
    manager = OrderManager(broker, order_id_factory=(f"PAPER-{n}" for n in range(20)).__next__)
    portfolio = Portfolio(400_000)
    engine = TradingEngine(
        risk_manager=RiskManager(RiskLimits(2, 0.375, 37.5)),
        order_manager=manager, portfolio=portfolio,
    )
    inst = instrument()
    runtime = GapFadePaperRuntime(
        CONFIG, engine, quotes, {inst.tradingsymbol: inst}, {}, tmp_path,
        trading_date=now[0].date(), clock=lambda: now[0], resumed_decision=resumed_decision,
    )
    return runtime, engine, quotes, inst


def test_entry_fill_creates_fixed_stop_and_1500_tick_forces_flat(tmp_path):
    now = [datetime(2026, 10, 19, 9, 31, tzinfo=IST)]
    runtime, engine, quotes, inst = _runtime(tmp_path, now)
    engine.pre_trade_check = None
    request = OrderRequest(
        inst, Side.SELL, OrderType.MARKET, 10, now[0], reference_price=100,
        strategy_id="gap_fade_v1", strategy_version="1.0.0",
        metadata={"role": "ENTRY", "sector": "metals_Mining_MM", "side": "SHORT",
                  "candidate_timestamp": now[0].isoformat(), "signal_timestamp": now[0].isoformat(),
                  "session_open": 101, "stop": 101.0202, "entry_reference": 100},
    )
    engine.submit_order(request)
    entry_tick = Tick(inst, now[0], now[0], 100, 1000, 100, 100.1)
    runtime.handle(TickEvent(entry_tick))
    stops = [order for order in engine.order_manager.broker.active_orders()
             if order.metadata.get("role") == "STOP"]
    assert len(stops) == 1
    assert stops[0].stop_price == pytest.approx(101.0202)
    now[0] = now[0].replace(hour=15, minute=0)
    exit_tick = Tick(inst, now[0], now[0], 99, 2000, 98.9, 99)
    runtime.handle(TickEvent(exit_tick))
    assert not engine.portfolio.open_positions
    assert not engine.order_manager.broker.active_orders()
    report = runtime.write_reports()
    assert report["EOD_flat_pass"] is True
    assert report["trades"] == 1


def test_daily_loss_shutdown_prevents_pending_entry_submission(tmp_path):
    now = [datetime(2026, 10, 19, 9, 31, tzinfo=IST)]
    runtime, engine, _quotes, inst = _runtime(tmp_path, now)
    engine.portfolio.cash -= 1500
    row = _decision(inst.tradingsymbol, "metals_Mining_MM", 1)
    row.today_open = 101
    row.stop = 101.0202
    runtime.decisions = {inst.tradingsymbol: row}
    runtime.selected_pending = {inst.tradingsymbol}
    runtime.entries_disabled = engine.portfolio.total_pnl <= -CONFIG.daily_hard_loss
    tick = Tick(inst, now[0], now[0], 100, 1000, 100, 100.1)
    runtime.handle(TickEvent(tick))
    assert row.rejection_reason == "ENTRIES_DISABLED"
    assert not engine.order_manager.orders


def test_restart_restores_decision_without_redeciding_and_outputs_are_schema_complete(tmp_path):
    now = [datetime(2026, 10, 19, 9, 31, tzinfo=IST)]
    runtime, engine, quotes, inst = _runtime(tmp_path, now)
    row = _decision(inst.tradingsymbol, "metals_Mining_MM", 1)
    runtime.decisions = {inst.tradingsymbol: row}
    runtime._persist_decision("DECISION_COMMITTED_BEFORE_ORDER_SUBMISSION", now[0])
    resumed = GapFadePaperRuntime(
        CONFIG, engine, quotes, {inst.tradingsymbol: inst}, {}, tmp_path,
        trading_date=now[0].date(), clock=lambda: now[0], resumed_decision=True,
    )
    assert resumed.decision_committed
    assert list(resumed.decisions) == [inst.tradingsymbol]
    for filename in ("signal_log.csv", "rejection_log.csv", "trade_ledger.csv",
                     "daily_report.json", "decision_state.json"):
        assert (tmp_path / filename).exists()
    with (tmp_path / "trade_ledger.csv").open(newline="") as stream:
        assert set(csv.DictReader(stream).fieldnames or ()) >= {
            "gross_pnl", "statutory_broker_cost", "slippage_cost", "total_cost", "net_pnl"
        }


def test_restart_before_0930_remains_eligible_to_make_one_decision(tmp_path):
    now = [datetime(2026, 10, 19, 9, 0, tzinfo=IST)]
    runtime, engine, quotes, inst = _runtime(tmp_path, now, resumed_decision=False)
    resumed = GapFadePaperRuntime(
        CONFIG, engine, quotes, {inst.tradingsymbol: inst}, {}, tmp_path,
        trading_date=now[0].date(), clock=lambda: now[0], resumed_decision=False,
    )
    assert not runtime.decision_committed
    assert not resumed.decision_committed
    assert not resumed.decisions


def test_instrument_resolution_fails_closed_per_symbol(tmp_path):
    master = tmp_path / "master.csv"
    master.write_text("instrument_token,exchange,tradingsymbol,segment,tick_size,lot_size,instrument_type\n1,NSE,ABB,NSE,0.05,1,EQ\n")
    mini = SimpleNamespace(symbols=("ABB", "MISSING"))
    resolved, unresolved = resolve_instruments(mini, master)
    assert set(resolved) == {"ABB"}
    assert set(unresolved) == {"MISSING"}


def test_duplicate_process_lock_fails_closed(tmp_path):
    lock_path = tmp_path / "session.lock"
    first = acquire_session_lock(lock_path)
    try:
        with pytest.raises(BlockingIOError):
            acquire_session_lock(lock_path)
    finally:
        first.close()


def test_runner_fails_closed_for_any_non_paper_mode_and_has_no_order_api_call():
    with pytest.raises(SystemExit):
        parse_args(["--mode", "live"])
    source = (ROOT / "scripts/run_gap_fade_v1_live_paper.py").read_text()
    assert ".place_order(" not in source
    assert "choices=[\"paper\"]" in source
