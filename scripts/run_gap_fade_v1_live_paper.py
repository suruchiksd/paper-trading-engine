#!/usr/bin/env python3
"""Run frozen Phase-7B Gap Fade V1 with Kite data and local paper fills only."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import sys
from datetime import datetime, time
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kiteconnect import KiteConnect  # noqa: E402
from kiteconnect.exceptions import TokenException  # noqa: E402
from paper_trading.adapters.gap_fade_v1 import (  # noqa: E402
    load_gap_fade_config,
    verify_frozen_research_identity,
)
from paper_trading.adapters.gap_fade_v1.live import (  # noqa: E402
    GapFadePaperRuntime,
    fetch_previous_valid_closes,
)
from paper_trading.engine import TradingEngine  # noqa: E402
from paper_trading.execution import (  # noqa: E402
    LiveExecutionConfig,
    LivePaperBroker,
    OrderManager,
    ZerodhaNseCashIntradayCosts,
)
from paper_trading.market_data import (  # noqa: E402
    CsvInstrumentRepository,
    KiteCredentials,
    LiveQuoteStore,
    ZerodhaLiveFeed,
)
from paper_trading.manual import LiveOrderGate  # noqa: E402
from paper_trading.portfolio import Portfolio  # noqa: E402
from paper_trading.risk import RiskLimits, RiskManager  # noqa: E402
from paper_trading.storage import SQLiteDatabase, SQLiteStore, SessionService  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
MODE = "LIVE_PAPER_GAP_FADE_V1"


def acquire_session_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+")
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        stream.close()
        raise
    return stream


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=["paper"])
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--resume", metavar="SESSION_ID")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config" / "gap_fade_v1.yaml")
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    parser.add_argument(
        "--instrument-master", type=Path,
        default=Path("/home/suruchi-pandey/market-data-platform-gpt/data/reference/instruments_nse.csv"),
    )
    parser.add_argument("--database", type=Path,
                        default=PROJECT_ROOT / "data" / "state" / "gap_fade_v1.sqlite")
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def resolve_instruments(config, master: Path):
    repository = CsvInstrumentRepository(master)
    resolved = {}
    unresolved = {}
    tokens: set[int] = set()
    for symbol in config.symbols:
        try:
            instrument = repository.resolve(symbol, segment="NSE")
            if instrument.instrument_token is None:
                raise ValueError("null instrument token")
            if instrument.instrument_token in tokens:
                raise ValueError(f"duplicate instrument token {instrument.instrument_token}")
            tokens.add(instrument.instrument_token)
            resolved[symbol] = instrument
        except Exception as exc:
            unresolved[symbol] = f"INSTRUMENT_RESOLUTION_FAILED:{type(exc).__name__}:{exc}"
    return resolved, unresolved


def _kite_or_refresh(credentials: KiteCredentials):
    kite = KiteConnect(api_key=credentials.api_key)
    kite.set_access_token(credentials.access_token or "")
    try:
        profile = kite.profile()
    except TokenException as exc:
        print("Kite access token is invalid/expired. Refresh exactly with:")
        print("  .venv/bin/python scripts/zerodha_login.py")
        print("  .venv/bin/python scripts/zerodha_login.py --request-token TOKEN_FROM_REDIRECT")
        raise SystemExit(2) from exc
    return kite, profile


def _configure_runtime_log(path: Path) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.FileHandler(path), logging.StreamHandler(sys.stdout)],
        force=True,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print("\n" + "=" * 72)
    print("PAPER MODE")
    print("GAP FADE V1")
    print("EDGE NOT VALIDATED")
    print("FORWARD OBSERVATION ONLY")
    print("LOCAL PAPER FILLS ONLY — NO REAL BROKER ORDER PATH")
    print("=" * 72 + "\n")

    config = load_gap_fade_config(args.config)
    source_identity = verify_frozen_research_identity(config)
    instruments, unresolved = resolve_instruments(config, args.instrument_master)
    print(f"Frozen strategy SHA-256: {source_identity['selected_strategy_sha256']}")
    print(f"Operational config SHA-256: {config.sha256}")
    print(f"Eligible universe count: {len(config.symbols)}")
    print(f"Resolved count: {len(instruments)}")
    print(f"Unresolved count: {len(unresolved)}")
    if unresolved:
        print("Unresolved symbols (each fails closed): " + ", ".join(sorted(unresolved)))
    print("Eligible universe: " + ", ".join(config.symbols))

    credentials = KiteCredentials.from_environment(args.env_file)
    if args.preflight:
        kite, profile = _kite_or_refresh(credentials)
        feed_probe = ZerodhaLiveFeed(
            credentials.api_key, credentials.access_token or "", list(instruments.values())
        )
        sample = next(iter(instruments.values()), None)
        if sample is None:
            raise SystemExit("preflight failed: no resolved instruments")
        # One harmless historical read proves authenticated market-data REST access.
        rows = kite.historical_data(
            sample.instrument_token, datetime.now(IST).date(), datetime.now(IST).date(), "day",
            continuous=False, oi=False,
        )
        print(f"Kite read-only profile OK: {profile.get('user_id', 'unknown')}")
        print(f"Historical market-data access OK (response rows: {len(rows)})")
        print(f"Websocket/feed construction OK ({len(feed_probe.instruments)} instruments)")
        print("NO ORDERS SUBMITTED")
        return 0

    now = datetime.now(IST)
    output = args.output or PROJECT_ROOT / "data" / "sessions" / f"gap_fade_v1_{now:%Y%m%d}"
    output.mkdir(parents=True, exist_ok=True)
    _configure_runtime_log(output / "runtime.log")
    try:
        lock_stream = acquire_session_lock(output / "session.lock")
    except BlockingIOError as exc:
        raise SystemExit(f"duplicate process rejected: {output / 'session.lock'}") from exc
    if not args.resume and (output / "decision_state.json").exists():
        raise SystemExit("existing session state requires --resume SESSION_ID")

    # The session lock is held before any authenticated market-data access.
    kite, profile = _kite_or_refresh(credentials)
    logging.info("Kite read-only profile authenticated: %s", profile.get("user_id", "unknown"))
    previous_closes, close_failures = fetch_previous_valid_closes(
        kite, instruments, now.date()
    )
    unresolved.update(close_failures)
    print(f"Previous valid closes resolved: {len(previous_closes)}")
    print(f"Previous valid closes unavailable: {len(close_failures)}")

    database = SQLiteDatabase(args.database)
    store = SQLiteStore(database)
    sessions = SessionService(store)
    quotes = LiveQuoteStore()
    feed = ZerodhaLiveFeed(
        credentials.api_key, credentials.access_token or "", list(instruments.values()),
        mode="full", stale_after_seconds=10, reconnect_max_tries=10,
        reconnect_max_delay=30, candle_finalize_delay_seconds=2,
    )
    broker = LivePaperBroker(
        quotes, feed.health_snapshot, lambda: datetime.now(IST),
        config=LiveExecutionConfig(
            max_quote_age_seconds=float(config.raw["paper_execution"]["quote_max_age_seconds"]),
            allow_ltp_fallback=False,
            market_slippage_bps=config.slippage_bps,
            stop_market_slippage_bps=config.slippage_bps,
        ),
        cost_model=ZerodhaNseCashIntradayCosts(),
    )
    if args.resume:
        recovered = sessions.resume(args.resume, broker)
        if recovered.session.mode != MODE:
            raise ValueError("resume session belongs to a different strategy/mode")
        session = recovered.session
        portfolio = recovered.portfolio
        manager = recovered.order_manager
        manager.order_id_factory = lambda: f"PAPER-GF1-{uuid4().hex}"
    else:
        session = sessions.create(
            now.date(), MODE, float(config.raw["paper_execution"]["starting_capital"]),
            strategy_context={
                "strategy_id": config.raw["strategy"]["id"],
                "config_sha256": config.sha256,
                "frozen_strategy_sha256": source_identity["selected_strategy_sha256"],
                "paper_only": True,
                "edge_validated": False,
            },
        )
        session = sessions.start(session.session_id)
        portfolio = Portfolio(float(config.raw["paper_execution"]["starting_capital"]))
        manager = OrderManager(broker, order_id_factory=lambda: f"PAPER-GF1-{uuid4().hex}")
    recorder = sessions.recorder(session.session_id)
    engine = TradingEngine(
        risk_manager=RiskManager(RiskLimits(
            maximum_positions=config.max_positions,
            maximum_daily_loss_pct=config.daily_hard_loss
            / float(config.raw["paper_execution"]["starting_capital"]) * 100,
            maximum_position_exposure_pct=config.max_position_notional
            / float(config.raw["paper_execution"]["starting_capital"]) * 100,
        )),
        order_manager=manager,
        portfolio=portfolio,
        persistence=recorder,
    )
    engine.pre_trade_check = LiveOrderGate(
        list(instruments.values()), quotes, feed.health_snapshot,
        lambda: datetime.now(IST),
        float(config.raw["paper_execution"]["quote_max_age_seconds"]),
    ).check
    decision_path = output / "decision_state.json"
    persisted_decision = False
    if decision_path.exists():
        persisted_state = json.loads(decision_path.read_text()).get("state", "")
        persisted_decision = persisted_state != "AWAITING_COMPLETED_0929_BARS"
    resumed_decision = bool(args.resume) and (
        persisted_decision
        or any(order.strategy_id == config.raw["strategy"]["id"] for order in manager.orders.values())
    )
    runtime = GapFadePaperRuntime(
        config, engine, quotes, instruments, previous_closes, output,
        trading_date=now.date(), clock=lambda: datetime.now(IST), unresolved=unresolved,
        resumed_decision=resumed_decision,
    )
    restored = runtime.restore_bars()
    recorder.record_audit("GAP_FADE_V1_START", datetime.now(IST), {
        "config_sha256": config.sha256,
        "frozen_strategy_sha256": source_identity["selected_strategy_sha256"],
        "paper_only": True, "edge_validated": False,
        "resolved_symbols": len(instruments), "unresolved_symbols": len(unresolved),
        "restored_bars": restored,
    })
    logging.info("session_id=%s output=%s", session.session_id, output)
    failed = False
    flat = False
    try:
        feed.start()
        for event in feed.events():
            runtime.handle(event)
            payload = getattr(event, "tick", None) or getattr(event, "candle", None)
            observed = getattr(payload, "exchange_timestamp", None) or getattr(payload, "timestamp", None)
            managed = [order for order in manager.broker.active_orders()
                       if order.strategy_id == config.raw["strategy"]["id"]]
            if (observed is not None
                    and observed.astimezone(IST).time().replace(tzinfo=None) >= time(15, 0)
                    and not portfolio.open_positions and not managed):
                break
    except KeyboardInterrupt:
        failed = True
        runtime.entries_disabled = True
        logging.error("operator interrupt: session cannot be declared complete")
    except Exception:
        failed = True
        runtime.entries_disabled = True
        logging.exception("market-data/runtime failure: new entries disabled")
    finally:
        feed.stop()
        for event in feed.finalize_candles():
            runtime.handle(event)
        report = runtime.write_reports()
        flat = bool(report["EOD_flat_pass"])
        recorder.record_audit("EOD_FLAT_ASSERTION", datetime.now(IST), {
            "passed": flat,
            "open_positions": report["open_positions"],
            "active_managed_orders": report["active_managed_orders"],
        })
        if failed or not flat:
            sessions.fail(session.session_id)
        else:
            sessions.complete(session.session_id, portfolio)
        fcntl.flock(lock_stream, fcntl.LOCK_UN)
        lock_stream.close()
    if not flat:
        raise SystemExit("EOD flat assertion FAILED; session marked failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
