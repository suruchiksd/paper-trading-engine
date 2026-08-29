# Paper Trading Engine

Local Python infrastructure for paper trading and medium-frequency research, with
strict boundaries between market data, strategies, risk, orders, execution,
portfolio accounting, persistence, and reporting.

## Current status and architecture

The engine supports read-only historical Parquet replay and deterministic candle-
based paper execution. It has no trading strategy, live market-data connection, or
real broker implementation. The central rule remains: **a strategy never calls a
broker directly**.

```text
Historical data -> ReplayFeed -> TradingEngine -> Strategy/manual OrderRequest
                                      |                    |
                                      |              RiskManager
                                      |                    |
                                      |              OrderManager
                                      |                    |
                                      +-> PaperBroker -> Fill -> Portfolio
```

- `RiskManager` returns an explicit decision based on portfolio and pending-order
  state. It neither fills orders nor modifies market data.
- `OrderManager` owns lifecycle transitions and order/fill traceability.
- `PaperBroker` assigns deterministic candle-based execution prices. It owns neither
  risk decisions nor portfolio accounting.
- `Portfolio` changes only from fills and owns cash, positions, marks, P&L, costs,
  and closed trades.
- `TradingEngine` sequences these components; it contains no fill or P&L formulas.

## Exact candle processing order

For every completed historical candle `T`:

1. Orders created before `T` are evaluated against the candle.
2. Broker fills update order state and then portfolio accounting.
3. The portfolio is marked using candle `T`'s close.
4. Fill hooks and the completed candle are dispatched to strategies.
5. An optional manual control callback runs.
6. Orders produced after observing `T` are eligible only on a later timestamp.

This mandatory anti-lookahead rule prevents a new order from using the candle that
caused it.

## Deterministic fill model

- MARKET fills in full at the next eligible candle open, with adverse side-aware
  basis-point slippage.
- BUY LIMIT fills at open when open is at/below its limit, otherwise at the limit if
  low touches it. SELL LIMIT is symmetric. Applied slippage is capped so the limit
  constraint is never violated.
- SELL STOP_MARKET triggers when low reaches the stop; BUY triggers when high reaches
  it. A gap beyond the trigger fills from the candle open before adverse slippage,
  rather than unrealistically filling at the trigger.
- STOP_LIMIT and partial fills are deferred. Candle volume is not treated as precise
  queue availability.

OHLC does not reveal intrabar sequence, spread, depth, latency, or queue priority.
Basic OCO groups make stop/target ambiguity explicit: `CONSERVATIVE` (default) picks
the adverse touched price, `OPTIMISTIC` picks the favorable price for research
comparison, and `ERROR` refuses the ambiguous result. No intrabar path is invented.

The configurable cost boundary currently supports no costs or a simple basis-points
model. It deliberately does not encode Zerodha/NSE taxes and charges.

## Portfolio convention and risk

Positions use signed quantities and weighted-average cost. Every reduction creates a
closed trade for the reduced quantity; a reversal closes the previous direction and
opens a new campaign. This is not tax-lot accounting. Gross realized P&L, allocated
costs, net realized P&L, unrealized P&L, cash, and equity are kept separately.

Short-sale proceeds enter cash, but borrow availability, margin, financing, and
collateral rules are not modeled. Current risk checks enforce maximum positions,
position exposure, daily loss, positive quantity, reference price, and available
cash for opening long quantity. `risk.yaml` remains the source of starting capital.

## Historical data boundary

`config/settings.yaml` configures the external root, equity/index directories,
instrument CSV, quality policy, paper fill policy, slippage, and simple costs. The
external platform is read directly and read-only; its Python package is not imported.
Historical values are raw and unadjusted.

Duplicate timestamps, invalid OHLC, and same-day one-minute gaps each support
`ERROR`, `WARN`, or `ALLOW`. WARN/ALLOW never repair prices, fabricate bars, or
forward-fill. Gap detection does not implement an exchange calendar or prove session
completeness.

## Setup and commands

Python 3.11 or newer is required. Use `python3` when no activated environment exists.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e '.[dev]'
pytest
```

Data-only historical replay (not an execution backtest):

```bash
python3 scripts/run_replay.py --symbol RELIANCE --date 2026-07-31
```

Synthetic paper-execution demonstration (not a strategy or performance test):

```bash
python3 scripts/run_paper_replay_demo.py --symbol RELIANCE \
  --date 2026-07-31 --submit-after 10:00 --quantity 10
```

Historical paper execution is distinct from future live-paper execution, which can
observe live bid/ask and depth.

## Structure

```text
config/                    Application, historical-data, execution, and risk settings
scripts/                   Data replay and synthetic execution demonstrations
src/paper_trading/
  models.py                Shared domain records
  events.py                In-process event envelopes
  engine.py                Deterministic orchestration
  market_data/             Parquet/instrument adapters and replay feed
  execution/               Broker protocol, PaperBroker, OMS, costs, transitions
  portfolio/               Weighted-average in-memory accounting
  risk/                    Deterministic pre-trade risk checks
  strategies/              Broker-independent strategy lifecycle
tests/                     Unit and integration tests with small fixtures
```

## Deferred

Persistence, session recovery, detailed Indian-equity charges, margin/borrow rules,
partial fills, queue simulation, STOP_LIMIT execution, exchange calendars, actual
strategies, live-paper interfaces, Zerodha WebSocket/API access, and real execution
are intentionally absent.

The recommended next phase is durable event/session persistence and reporting with
restart/reconciliation tests. Live Zerodha work should remain a separate later phase.
