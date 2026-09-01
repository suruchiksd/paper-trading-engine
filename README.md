# Paper Trading Engine

Local Python infrastructure for paper trading and medium-frequency research, with
strict boundaries between market data, strategies, risk, orders, execution,
portfolio accounting, persistence, and reporting.

## Current status and architecture

The engine supports read-only historical Parquet replay, deterministic candle-based
paper execution, durable SQLite sessions, read-only Zerodha WebSocket market data,
and manual live paper trading. It has no trading strategy or real broker
implementation. The central rules remain: **a strategy never calls a broker
directly**, and **no Zerodha order API is used**.

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

## Durable sessions and crash recovery

Persistence is configured under `persistence` in `config/settings.yaml`; the default
database is `data/state/paper_trading.sqlite`. Relative paths resolve from the project
directory containing `config/`. SQLite foreign keys, WAL journaling, explicit
transactions, and `synchronous=FULL` are enabled. These settings improve local
durability but are not a claim of immunity to filesystem or hardware failure.

A session represents historical paper replay or a future operating mode and moves
through `CREATED`, `RUNNING`, and a terminal/paused state: `STOPPED`, `COMPLETED`, or
`FAILED`. Only non-terminal sessions can be resumed.

The fill ledger is the sole authoritative source for economic accounting. Recovery:

1. Loads and validates every order and fill.
2. Rebuilds cash, weighted-average positions, costs, realized P&L, and trades by
   applying fills once in database sequence.
3. Reconciles regenerated closed trades against persisted final trade records.
4. Validates position checkpoints and reapplies their latest market marks.
5. Registers only `OPEN`/`PARTIALLY_FILLED` orders with a fresh `PaperBroker`.

Terminal orders never become active again. Persisted `eligible_after` timestamps
preserve next-bar eligibility. Unique fill IDs reject duplicate economic application,
and repeated recovery creates fresh runtime objects without changing economics.

One execution commit atomically stores resulting order states, unique fills, closed
trades, audit events, an after-fill portfolio snapshot, and position checkpoints.
Portfolio snapshots are reporting/debug records, never an alternative accounting
authority. Historical candles are not copied into SQLite.

The schema initializes automatically and has an explicit version. Unsupported
versions and inconsistencies—such as filled quantity without matching fills,
instrument mismatch, incorrect average fill price, trade drift, or checkpoint
drift—fail recovery rather than being guessed or repaired. Structured metadata uses
JSON; pickle/eval serialization is not used.

Create and inspect a persisted synthetic replay:

```bash
python3 scripts/run_paper_replay_demo.py --symbol RELIANCE \
  --date 2026-07-31 --submit-after 10:00 --quantity 10 --persist
python3 scripts/session_status.py --session SESSION_ID
```

Use `--database /path/to/state.sqlite` with either script to override configuration.

## Zerodha live data and manual live paper trading

**THIS MODE DOES NOT PLACE ZERODHA ORDERS.** Kite is used only for read-only market
data. `run_live_feed.py` remains a read-only monitor. The separate manual command
uses a local `LivePaperBroker`, the existing risk/OMS/portfolio pipeline, and SQLite.

```text
KiteTicker callback -> normalize/enqueue Tick -> OneMinuteCandleBuilder
                    -> TickEvent/CandleEvent -> read-only TradingEngine
```

Callbacks only normalize and enqueue. A normal consumer thread processes the bounded
standard-library queue. Full subscription mode is the default because it supplies
exchange timestamps, cumulative session volume, and market depth; only best bid/ask
are retained. Quote/LTP modes can be configured, but packets without an exchange
timestamp are deliberately excluded from candle aggregation.

Credentials are loaded from `.env` or the environment:

```text
KITE_API_KEY
KITE_API_SECRET
KITE_ACCESS_TOKEN
```

They are never persisted or logged. To obtain an access token, print the login URL,
complete browser login manually, then explicitly exchange the redirect token:

