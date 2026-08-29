#!/usr/bin/env python3
"""Run read-only Zerodha live market data and print completed one-minute candles."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from paper_trading.config import load_config  # noqa: E402
from paper_trading.engine import TradingEngine  # noqa: E402
from paper_trading.events import CandleEvent, TickEvent  # noqa: E402
from paper_trading.market_data import (  # noqa: E402
    CsvInstrumentRepository,
    KiteCredentials,
    ZerodhaLiveFeed,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", action="append", help="repeat for multiple instruments")
    parser.add_argument("--segment", help="optional NSE or INDICES segment filter")
    parser.add_argument("--ticks", action="store_true", help="print normalized ticks")
    parser.add_argument("--config-dir", type=Path, default=PROJECT_ROOT / "config")
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config(args.config_dir)
    credentials = KiteCredentials.from_environment(args.env_file)
    symbols = args.symbol or list(config.live_market_data.symbols)
    repository = CsvInstrumentRepository(config.historical_data.instrument_master)
    instruments = [repository.resolve(symbol, segment=args.segment) for symbol in symbols]
    live = config.live_market_data
    feed = ZerodhaLiveFeed(
        credentials.api_key,
        credentials.access_token or "",
        instruments,
        mode=live.mode,
        stale_after_seconds=live.stale_after_seconds,
        reconnect_max_tries=live.reconnect_max_tries,
        reconnect_max_delay=live.reconnect_max_delay,
    )
    engine = TradingEngine()  # deliberately has no execution components
    print("Connecting read-only live market data; no orders can be generated.")
    print(f"Requested: {', '.join(item.tradingsymbol for item in instruments)}")
    try:
        feed.start()
        for event in feed.events():
            engine.dispatch_market_event(event)
            if args.ticks and isinstance(event, TickEvent):
                tick = event.tick
                print(
                    f"TICK {tick.exchange_timestamp} {tick.instrument.tradingsymbol} "
                    f"LTP={tick.last_price} BID={tick.best_bid} ASK={tick.best_ask}"
                )
            elif isinstance(event, CandleEvent):
                candle = event.candle
                print(
                    f"CANDLE COMPLETE {candle.timestamp.isoformat()} "
                    f"{candle.instrument.tradingsymbol} O={candle.open} H={candle.high} "
                    f"L={candle.low} C={candle.close} V={candle.volume}"
                )
                health = feed.health_snapshot()
                print(
                    f"Health: state={health.state} ticks={health.ticks_received} "
                    f"last_tick={health.last_tick_received_at} reconnects={health.reconnect_count}"
                )
    except KeyboardInterrupt:
        print("Stopping live feed...")
    finally:
        feed.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
