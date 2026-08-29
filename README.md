# Paper Trading Engine

A local Python foundation for paper trading and medium-frequency strategy research,
with strict separation between market data, strategy intent, risk, execution,
portfolio accounting, persistence, and reporting.

## Current status

This first phase provides typed domain models, event envelopes, YAML configuration,
a broker-independent strategy lifecycle, and a non-operational engine composition
root. It does **not** connect to Zerodha, stream data, place orders, or implement a
trading strategy.

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
  market_data/                  Future feeds and replay adapters
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

## Roadmap

The recommended next phase is to define narrow interfaces for a market-data feed,
risk manager, order manager, broker, portfolio service, and event persistence; then
implement deterministic historical replay plus a simulated paper broker. Live
Zerodha data and real execution should remain separate later phases.

This repository intentionally has no dependency on neighboring research or market
data repositories. Optional file-based adapters can be added without coupling their
packages or duplicating their historical datasets.