```bash
python3 scripts/zerodha_login.py
python3 scripts/zerodha_login.py --request-token TOKEN_FROM_REDIRECT
```

The helper prints the resulting access-token assignment once for the user to copy to
`.env`; it does not store it automatically and performs no order API calls.

Run one or more read-only subscriptions:

```bash
python3 scripts/run_live_feed.py --symbol RELIANCE
python3 scripts/run_live_feed.py --symbol RELIANCE --symbol SBIN
```

Completed candles print by default; `--ticks` also prints normalized ticks. Ctrl+C
stops reconnect attempts and closes the socket cleanly.

Run a durable manual paper session (all IDs begin with `PAPER-`):

```bash
.venv/bin/python scripts/run_manual_live_paper.py --symbol RELIANCE --symbol SBIN
.venv/bin/python scripts/run_manual_live_paper.py --resume SESSION_ID \
  --symbol RELIANCE --symbol SBIN
```

Commands include `buy RELIANCE 10 market intraday`,
`sell SBIN 10 limit 812.50 swing`, `sell RELIANCE 10 stop 1280 intraday`,
`squareoff intraday`, `complete`, `quote`, `orders`, `positions`, `trades`, `pnl`,
`health`, `cancel PAPER-ID`, and `quit`. Intent defaults to the safer `INTRADAY`
when omitted. Unsubscribed symbols are rejected.
Every manual request passes feed/quote freshness checks, then the existing
`RiskManager`, `OrderManager`, local broker, portfolio, and persistence boundary.

Live MARKET buys use ask and sells use bid; deterministic adverse slippage applies.
If configured, absent depth falls back to LTP and records that degraded source in
audit metadata. LIMIT orders require an executable ask/bid through the limit and
never violate it after slippage. STOP_MARKET triggers from observed LTP, then fills
from the currently observable executable side—so jumps through a stop are retained.
There is no queue-position, partial-fill, or depth-liquidity model.

New orders require a connected, healthy feed and a fresh quote for that instrument.
Stale/disconnected data never fills existing orders; they remain open until a fresh
newly observed tick arrives. Marks use LTP and cannot cause fills. On resume, cash,
positions, P&L and open local orders are restored from SQLite. Events missed while
offline are deliberately not inferred or replayed, and this limitation is journaled.

`INTRADAY` and `SWING` describe holding intent independently of execution type.
Intent follows the order into fills, the weighted-average position campaign, closed
trades, persistence recovery, and reports. One symbol has one campaign: increasing
it with a different intent is rejected as `HOLDING_INTENT_CONFLICT`; reductions are
always permitted and retain the campaign's original intent.

Session times live under `session_controls`. New or increasing exposure is rejected
at/after `new_entry_cutoff`, while reductions and exits remain allowed. Optional
automatic square-off submits ordinary local MARKET closing orders for INTRADAY
positions through feed safety, risk, OMS, `LivePaperBroker`, portfolio, and SQLite.
It never mutates a position or fabricates a fill. Stale/disconnected feeds leave the
position open and produce a durable failure audit event. SWING positions are never
included. `squareoff intraday` invokes the same path manually. At session end,
configured EOD cancellation applies only to open INTRADAY orders.

`quit` produces `STOPPED`, preserving all paper state for `--resume`. `complete`
produces terminal `COMPLETED` only when there are no open positions or open orders.
A SWING position therefore requires `quit`/`STOPPED`, followed by `--resume`;
completion is rejected with a specific continuity blocker. Overnight SWING
continuity uses the same resumable multi-day LIVE_PAPER session and fill ledger—there
is no new capital or cost basis. Starting a separate session creates a separate configured-capital
account; cross-session account continuity is intentionally not implied. Daily-loss
limits are therefore session-ledger based in this version.

Read-only CSV export works for any session status:

```bash
.venv/bin/python scripts/export_session.py --session SESSION_ID
```

