# Paper Trading Engine

A local Python foundation for paper trading and medium-frequency strategy research,
with strict separation between market data, strategy intent, risk, execution,
portfolio accounting, persistence, and reporting.

## Current status

The engine now provides typed domain models, event envelopes, YAML configuration,
a broker-independent strategy lifecycle, and deterministic historical candle replay
from canonical yearly Parquet partitions. It does **not** connect to Zerodha, place
orders, simulate fills, or implement a trading strategy.

The central rule is: **a strategy never calls a broker directly**. Strategies emit
`Signal` objects. The planned flow is:

```text
MarketData -> Tick/Candle -> Strategy -> Signal -> RiskManager -> Order
           -> PaperBroker or ZerodhaBroker -> Fill -> Portfolio
```

This keeps the same strategy usable in historical replay, paper execution, and—only
after deliberate future work—real execution.

## Planned operating modes

1. Historical backtest and replay
2. Live manual paper trading with Zerodha market data
3. Live algorithmic paper trading
4. Multiple independent MFT strategies
5. Combined researched MFT strategies
6. Optional real Zerodha execution behind the same broker boundary

## Structure

```text
config/                         Example application and risk configuration
data/state/                     Runtime database/state (ignored)
data/sessions/                  Runtime session artifacts (ignored)
logs/                           Runtime logs (ignored)
scripts/                        Future operational entry points
src/paper_trading/
  config.py                     Typed YAML configuration loader
  models.py                     Core domain enums and models
  events.py                     Simple event envelopes
  engine.py                     Future orchestration boundary
  strategies/base.py            Broker-independent strategy lifecycle
  market_data/                  Feed protocols, instrument lookup, Parquet reader, replay
  execution/                    Future OMS and broker adapters
  portfolio/                    Future positions and P&L services
  risk/                         Future risk policies
  storage/                      Future persistence adapters
tests/                          Focused model and configuration tests
```

## Setup

Python 3.11 or newer is required.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
pytest
```

The `.env` values are placeholders for later integrations. Never commit credentials.

## Historical data and replay

`config/settings.yaml` declares the external historical root, equity/index
subdirectories, instrument-master CSV, and data-quality policies. Paths are settings,
not package dependencies: this project reads canonical files directly and never
imports or modifies the external market-data platform.

Run a one-day replay from any working directory:

```bash
python3 /home/suruchi-pandey/Projects/paper-trading-engine/scripts/run_replay.py \
  --symbol RELIANCE --date 2026-07-31
```

Repeat `--symbol` to merge symbols deterministically. Use `--segment INDICES` when
an index symbol needs disambiguation. Replay runs as fast as possible and dispatches
completed candles to registered strategies; it performs **no order or fill
simulation**. Historical replay is therefore not yet an execution backtest.

The default quality policy is `ERROR` for duplicate timestamps, invalid OHLC, and
intra-day one-minute gaps. Each can be configured as `error`, `warn`, or `allow`.
WARN and ALLOW retain original rows; no prices are repaired and no bars are generated
or forward-filled. Gap checks only identify gaps between returned timestamps on the
same date. They do not implement an NSE calendar, detect missing session edges, or
prove exchange-session completeness. Vendor data is raw and not corporate-action
adjusted.

## Roadmap

The recommended next phase is to define risk, order-manager, broker, and portfolio
interfaces, then implement a deterministic simulated paper broker and accounting
tests on top of replay. Live Zerodha data and real execution remain separate phases.

This repository intentionally has no dependency on neighboring research or market
data repositories. Optional file-based adapters can be added without coupling their
packages or duplicating their historical datasets.
