"""Live-market-data/local-paper runtime for frozen Gap Fade V1."""

from __future__ import annotations

import csv
import json
import logging
import math
import os
import time as time_module
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable, Mapping, Protocol

from ...engine import TradingEngine
from ...events import CandleEvent, TickEvent
from ...execution import ZerodhaNseCashIntradayCosts
from ...models import Candle, Instrument, OrderRequest, OrderStatus, OrderType, Side, TradeIntent
from .adapter import (
    GapFadeConfig,
    GapFadeDecision,
    IST,
    OpeningWindow,
    PreviousClose,
    apply_entry_geometry,
    evaluate_symbol,
    select_portfolio_candidates,
)

logger = logging.getLogger(__name__)

FUNNEL_FIELDS = list(asdict(GapFadeDecision("", "", "", "")).keys())
TRADE_FIELDS = [
    "date", "symbol", "sector", "side", "qty", "candidate_timestamp",
    "signal_timestamp", "order_timestamp", "fill_timestamp", "entry_timestamp",
    "entry_price", "stop", "exit_timestamp", "exit_price", "exit_reason",
    "gross_pnl", "brokerage", "stt", "nse_transaction_charge", "sebi_charge",
    "stamp_duty", "gst", "ipft", "statutory_broker_cost", "slippage_cost",
    "total_cost", "net_pnl",
]


class KiteHistoricalLike(Protocol):
    def historical_data(self, instrument_token: int, from_date, to_date, interval: str,
                        continuous: bool = False, oi: bool = False) -> list[dict]: ...


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
    os.replace(temporary, path)


def _write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def fetch_previous_valid_closes(
    kite: KiteHistoricalLike,
    instruments: Mapping[str, Instrument],
    trading_date: date,
    *,
    lookback_calendar_days: int = 15,
    throttle_seconds: float = 0.34,
    sleeper: Callable[[float], None] = time_module.sleep,
) -> tuple[dict[str, PreviousClose], dict[str, str]]:
    """Fetch prior exchange daily closes read-only; a symbol failure is isolated."""
    found: dict[str, PreviousClose] = {}
    failures: dict[str, str] = {}
    start = trading_date - timedelta(days=lookback_calendar_days)
    for index, (symbol, instrument) in enumerate(sorted(instruments.items())):
        if instrument.instrument_token is None:
            failures[symbol] = "INSTRUMENT_TOKEN_UNAVAILABLE"
            continue
        try:
            rows = kite.historical_data(
                instrument.instrument_token, start, trading_date, "day", continuous=False, oi=False
            )
            candidates: list[tuple[date, float]] = []
            for row in rows:
                raw_date = row.get("date")
                if isinstance(raw_date, datetime):
                    source_date = raw_date.astimezone(IST).date() if raw_date.tzinfo else raw_date.date()
                elif isinstance(raw_date, date):
                    source_date = raw_date
                elif isinstance(raw_date, str):
                    source_date = datetime.fromisoformat(raw_date).date()
                else:
                    continue
                close = float(row.get("close", 0) or 0)
                if source_date < trading_date and math.isfinite(close) and close > 0:
                    candidates.append((source_date, close))
            if not candidates:
                failures[symbol] = "PREVIOUS_VALID_CLOSE_NOT_RETURNED"
            else:
                source_date, close = max(candidates, key=lambda item: item[0])
                found[symbol] = PreviousClose(close, source_date)
        except Exception as exc:  # individual symbols fail closed without ending the runner
            failures[symbol] = f"HISTORICAL_DATA_ERROR:{type(exc).__name__}:{exc}"
        if throttle_seconds and index + 1 < len(instruments):
            sleeper(throttle_seconds)
    return found, failures