The default `data/sessions/SESSION_ID/` output contains `session_summary.csv`,
`orders.csv`, `fills.csv`, `trades.csv`, `positions.csv`, `risk_events.csv`, and
`event_journal.csv`. These are derived artifacts and never update SQLite. Summary
fields cover lifecycle/capital/P&L, order/fill/trade counts, wins/losses, intent-aware
open-position counts, rejection count, and final feed counters when the session
recorded them. Optional export after successful completion is independently
configured; export failure cannot roll back trading records.

Live candles use the same `Candle(interval="1minute", completed=True)` domain model
and Asia/Kolkata start timestamp as historical candles. A candle finalizes when the
next tick for that instrument reaches a later minute, or shortly after its minute
ends through the configurable timer finalizer. Clean shutdown flushes the last
observed candle. Another instrument never creates a candle for an inactive symbol;
missing minutes are counted/logged but never manufactured.
On rollover, the completed prior-minute `CandleEvent` is dispatched before the new
minute's `TickEvent`, preventing consumers from seeing the new price first.

Zerodha volume is treated as cumulative session volume. The first observed minute
uses its first tick as a baseline, so its volume represents only the observed delta.
Consecutive minutes use the previous minute's last cumulative value. After a minute
gap, the new first tick becomes a fresh baseline to avoid assigning multi-minute
volume to one candle. A decrease is warned and, by default, resets the baseline; an
explicit drop policy is also available. Indices or packets without volume produce
zero candle volume.

Exact duplicate ticks are dropped. Ticks older than the last accepted exchange
timestamp, or belonging to an already finalized minute, are dropped and counted;
finalized candles are never reopened. Naive exchange timestamps from the official
client are interpreted as Asia/Kolkata, while absent exchange timestamps remain
`None` rather than being replaced with receipt time.

Feed health exposes connection state, last receive/exchange timestamps, tick count,
reconnect count, malformed/dropped/duplicate/out-of-order counts, volume regressions,
last error, and injected-clock staleness. Default staleness is ten seconds. Manual
live-paper order admission and fill evaluation both enforce feed and per-symbol
quote freshness.

KiteTicker's bounded automatic reconnect is enabled. Subscriptions and full mode are
reapplied on connection, temporary disconnects remain reconnectable, and exhausted
retries surface as errors. Authentication/token failures stop retries with an
actionable message; credentials are never refreshed automatically.

## Structure

```text
config/                    Application, historical-data, execution, and risk settings
scripts/                   Data replay and synthetic execution demonstrations
src/paper_trading/
  models.py                Shared domain records
  events.py                In-process event envelopes
  engine.py                Deterministic orchestration
  market_data/             Parquet/instrument adapters and replay feed
                           Zerodha normalization, live feed, health, candle builder
  execution/               Broker protocol, historical/live paper brokers, OMS
  portfolio/               Weighted-average in-memory accounting
  risk/                    Deterministic pre-trade risk checks
  session_controls.py     Intent, cutoff, EOD, and completion controls
  reporting.py            Read-only session summary and CSV export
  storage/                 SQLite schema, repositories, lifecycle, and recovery
  strategies/              Broker-independent strategy lifecycle
tests/                     Unit and integration tests with small fixtures
```

## Deferred

Detailed Indian-equity charges, margin/borrow rules, partial fills, queue simulation,
STOP_LIMIT execution, exchange calendars, actual strategies, dynamic subscriptions,
and real execution are intentionally absent. SQLite is
local single-process-oriented storage; distributed writers and automated schema
migrations are not supported. Marks are checkpointed in place while historical
snapshots are written only at session and fill boundaries.

Live limitations include no exchange calendar, raw-tick archive, full-depth model,
latency metrics beyond timestamps, or cross-process queue. The terminal and feed
threads only enqueue work; one main loop owns trading and durable-state mutations.

The recommended next phase is supervised operational failure drills and account-level
reconciliation design. Strategies and real Zerodha execution remain separate later
phases.
