#!/usr/bin/env python3
"""Run a read-only historical candle replay without execution."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from paper_trading.config import load_config  # noqa: E402
from paper_trading.engine import TradingEngine  # noqa: E402
from paper_trading.market_data import (  # noqa: E402
    CsvInstrumentRepository,
    HistoricalReplayFeed,
    ParquetHistoricalCandleRepository,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", action="append", required=True, help="repeat for multiple symbols")
    parser.add_argument("--date", required=True, type=date.fromisoformat, help="trading date (YYYY-MM-DD)")
    parser.add_argument("--segment", help="optional instrument segment, e.g. NSE or INDICES")
    parser.add_argument("--config-dir", type=Path, default=PROJECT_ROOT / "config")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config_dir)
    master = CsvInstrumentRepository(config.historical_data.instrument_master)
    instruments = [master.resolve(symbol, segment=args.segment) for symbol in args.symbol]
    historical = config.historical_data
    repository = ParquetHistoricalCandleRepository(
        historical.root,
        equities_subdir=historical.equities_subdir,
        indices_subdir=historical.indices_subdir,
        quality_policy=historical.quality,
    )
    feed = HistoricalReplayFeed(repository, instruments, args.date)
    count = TradingEngine().replay(feed)

    print("Historical replay (data dispatch only; no order/fill simulation)")
    print(f"Date: {args.date.isoformat()}")
    print(f"Symbols: {', '.join(instrument.tradingsymbol for instrument in instruments)}")
    print(f"Candles loaded: {count}")
    if feed.session_start is not None:
        print(f"First: {feed.session_start.isoformat()}")
        print(f"Last: {feed.session_end.isoformat()}")
    print(f"Replay events: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
