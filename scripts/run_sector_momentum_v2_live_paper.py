#!/usr/bin/env python3
"""Run one frozen Sector Momentum V2 rebalance using read-only Kite market data.

Kite is used only for ticks. Orders are local PaperBroker objects; this module has
no real-broker order integration. The live open contract is the open of the first
completed, exchange-timestamped 09:15 IST one-minute candle observed after the
feed was connected before the cash-market open.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import os
import queue
import shutil
import sys
import threading
from dataclasses import asdict
from datetime import datetime, time, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from paper_trading.adapters.sector_momentum_v2 import (  # noqa: E402
    Quote, QuoteResolver, SectorMomentumV2Orchestrator, V2SessionStore,
    canonical_key, instrument_for, load_signals, validate_target_state,
)
from paper_trading.config import load_config  # noqa: E402
from paper_trading.events import CandleEvent  # noqa: E402
from paper_trading.execution import OrderManager, PaperBroker, SlippageConfig  # noqa: E402
from paper_trading.execution.costs import BasisPointsTransactionCosts  # noqa: E402
from paper_trading.market_data import (  # noqa: E402
    CsvInstrumentRepository, InstrumentNotFoundError, KiteCredentials, ZerodhaLiveFeed,
)
from paper_trading.models import Candle, OrderRequest, OrderType, Side, TradeIntent  # noqa: E402
from paper_trading.portfolio import Portfolio  # noqa: E402
from paper_trading.risk import RiskLimits, RiskManager  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
PREREG = "2e99d105d6a23538dafcaa4d6c17ba71b008c2e30fee63bec2cb50706a6aee2f"
BUILD = "96e614a5638f221b1c340e4f9201e792b5c01e1394eac682ef7b53480aa0f2fb"
EXPECTED_MODE = "paper"
OPEN_MINUTE = time(9, 15)
LATEST_SAFE_START = time(9, 14, 30)
OPEN_CAPTURE_DEADLINE = time(9, 16, 20)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def json_text(value) -> str:
    return json.dumps(value, sort_keys=True, indent=2, default=str) + "\n"


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=[EXPECTED_MODE])
    parser.add_argument("--signal", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--session-date", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-state-sha256", required=True)
    parser.add_argument("--config-dir", type=Path, default=PROJECT_ROOT / "config")
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    parser.add_argument("--preflight", action="store_true")
    return parser.parse_args()


class SessionOpenQuoteResolver(QuoteResolver):
    """QuoteResolver whose rows retain the audited live source label/timestamp."""

    def __init__(self, rows):
        super().__init__({})
        self.rows = dict(rows)

    def _row(self, day, symbol):
        strategy_symbol = str(symbol).split(":")[-1].upper()
        row = self.rows.get((day, strategy_symbol))
        return strategy_symbol, row, (row.get("source") if row else "none")


class SessionTimestampOrchestrator(SectorMomentumV2Orchestrator):
    """V2-specific clock adapter; sizing and strategy behavior remain inherited."""

    def __init__(self, *args, event_timestamp: datetime, **kwargs):
        super().__init__(*args, **kwargs)
        self.event_timestamp = event_timestamp

    def _request(self, symbol, side, qty, price, day, reason):
        return OrderRequest(
            instrument_for(symbol), side, OrderType.MARKET, int(qty), self.event_timestamp,
            eligible_after=self.event_timestamp, reference_price=float(price),
            strategy_id="SECTOR_MOMENTUM", strategy_version="V2",
            metadata={"reason": reason}, intent=TradeIntent.SWING,
        )

    def _execute(self, req, open_price, day, decisions, orders, fills):
        decision = self.risk.evaluate(req, self.portfolio)
        decisions.append({
            "symbol": req.instrument.tradingsymbol, "side": req.side.value,
            "requested_quantity": req.quantity, "approved": decision.approved,
            "approved_quantity": decision.approved_quantity, "reason": decision.reason,
        })
        if not decision.approved:
            return
        if self.dry_run:
            orders.append(req)
            return
        order = self.order_manager.submit(req)
        fill_time = self.event_timestamp + timedelta(microseconds=1)
        generated = self.order_manager.process_candle(
            Candle(req.instrument, "1minute", fill_time, open_price, open_price, open_price, open_price)
        )
        for fill in generated:
            self.portfolio.process_fill(fill)
            fills.append(fill)
        orders.append(order)


def load_and_validate(signal_path: Path, session_date: str):
    signals = load_signals(signal_path)
    validate_target_state(
        signals, preregistration_hash=PREREG, build_reference=BUILD,
        execution_date=session_date,
    )
    dates = {item.row["signal_date"] for item in signals}
    if dates != {"2026-09-25"}:
        raise ValueError(f"unexpected signal date: {sorted(dates)}")
    if len(signals) != 13 or sum(item.active for item in signals) != 9:
        raise ValueError("Sep25 target must contain 13 rows and nine active targets")
    return signals


def restore_state(state_path: Path):
    portfolio = Portfolio(500_000)
    store = V2SessionStore(state_path)
    state = store.restore_cash_and_positions(portfolio)
    if state.get("session_date") != "2026-09-21" or len(portfolio.open_positions) != 9:
        raise ValueError("unexpected prospective starting state")
    if state.get("strategy_hash") != PREREG:
        raise ValueError("prospective state strategy hash mismatch")
    return portfolio, state


def position_rows(portfolio: Portfolio) -> list[dict]:
    return [{
        "symbol": position.instrument.key, "quantity": position.quantity,
        "average_price": position.average_price, "last_price": position.last_price,
        "market_value": position.quantity * (position.last_price or position.average_price),
    } for position in sorted(portfolio.open_positions, key=lambda item: item.instrument.key)]


def subscription_plan(signals, portfolio, repository):
    active = {item.symbol for item in signals if item.active}
    held = {key.split(":")[-1] for key, position in portfolio.positions.items() if position.quantity > 0}
    strategy_symbols = sorted(active | held)
    instruments, aliases, rows = [], {}, []
    for symbol in strategy_symbols:
        series = "EQ"
        live_symbol = symbol
        try:
            instrument = repository.resolve(symbol, segment="NSE")
        except InstrumentNotFoundError:
            if symbol not in held or symbol in active:
                raise
            live_symbol = f"{symbol}-BE"
            instrument = repository.resolve(live_symbol, segment="NSE")
            series = "BE"
        if symbol in active and series != "EQ":
            raise ValueError(f"active/new target cannot subscribe via non-EQ instrument: {symbol}")
        instruments.append(instrument)
        aliases[instrument.key] = (symbol, series)
        rows.append({
            "strategy_symbol": symbol, "live_symbol": live_symbol, "series": series,
            "instrument_key": instrument.key, "instrument_token": instrument.instrument_token,
            "role": "ACTIVE_TARGET" if symbol in active else "EXIT_ONLY_HELD",
        })
    return instruments, aliases, rows


def synthetic_quotes(signals, portfolio, session_date):
    held = {key.split(":")[-1]: position for key, position in portfolio.positions.items() if position.quantity > 0}
    rows = {}
    for item in signals:
        symbol = item.symbol
        if symbol in rows:
            continue
        if symbol in held:
            price = float(held[symbol].last_price or held[symbol].average_price)
        else:
            price = 100.0 + item.target_slot
        series = "BE" if symbol == "TBZ" and not item.active else "EQ"
        rows[(session_date, symbol)] = {
            "open": price, "close": None, "series": series,
            "source": "synthetic_preflight", "quote_timestamp": f"{session_date}T09:15:00+05:30",
            "live_symbol": f"{symbol}-BE" if series == "BE" else symbol,
        }
    return rows


def collect_live_open_rows(feed, aliases, session_date: str):
    expected_date = datetime.fromisoformat(session_date).date()
    work: queue.Queue[tuple[str, object]] = queue.Queue()
    rows = {}

    def consume():
        try:
            for event in feed.events():
                work.put(("event", event))
        except Exception as exc:
            work.put(("error", exc))

    feed.start()
    thread = threading.Thread(target=consume, daemon=True, name="v2-open-capture")
    thread.start()
    try:
        while datetime.now(IST).time() <= OPEN_CAPTURE_DEADLINE:
            try:
                kind, payload = work.get(timeout=0.25)
            except queue.Empty:
                continue
            if kind == "error":
                raise RuntimeError(f"live feed failed: {payload}")
            if not isinstance(payload, CandleEvent):
                continue
            candle = payload.candle
            if candle.timestamp.astimezone(IST).date() != expected_date:
                continue
            if candle.timestamp.astimezone(IST).time().replace(tzinfo=None) != OPEN_MINUTE:
                continue
            alias = aliases.get(candle.instrument.key)
            if alias is None:
                continue
            symbol, series = alias
            rows[(session_date, symbol)] = {
                "open": float(candle.open), "close": None, "series": series,
                "source": "zerodha_first_complete_1minute_candle",
                "quote_timestamp": candle.timestamp.astimezone(IST).isoformat(),
                "live_symbol": candle.instrument.tradingsymbol,
            }
            if len(rows) == len(aliases):
                break
    finally:
        feed.stop()
        thread.join(timeout=5)
    return rows, feed.health_snapshot()


def build_runtime(portfolio, quote_rows, event_timestamp):
    resolver = SessionOpenQuoteResolver(quote_rows)
    broker = PaperBroker(
        slippage=SlippageConfig(market_bps=5),
        cost_model=BasisPointsTransactionCosts(20),
    )
    manager = OrderManager(broker, order_id_factory=lambda: f"PAPER-V2-{uuid4().hex}")
    orchestrator = SessionTimestampOrchestrator(
        portfolio, RiskManager(RiskLimits(9, 100, 100)), manager, resolver,
        slippage_bps=5, transaction_cost_bps=20, dry_run=False,
        event_timestamp=event_timestamp,
    )
    return orchestrator, resolver


def normalized_outputs(result, before_rows, ending_portfolio, quote_rows, session_id):
    quote_by_symbol = {}
    for quote in result["quote_audit"]:
        quote_by_symbol.setdefault(quote.symbol, quote)
    order_rows, fill_rows = [], []
    order_by_id = {}
    for order in result["orders"]:
        order_by_id[order.order_id] = order
        q = quote_by_symbol.get(order.instrument.tradingsymbol)
        order_rows.append({
            "session_id": session_id, "order_id": order.order_id,
            "timestamp": order.created_at.isoformat(), "symbol": order.instrument.key,
            "side": order.side.value, "quantity": order.quantity, "order_type": order.order_type.value,
            "status": order.status.value, "reason": order.metadata.get("reason", ""),
            "reference_price": q.open if q else "", "paper_only": True,
        })
    for fill in result["fills"]:
        order = order_by_id[fill.order_id]
        q = quote_by_symbol.get(fill.instrument.tradingsymbol)
        reference = q.open if q else None
        fill_rows.append({
            "session_id": session_id, "fill_id": fill.fill_id, "order_id": fill.order_id,
            "timestamp": fill.timestamp.isoformat(), "symbol": fill.instrument.key,
            "side": fill.side.value, "quantity": fill.quantity, "reference_open": reference,
            "fill_price": fill.price, "slippage": "" if reference is None else fill.price-reference,
            "transaction_cost": fill.fees, "cash_impact": (
                -(fill.quantity*fill.price+fill.fees) if fill.side is Side.BUY
                else fill.quantity*fill.price-fill.fees
            ), "quote_source": q.source if q else "",
            "quote_timestamp": quote_rows.get((q.day, q.symbol), {}).get("quote_timestamp", "") if q else "",
        })
    risk_rows = [{"session_id": session_id, **row} for row in result["risk_decisions"]]
    quote_rows_out = []
    for q in result["quote_audit"]:
        metadata = quote_rows.get((q.day, q.symbol), {})
        quote_rows_out.append({**asdict(q), "quote_timestamp": metadata.get("quote_timestamp", ""), "live_symbol": metadata.get("live_symbol", q.symbol)})
    mark_rows = [asdict(item) for item in result["mark_audit"]]

    return order_rows, fill_rows, risk_rows, quote_rows_out, mark_rows


def write_operational_outputs(directory, *, signal_path, state_before, state_before_hash,
                              portfolio_before, portfolio_after, result, quote_rows,
                              session_id, session_date, pending_exits, health=None):
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copy2(signal_path, directory / "target_state.csv")
    (directory / "paper_state_before.json").write_text(json_text(state_before))
    before_positions = position_rows(portfolio_before)
    after_positions = position_rows(portfolio_after)
    position_fields = ["symbol", "quantity", "average_price", "last_price", "market_value"]
    write_csv(directory / "positions_before.csv", before_positions, position_fields)
    write_csv(directory / "positions_after.csv", after_positions, position_fields)

    order_rows, fill_rows, risk_rows, quote_audit, mark_audit = normalized_outputs(
        result, before_positions, portfolio_after, quote_rows, session_id
    )
    write_csv(directory / "orders.csv", order_rows, [
        "session_id", "order_id", "timestamp", "symbol", "side", "quantity", "order_type",
        "status", "reason", "reference_price", "paper_only",
    ])
    write_csv(directory / "fills.csv", fill_rows, [
        "session_id", "fill_id", "order_id", "timestamp", "symbol", "side", "quantity",
        "reference_open", "fill_price", "slippage", "transaction_cost", "cash_impact",
        "quote_source", "quote_timestamp",
    ])
    write_csv(directory / "risk_decisions.csv", risk_rows, [
        "session_id", "symbol", "side", "requested_quantity", "approved", "approved_quantity", "reason",
    ])
    write_csv(directory / "execution_quote_audit.csv", quote_audit, [
        "day", "symbol", "purpose", "source", "series", "open", "close", "status", "reason",
        "quote_timestamp", "live_symbol",
    ])
    write_csv(directory / "mark_audit.csv", mark_audit, [
        "day", "symbol", "purpose", "source", "series", "open", "close", "status", "reason",
    ])

    cash = float(state_before["cash"])
    quantities = {row["symbol"]: int(row["quantity"]) for row in before_positions}
    cash_rows, event_rows = [], []
    for sequence, fill in enumerate(fill_rows, start=1):
        before_cash = cash
        before_qty = quantities.get(fill["symbol"], 0)
        before_count = sum(value > 0 for value in quantities.values())
        cash += float(fill["cash_impact"])
        signed = int(fill["quantity"]) if fill["side"] == "BUY" else -int(fill["quantity"])
        quantities[fill["symbol"]] = before_qty + signed
        after_count = sum(value > 0 for value in quantities.values())
        cash_rows.append({
            "sequence": sequence, "fill_id": fill["fill_id"], "symbol": fill["symbol"],
            "cash_before": before_cash, "cash_impact": fill["cash_impact"], "cash_after": cash,
        })
        event_rows.append({
            "timestamp": fill["timestamp"], "event_type": "PAPER_FILL", "symbol": fill["symbol"],
            "side": fill["side"], "requested_quantity": fill["quantity"],
            "approved_quantity": fill["quantity"], "filled_quantity": fill["quantity"],
            "reference_price": fill["reference_open"], "fill_price": fill["fill_price"],
            "slippage": fill["slippage"], "transaction_cost": fill["transaction_cost"],
            "cash_before": before_cash, "cash_after": cash,
            "position_quantity_before": before_qty, "position_quantity_after": quantities[fill["symbol"]],
            "position_count_before": before_count, "position_count_after": after_count,
            "risk_decision": "APPROVED", "reason": "LOCAL_PAPER_FILL",
            "quote_source": fill["quote_source"], "quote_timestamp": fill["quote_timestamp"],
        })
    write_csv(directory / "cash_ledger.csv", cash_rows, [
        "sequence", "fill_id", "symbol", "cash_before", "cash_impact", "cash_after",
    ])
    event_fields = [
        "timestamp", "event_type", "symbol", "side", "requested_quantity", "approved_quantity",
        "filled_quantity", "reference_price", "fill_price", "slippage", "transaction_cost",
        "cash_before", "cash_after", "position_quantity_before", "position_quantity_after",
        "position_count_before", "position_count_after", "risk_decision", "reason", "quote_source",
        "quote_timestamp",
    ]
    write_csv(directory / "event_log.csv", event_rows, event_fields)
    summary = {
        "session_id": session_id, "mode": "PAPER", "paper_only": True,
        "signal_date": "2026-09-25", "execution_date": session_date,
        "execution_price_contract": "FIRST_COMPLETE_EXCHANGE_TIMESTAMPED_09_15_IST_ONE_MINUTE_CANDLE_OPEN",
        "starting_cash": state_before["cash"], "ending_cash": portfolio_after.cash,
        "starting_equity": portfolio_before.equity, "ending_equity": portfolio_after.equity,
        "day_pnl": portfolio_after.equity-portfolio_before.equity,
        "starting_positions": len(portfolio_before.open_positions),
        "ending_positions": len(portfolio_after.open_positions),
        "orders": len(order_rows), "fills": len(fill_rows),
        "risk_approved": sum(bool(row["approved"]) for row in risk_rows),
        "risk_rejected": sum(not bool(row["approved"]) for row in risk_rows),
        "pending_exits": sorted(pending_exits), "maximum_positions": max(
            [len(portfolio_before.open_positions)] + [int(row["position_count_after"]) for row in event_rows]
        ),
        "negative_cash": portfolio_after.cash < -0.01, "live_broker_endpoint_used": False,
        "health": asdict(health) if health is not None else None,
    }
    (directory / "session_summary.json").write_text(json_text(summary))
    (directory / "health_status.json").write_text(json_text({
        "session_status": "OPEN_REBALANCE_COMPLETE", "latest_quote_timestamp": max(
            (row.get("quote_timestamp", "") for row in quote_rows.values()), default=""
        ), "quote_freshness": "CAPTURED_FIRST_MINUTE", "cash": portfolio_after.cash,
        "equity": portfolio_after.equity, "open_positions": len(portfolio_after.open_positions),
        "position_count": len(portfolio_after.open_positions), "unrealized_pnl": portfolio_after.unrealized_pnl,
        "day_pnl": portfolio_after.equity-portfolio_before.equity,
        "orders": len(order_rows), "fills": len(fill_rows),
        "risk_rejections": summary["risk_rejected"], "pending_exits": sorted(pending_exits), "errors": [],
    }))
    return summary


def portfolio_signature(portfolio):
    return {
        "cash": portfolio.cash,
        "positions": position_rows(portfolio),
    }


def run_preflight(args, signals, real_state_hash):
    preflight = args.output / "preflight"
    preflight.mkdir(parents=True, exist_ok=True)
    portfolio, state = restore_state(args.state)
    before = copy.deepcopy(portfolio)
    quote_rows = synthetic_quotes(signals, portfolio, args.session_date)
    event_timestamp = datetime.fromisoformat(args.session_date + "T09:16:05+05:30")
    orchestrator, resolver = build_runtime(portfolio, quote_rows, event_timestamp)
    result = orchestrator.rebalance(signals, execution_date=args.session_date, strategy_hash=PREREG)
    result["quote_audit"] = resolver.audit
    result["mark_audit"] = resolver.mark_audit
    session_id = "PAPER-V2-20260928-SYNTHETIC-PREFLIGHT"
    summary = write_operational_outputs(
        preflight, signal_path=args.signal, state_before=state, state_before_hash=real_state_hash,
        portfolio_before=before, portfolio_after=portfolio, result=result, quote_rows=quote_rows,
        session_id=session_id, session_date=args.session_date, pending_exits=result["pending_exits"],
    )
    isolated_state = preflight / "synthetic_state.json"
    V2SessionStore(isolated_state).save(
        portfolio, session_date=args.session_date, strategy_hash=PREREG,
        pending_exits=result["pending_exits"],
    )
    reloaded = Portfolio(500_000)
    loaded = V2SessionStore(isolated_state).restore_cash_and_positions(reloaded)
    expected_exits = {item.symbol for item in signals if not item.active}
    retained = {item.symbol for item in signals if item.row["selection_status"] == "KEEP"}
    entries = {item.symbol for item in signals if item.row["selection_status"] == "ENTER"}
    purposes = {}
    for quote in resolver.audit:
        purposes.setdefault(quote.purpose, set()).add(quote.symbol)
    checks = {
        "four_expected_exits_attempted": purposes.get("exit", set()) == expected_exits and len(expected_exits) == 4,
        "retained_2pp_checks_occurred": retained.issubset(purposes.get("retained_resize", set())),
        "new_entry_logic_occurred": entries.issubset(purposes.get("entry", set())),
        "risk_decisions_produced": len(result["risk_decisions"]) > 0,
        "maximum_positions_le_9": len(portfolio.open_positions) <= 9 and summary["maximum_positions"] <= 9,
        "cash_nonnegative": portfolio.cash >= -0.01,
        "persistence_reload_exact": portfolio_signature(portfolio) == portfolio_signature(reloaded),
        "session_identity_reloaded": loaded["session_date"] == args.session_date and loaded["strategy_hash"] == PREREG,
        "real_state_unchanged": sha256(args.state) == real_state_hash,
        "all_logs_written": all((preflight / name).exists() for name in (
            "orders.csv", "fills.csv", "positions_before.csv", "positions_after.csv",
            "risk_decisions.csv", "execution_quote_audit.csv", "mark_audit.csv",
            "cash_ledger.csv", "event_log.csv", "session_summary.json",
        )),
    }
    report = {
        "status": "PASS" if all(checks.values()) else "FAIL", "checks": checks,
        "synthetic_prices_only": True, "synthetic_quantities_are_not_monday_quantities": True,
        "real_state_sha256_before": real_state_hash, "real_state_sha256_after": sha256(args.state),
        "orders": len(result["orders"]), "fills": len(result["fills"]),
        "risk_decisions": len(result["risk_decisions"]),
    }
    (preflight / "preflight_report.json").write_text(json_text(report))
    if not all(checks.values()):
        raise RuntimeError(f"synthetic preflight failed: {checks}")
    return report


def atomic_persist_state(path, portfolio, session_date, pending_exits):
    staged = path.with_suffix(path.suffix + ".staged")
    V2SessionStore(staged).save(
        portfolio, session_date=session_date, strategy_hash=PREREG,
        pending_exits=pending_exits,
    )
    verified = Portfolio(500_000)
    V2SessionStore(staged).restore_cash_and_positions(verified)
    if portfolio_signature(portfolio) != portfolio_signature(verified):
        raise RuntimeError("staged state reload mismatch")
    os.replace(staged, path)


def run_live(args, signals, real_state_hash):
    now = datetime.now(IST)
    if now.date() != datetime.fromisoformat(args.session_date).date():
        raise RuntimeError("live paper command may run only on its authorized session date")
    if now.time() > LATEST_SAFE_START:
        raise RuntimeError("start was too late to certify capture of the 09:15 first-minute open")
    portfolio, state = restore_state(args.state)
    before = copy.deepcopy(portfolio)
    config = load_config(args.config_dir)
    credentials = KiteCredentials.from_environment(args.env_file)
    repository = CsvInstrumentRepository(config.historical_data.instrument_master)
    instruments, aliases, subscription_rows = subscription_plan(signals, portfolio, repository)
    write_csv(args.output / "subscription_plan.csv", subscription_rows, [
        "strategy_symbol", "live_symbol", "series", "instrument_key", "instrument_token", "role",
    ])
    feed_config = config.live_market_data
    feed = ZerodhaLiveFeed(
        credentials.api_key, credentials.access_token or "", instruments,
        mode="full", stale_after_seconds=feed_config.stale_after_seconds,
        reconnect_max_tries=feed_config.reconnect_max_tries,
        reconnect_max_delay=feed_config.reconnect_max_delay,
        candle_finalize_delay_seconds=feed_config.candle_finalize_delay_seconds,
    )
    quote_rows, health = collect_live_open_rows(feed, aliases, args.session_date)
    write_csv(args.output / "captured_open_quotes.csv", [
        {"day": day, "symbol": symbol, **row} for (day, symbol), row in sorted(quote_rows.items())
    ], ["day", "symbol", "live_symbol", "series", "open", "source", "quote_timestamp"])
    event_timestamp = datetime.now(IST)
    orchestrator, resolver = build_runtime(portfolio, quote_rows, event_timestamp)
    result = orchestrator.rebalance(signals, execution_date=args.session_date, strategy_hash=PREREG)
    result["quote_audit"] = resolver.audit
    result["mark_audit"] = resolver.mark_audit
    session_id = f"PAPER-V2-{args.session_date.replace('-', '')}-{uuid4().hex[:12]}"
    summary = write_operational_outputs(
        args.output, signal_path=args.signal, state_before=state, state_before_hash=real_state_hash,
        portfolio_before=before, portfolio_after=portfolio, result=result, quote_rows=quote_rows,
        session_id=session_id, session_date=args.session_date, pending_exits=result["pending_exits"], health=health,
    )
    staged_after = args.output / "paper_state_after.json"
    V2SessionStore(staged_after).save(
        portfolio, session_date=args.session_date, strategy_hash=PREREG,
        pending_exits=result["pending_exits"],
    )
    atomic_persist_state(args.state, portfolio, args.session_date, result["pending_exits"])
    integrity = {
        "state_sha256_before": real_state_hash, "state_sha256_after": sha256(args.state),
        "target_sha256": sha256(args.signal), "paper_only": True,
        "live_broker_endpoint_used": False, "session_id": session_id,
    }
    (args.output / "state_integrity.json").write_text(json_text(integrity))
    return summary


def main() -> int:
    args = parse_args()
    args.signal = args.signal.resolve(); args.state = args.state.resolve(); args.output = args.output.resolve()
    if args.mode != EXPECTED_MODE:
        raise RuntimeError("only explicit paper mode is supported")
    if args.session_date != "2026-09-28":
        raise RuntimeError("this frozen launch command is authorized only for 2026-09-28")
    if sha256(args.state) != args.expected_state_sha256:
        raise RuntimeError("prospective state hash differs from the certified starting state")
    signals = load_and_validate(args.signal, args.session_date)
    real_state_hash = sha256(args.state)
    if args.preflight:
        report = run_preflight(args, signals, real_state_hash)
        print(json_text(report), end="")
        return 0
    summary = run_live(args, signals, real_state_hash)
    print(json_text(summary), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
