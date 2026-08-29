"""SQLite schema for durable sessions and audit state."""

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    trading_date TEXT NOT NULL,
    mode TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    status TEXT NOT NULL,
    starting_capital REAL NOT NULL CHECK (starting_capital > 0),
    strategy_context_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    signal_id TEXT, strategy_id TEXT, strategy_version TEXT,
    exchange TEXT NOT NULL, tradingsymbol TEXT NOT NULL,
    instrument_token INTEGER, instrument_type TEXT NOT NULL,
    tick_size REAL NOT NULL, lot_size INTEGER NOT NULL, segment TEXT,
    side TEXT NOT NULL, order_type TEXT NOT NULL, quantity INTEGER NOT NULL,
    limit_price REAL, stop_price REAL,
    filled_quantity INTEGER NOT NULL, average_fill_price REAL,
    status TEXT NOT NULL, created_at TEXT NOT NULL,
    eligible_after TEXT NOT NULL, updated_at TEXT,
    broker_order_id TEXT, oco_group_id TEXT,
    rejection_reason TEXT, cancellation_reason TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_orders_session_status ON orders(session_id, status);

CREATE TABLE IF NOT EXISTS fills (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    fill_id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    exchange TEXT NOT NULL, tradingsymbol TEXT NOT NULL,
    instrument_token INTEGER, instrument_type TEXT NOT NULL,
    tick_size REAL NOT NULL, lot_size INTEGER NOT NULL, segment TEXT,
    side TEXT NOT NULL, quantity INTEGER NOT NULL, price REAL NOT NULL,
    timestamp TEXT NOT NULL, fees REAL NOT NULL,
    signal_id TEXT, strategy_id TEXT, strategy_version TEXT
);
CREATE INDEX IF NOT EXISTS idx_fills_session_sequence ON fills(session_id, sequence);

CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    exchange TEXT NOT NULL, tradingsymbol TEXT NOT NULL,
    instrument_token INTEGER, instrument_type TEXT NOT NULL,
    tick_size REAL NOT NULL, lot_size INTEGER NOT NULL, segment TEXT,
    side TEXT NOT NULL, quantity INTEGER NOT NULL,
    entry_price REAL NOT NULL, entry_timestamp TEXT NOT NULL,
    exit_price REAL NOT NULL, exit_timestamp TEXT NOT NULL,
    gross_realized_pnl REAL NOT NULL, costs REAL NOT NULL,
    net_realized_pnl REAL NOT NULL,
    signal_id TEXT, strategy_id TEXT, strategy_version TEXT,
    entry_fill_ids_json TEXT NOT NULL, exit_fill_ids_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    timestamp TEXT NOT NULL, cash REAL NOT NULL, market_value REAL NOT NULL,
    equity REAL NOT NULL, realized_pnl REAL NOT NULL, unrealized_pnl REAL NOT NULL,
    costs REAL NOT NULL, open_positions INTEGER NOT NULL, reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS position_checkpoints (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    instrument_key TEXT NOT NULL, quantity INTEGER NOT NULL,
    average_price REAL NOT NULL, last_price REAL, updated_at TEXT NOT NULL,
    PRIMARY KEY (session_id, instrument_key)
);

CREATE TABLE IF NOT EXISTS risk_events (
    risk_event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    timestamp TEXT NOT NULL, approved INTEGER NOT NULL, reason TEXT NOT NULL,
    approved_quantity INTEGER NOT NULL, signal_id TEXT, order_id TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS event_journal (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    timestamp TEXT NOT NULL, event_type TEXT NOT NULL,
    entity_type TEXT, entity_id TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_journal_session_event ON event_journal(session_id, event_id);
"""