class BarJournal:
    fields = ["timestamp", "exchange", "symbol", "token", "open", "high", "low", "close", "volume"]

    def __init__(self, path: Path) -> None:
        self.path = path
        self.keys: set[tuple[str, str]] = set()

    def restore(self, opening: OpeningWindow, instruments: Mapping[str, Instrument]) -> int:
        if not self.path.exists():
            return 0
        restored = 0
        with self.path.open(newline="") as stream:
            for row in csv.DictReader(stream):
                key = (row["symbol"], row["timestamp"])
                instrument = instruments.get(row["symbol"])
                if key in self.keys or instrument is None:
                    continue
                self.keys.add(key)
                if opening.ingest(Candle(
                    instrument, "1minute", datetime.fromisoformat(row["timestamp"]),
                    float(row["open"]), float(row["high"]), float(row["low"]),
                    float(row["close"]), int(row["volume"]),
                )):
                    restored += 1
        return restored

    def record(self, candle: Candle) -> bool:
        key = (candle.instrument.tradingsymbol, candle.timestamp.isoformat())
        if key in self.keys:
            return False
        self.keys.add(key)
        row = {
            "timestamp": candle.timestamp.isoformat(), "exchange": candle.instrument.exchange,
            "symbol": candle.instrument.tradingsymbol, "token": candle.instrument.instrument_token,
            "open": candle.open, "high": candle.high, "low": candle.low,
            "close": candle.close, "volume": candle.volume,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        exists = self.path.exists() and self.path.stat().st_size > 0
        with self.path.open("a", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=self.fields)
            if not exists:
                writer.writeheader()
            writer.writerow(row)
            stream.flush()
            os.fsync(stream.fileno())
        return True


class GapFadePaperRuntime:
    """Single-decision paper controller; it has no real broker/order client."""

    def __init__(
        self,
        config: GapFadeConfig,
        engine: TradingEngine,
        quotes,
        instruments: Mapping[str, Instrument],
        previous_closes: Mapping[str, PreviousClose],
        output: Path,
        *,
        trading_date: date,
        clock: Callable[[], datetime],
        unresolved: Mapping[str, str] | None = None,
        resumed_decision: bool = False,
    ) -> None:
        self.config = config
        self.engine = engine
        self.quotes = quotes
        self.instruments = dict(instruments)
        self.previous_closes = dict(previous_closes)
        self.output = output
        self.trading_date = trading_date
        self.clock = clock
        self.opening = OpeningWindow()
        self.journal = BarJournal(output / "completed_bars.csv")
        self.decisions: dict[str, GapFadeDecision] = {}
        self.first_executable_bid: dict[str, float] = {}
        self.decision_committed = resumed_decision
        self.entries_disabled = False
        self.fill_cursor = len(engine.fill_events)
        self.selected_pending: set[str] = set()
        self.entry_symbols = {
            order.instrument.tradingsymbol for order in engine.order_manager.orders.values()
            if order.strategy_id == config.raw["strategy"]["id"]
            and order.metadata.get("role") == "ENTRY"
            and order.status is not OrderStatus.REJECTED
        }
        self.unresolved = dict(unresolved or {})
        self._restore_decision_rows()
        self._initialize_outputs()

    def _initialize_outputs(self) -> None:
        self.output.mkdir(parents=True, exist_ok=True)
        for name in ("signal_log.csv", "rejection_log.csv"):
            if not (self.output / name).exists():
                _write_csv(self.output / name, [], FUNNEL_FIELDS)
        if not (self.output / "trade_ledger.csv").exists():
            _write_csv(self.output / "trade_ledger.csv", [], TRADE_FIELDS)
        if not (self.output / "decision_state.json").exists():
            _atomic_json(self.output / "decision_state.json", {
                "strategy_id": self.config.raw["strategy"]["id"],
                "config_sha256": self.config.sha256,
                "trading_date": self.trading_date,
                "state": "AWAITING_COMPLETED_0929_BARS",
                "decisions": [],
            })
        self.write_reports()

    def _restore_decision_rows(self) -> None:
        path = self.output / "decision_state.json"
        if not path.exists():
            return
        payload = json.loads(path.read_text())
        if payload.get("config_sha256") != self.config.sha256:
            raise ValueError("resume decision state uses a different config hash")
        for item in payload.get("decisions", []):
            row = GapFadeDecision(**item)
            self.decisions[row.symbol] = row

    def restore_bars(self) -> int:
        return self.journal.restore(self.opening, self.instruments)

    def handle(self, event) -> None:
        if isinstance(event, CandleEvent):
            candle = event.candle
            local_clock = candle.timestamp.astimezone(IST).time().replace(tzinfo=None)
            if (candle.instrument.tradingsymbol in self.instruments
                    and time(9, 15) <= local_clock <= time(9, 29)
                    and self.journal.record(candle)):
                self.opening.ingest(candle)
            self.engine.process_candle(candle)
            return
        if not isinstance(event, TickEvent):
            return
        tick = event.tick
        observed = (tick.exchange_timestamp or tick.received_timestamp).astimezone(IST)
        symbol = tick.instrument.tradingsymbol
        now_time = observed.time().replace(tzinfo=None)
        self.quotes.update(tick)
        if now_time >= time(15, 0):
            self.entries_disabled = True
            self._square_off(observed)
        else:
            if now_time >= time(9, 30) and symbol in self.instruments and symbol not in self.first_executable_bid:
                if tick.best_bid is not None and tick.best_bid > 0:
                    self.first_executable_bid[symbol] = tick.best_bid
            if not self.decision_committed and now_time >= time(9, 30):
                deadline = time(9, 30, self.config.decision_grace_seconds)
                if self.opening.symbols_complete(self.instruments, self.trading_date) or now_time >= deadline:
                    self._commit_decision(observed)
            if self.decision_committed:
                self._submit_selected_on_tick(tick, observed)
                self._recover_missing_stops(observed)
        self.engine.process_live_tick(tick)
        self._process_new_fills(observed)
        if self.engine.portfolio.total_pnl <= -self.config.daily_hard_loss:
            self.entries_disabled = True

    def _commit_decision(self, observed: datetime) -> None:
        if self.decision_committed:
            return
        rows: list[GapFadeDecision] = []
        for symbol in self.config.symbols:
            if symbol not in self.instruments:
                reason = self.unresolved.get(symbol, "INSTRUMENT_UNRESOLVED")
                signal = datetime.combine(self.trading_date, time(9, 30), IST).isoformat()
                rows.append(GapFadeDecision(
                    symbol, self.config.sector_by_symbol[symbol], signal, signal,
                    rejection_reason=reason,
                ))
                continue
            row = evaluate_symbol(
                config=self.config,
                symbol=symbol,
                trading_date=self.trading_date,
                opening=self.opening.snapshot(symbol, self.trading_date),
                previous_close=self.previous_closes.get(symbol),
            )
            if row.eligible:
                apply_entry_geometry(
                    row, self.config,
                    raw_entry_reference=self.first_executable_bid.get(symbol),
                    available_notional=self.config.max_gross_exposure,
                )
            rows.append(row)
        if self.entries_disabled or self.engine.portfolio.total_pnl <= -self.config.daily_hard_loss:
            for row in rows:
                if row.eligible:
                    row.eligible = False
                    row.rejection_reason = "DAILY_HARD_LOSS_NO_NEW_ENTRIES"
        selected = select_portfolio_candidates(
            rows,
            maximum_positions=min(self.config.max_positions, self.config.max_trades),
            maximum_positions_per_sector=self.config.max_positions_per_sector,
        )
        self.decisions = {row.symbol: row for row in rows}
        self.selected_pending = {row.symbol for row in selected}
        self.decision_committed = True
        self._persist_decision("DECISION_COMMITTED_BEFORE_ORDER_SUBMISSION", observed)

    def _submit_selected_on_tick(self, tick, observed: datetime) -> None:
        symbol = tick.instrument.tradingsymbol
        if symbol not in self.selected_pending or symbol in self.entry_symbols:
            return
        row = self.decisions[symbol]
        if self.entries_disabled:
            row.accepted = False
            row.rejection_reason = "ENTRIES_DISABLED"
            self.selected_pending.discard(symbol)
            self._persist_decision("ENTRY_REJECTED", observed)
            return
        if len(self.entry_symbols) >= self.config.max_trades:
            row.accepted = False
            row.rejection_reason = "MAXIMUM_DAILY_TRADES"
            self.selected_pending.discard(symbol)
            self._persist_decision("ENTRY_REJECTED", observed)
            return
        bid = tick.best_bid
        active_notional = sum(
            abs(position.quantity) * (position.last_price or position.average_price)
            for position in self.engine.portfolio.open_positions
        )
        pending_notional = sum(
            float(order.metadata.get("estimated_notional", 0))
            for order in self.engine.order_manager.broker.active_orders()
            if order.metadata.get("role") == "ENTRY"
        )
        apply_entry_geometry(
            row, self.config,
            raw_entry_reference=bid,
            available_notional=max(0.0, self.config.max_gross_exposure - active_notional - pending_notional),
        )
        if not row.eligible or not row.quantity:
            row.accepted = False
            self.selected_pending.discard(symbol)
            self._persist_decision("ENTRY_GEOMETRY_REJECTED", observed)
            return
        request = OrderRequest(
            self.instruments[symbol], Side.SELL, OrderType.MARKET, row.quantity, observed,
            reference_price=bid,
            strategy_id=self.config.raw["strategy"]["id"],
            strategy_version=self.config.raw["strategy"]["version"],
            metadata={
                "role": "ENTRY", "sector": row.sector, "side": "SHORT",
                "candidate_timestamp": row.candidate_timestamp,
                "signal_timestamp": row.signal_timestamp,
                "session_open": row.today_open, "stop": row.stop,
                "gap_pct": row.gap_pct, "retention": row.retention,
                "priority_score": row.priority_score, "rank": row.rank,
                "entry_reference": bid,
                "estimated_notional": row.quantity * bid,
            },
            intent=TradeIntent.INTRADAY,
        )
        decision, order = self.engine.submit_order(request)
        row.rms_result = decision.reason
        row.order_status = order.status.value
        row.order_timestamp = observed.isoformat()
        self.selected_pending.discard(symbol)
        if decision.approved and order.status is OrderStatus.OPEN:
            self.entry_symbols.add(symbol)
        else:
            row.accepted = False
            row.rejection_reason = f"RMS_OR_OMS_REJECTED:{decision.reason}"
        self._persist_decision("ORDER_SUBMISSION_RECORDED", observed)

    def _process_new_fills(self, observed: datetime) -> None:
        events = self.engine.fill_events[self.fill_cursor:]
        self.fill_cursor = len(self.engine.fill_events)
        changed = False
        for event in events:
            fill = event.fill
            order = self.engine.order_manager.orders[fill.order_id]
            symbol = fill.instrument.tradingsymbol
            row = self.decisions.get(symbol)
            if row is not None:
                row.order_status = order.status.value
                row.fill_status = "FILLED"
                row.fill_timestamp = fill.timestamp.isoformat()
                changed = True
            if order.metadata.get("role") != "ENTRY":
                continue
            stop = float(order.metadata["stop"])
            if stop <= fill.price:
                logger.error("invalid protective stop after fill for %s; flattening", symbol)
                self._flatten_symbol(fill.instrument, observed, "INVALID_STOP_AFTER_FILL")
                continue
            request = OrderRequest(
                fill.instrument, Side.BUY, OrderType.STOP_MARKET, fill.quantity, observed,
                reference_price=fill.price, stop_price=stop,
                strategy_id=self.config.raw["strategy"]["id"],
                strategy_version=self.config.raw["strategy"]["version"],
                metadata={
                    "role": "STOP", "exit_reason": "STOP", "entry_order_id": fill.order_id,
                    "sector": order.metadata["sector"], "raw_reference": stop,
                },
                intent=TradeIntent.INTRADAY,
            )
            risk, stop_order = self.engine.submit_order(request)
            if not risk.approved or stop_order.status is not OrderStatus.OPEN:
                logger.error("protective stop rejected for %s; flattening", symbol)
                self._flatten_symbol(fill.instrument, observed, "STOP_CREATION_FAILED")
        if changed:
            self._persist_decision("FILL_RECORDED_AND_STOP_CHECKED", observed)

    def _recover_missing_stops(self, observed: datetime) -> None:
        active = list(self.engine.order_manager.broker.active_orders())
        stopped = {
            order.instrument.key for order in active if order.metadata.get("role") == "STOP"
        }
        for position in self.engine.portfolio.open_positions:
            if position.quantity >= 0 or position.instrument.key in stopped:
                continue
            entry = next((
                order for order in self.engine.order_manager.orders.values()
                if order.instrument.key == position.instrument.key
                and order.metadata.get("role") == "ENTRY"
                and order.status is OrderStatus.FILLED
            ), None)
            if entry is None or "stop" not in entry.metadata:
                self._flatten_symbol(position.instrument, observed, "RECOVERY_STOP_STATE_MISSING")
                continue
            quote = self.quotes.get(position.instrument.key)
            if quote is None:
                continue
            request = OrderRequest(
                position.instrument, Side.BUY, OrderType.STOP_MARKET, abs(position.quantity), observed,
                reference_price=quote.last_price, stop_price=float(entry.metadata["stop"]),
                strategy_id=self.config.raw["strategy"]["id"],
                strategy_version=self.config.raw["strategy"]["version"],
                metadata={"role": "STOP", "exit_reason": "STOP", "entry_order_id": entry.order_id,
                          "sector": entry.metadata["sector"], "recovered": True},
                intent=TradeIntent.INTRADAY,
            )
            risk, order = self.engine.submit_order(request)
            if not risk.approved or order.status is not OrderStatus.OPEN:
                self._flatten_symbol(position.instrument, observed, "RECOVERY_STOP_CREATION_FAILED")

    def _flatten_symbol(self, instrument: Instrument, observed: datetime, reason: str) -> None:
        position = self.engine.portfolio.positions.get(instrument.key)
        if position is None or position.quantity == 0:
            return
        quote = self.quotes.get(instrument.key)
        if quote is None or quote.last_price <= 0:
            return
        self.engine.submit_order(OrderRequest(
            instrument, Side.BUY if position.quantity < 0 else Side.SELL,
            OrderType.MARKET, abs(position.quantity), observed,
            reference_price=quote.last_price,
            strategy_id=self.config.raw["strategy"]["id"],
            strategy_version=self.config.raw["strategy"]["version"],
            metadata={"role": "EXIT", "exit_reason": reason, "raw_reference": quote.last_price},
            intent=TradeIntent.INTRADAY,
        ))

    def _square_off(self, observed: datetime) -> None:
        for order in tuple(self.engine.order_manager.broker.active_orders()):
            if order.strategy_id == self.config.raw["strategy"]["id"] and order.metadata.get("role") in {"STOP", "ENTRY"}:
                self.engine.cancel_order(order.order_id, observed, "cancelled for 15:00 EOD flat")
        active_exit_keys = {
            order.instrument.key for order in self.engine.order_manager.broker.active_orders()
            if order.metadata.get("role") == "EXIT"
        }
        for position in tuple(self.engine.portfolio.open_positions):
            if position.instrument.key in active_exit_keys:
                continue
            self._flatten_symbol(position.instrument, observed, "EOD_1500")

    def _persist_decision(self, state: str, observed: datetime) -> None:
        rows = [self.decisions[symbol].to_dict() for symbol in sorted(self.decisions)]
        _atomic_json(self.output / "decision_state.json", {
            "strategy_id": self.config.raw["strategy"]["id"],
            "config_sha256": self.config.sha256,
            "frozen_strategy_sha256": self.config.raw["frozen_research"]["selected_strategy_sha256"],
            "trading_date": self.trading_date,
            "updated_at": observed.isoformat(),
            "state": state,
            "decisions": rows,
        })
        _write_csv(self.output / "signal_log.csv", [row for row in rows if row["accepted"]], FUNNEL_FIELDS)
        _write_csv(self.output / "rejection_log.csv", [row for row in rows if not row["accepted"]], FUNNEL_FIELDS)

    @staticmethod
    def _raw_reference(order, fill, *, entry: bool) -> float:
        execution = order.metadata.get("live_execution", {})
        observed = execution.get("observed_bid" if entry else "observed_ask")
        fallback = execution.get("observed_last")
        return float(observed or fallback or order.metadata.get("raw_reference")
                     or order.metadata.get("entry_reference") or fill.price)

    def write_reports(self) -> dict[str, object]:
        fills_by_id = {fill.fill_id: fill for fill in self.engine.order_manager.fills}
        ledger: list[dict[str, object]] = []
        costs_model = ZerodhaNseCashIntradayCosts()
        for trade in self.engine.portfolio.closed_trades:
            if trade.strategy_id != self.config.raw["strategy"]["id"]:
                continue
            entry_fill = next((fills_by_id.get(fill_id) for fill_id in trade.entry_fill_ids), None)
            exit_fill = next((fills_by_id.get(fill_id) for fill_id in trade.exit_fill_ids), None)
            if entry_fill is None or exit_fill is None:
                continue
            entry_order = self.engine.order_manager.orders[entry_fill.order_id]
            exit_order = self.engine.order_manager.orders[exit_fill.order_id]
            raw_entry = self._raw_reference(entry_order, entry_fill, entry=True)
            raw_exit = self._raw_reference(exit_order, exit_fill, entry=False)
            gross = trade.quantity * (raw_entry - raw_exit)
            slippage = trade.quantity * ((raw_entry - entry_fill.price) + (exit_fill.price - raw_exit))
            costs = costs_model.round_trip(
                side="SHORT", quantity=trade.quantity,
                entry_price=entry_fill.price, exit_price=exit_fill.price,
            )
            row = self.decisions.get(trade.instrument.tradingsymbol)
            statutory = costs.statutory_broker_cost
            ledger.append({
                "date": trade.entry_timestamp.date(), "symbol": trade.instrument.tradingsymbol,
                "sector": entry_order.metadata.get("sector", ""), "side": "SHORT",
                "qty": trade.quantity,
                "candidate_timestamp": entry_order.metadata.get("candidate_timestamp", ""),
                "signal_timestamp": entry_order.metadata.get("signal_timestamp", ""),
                "order_timestamp": entry_order.created_at.isoformat(),
                "fill_timestamp": entry_fill.timestamp.isoformat(),
                "entry_timestamp": trade.entry_timestamp.isoformat(), "entry_price": entry_fill.price,
                "stop": entry_order.metadata.get("stop", row.stop if row else ""),
                "exit_timestamp": trade.exit_timestamp.isoformat(), "exit_price": exit_fill.price,
                "exit_reason": exit_order.metadata.get("exit_reason", "STOP"),
                "gross_pnl": round(gross, 2), "brokerage": costs.brokerage, "stt": costs.stt,
                "nse_transaction_charge": costs.nse_transaction_charge,
                "sebi_charge": costs.sebi_charge, "stamp_duty": costs.stamp_duty,
                "gst": costs.gst, "ipft": costs.ipft,
                "statutory_broker_cost": statutory, "slippage_cost": round(slippage, 2),
                "total_cost": round(statutory + slippage, 2),
                "net_pnl": round(gross - statutory - slippage, 2),
            })
        _write_csv(self.output / "trade_ledger.csv", ledger, TRADE_FIELDS)
        net_values = [float(row["net_pnl"]) for row in ledger]
        equity = float(self.config.raw["paper_execution"]["starting_capital"])
        peak = equity
        drawdown = 0.0
        for value in net_values:
            equity += value
            peak = max(peak, equity)
            drawdown = max(drawdown, peak - equity)
        active_managed = [
            order for order in self.engine.order_manager.broker.active_orders()
            if order.strategy_id == self.config.raw["strategy"]["id"]
        ]
        flat = not self.engine.portfolio.open_positions and not active_managed
        report = {
            "strategy_id": self.config.raw["strategy"]["id"],
            "status": "FORWARD PAPER OBSERVATION — EDGE NOT VALIDATED",
            "starting_capital": float(self.config.raw["paper_execution"]["starting_capital"]),
            "ending_equity": round(equity, 2), "trades": len(ledger),
            "wins": sum(value > 0 for value in net_values),
            "losses": sum(value <= 0 for value in net_values),
            "gross": round(sum(float(row["gross_pnl"]) for row in ledger), 2),
            "cost": round(sum(float(row["total_cost"]) for row in ledger), 2),
            "net": round(sum(net_values), 2), "drawdown": round(drawdown, 2),
            "open_positions": len(self.engine.portfolio.open_positions),
            "active_managed_orders": len(active_managed),
            "EOD_flat_pass": flat,
            "zero_trade_result": "ZERO TRADES — NO VALID SETUP" if not ledger else None,
        }
        _atomic_json(self.output / "daily_report.json", report)
        return report
