PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS bot_kv (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  level TEXT NOT NULL,
  event_type TEXT NOT NULL,
  cycle_id INTEGER,
  position_id INTEGER,
  data_json TEXT NOT NULL,
  message TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);

-- Cycle ledger (one row per open->close round)
CREATE TABLE IF NOT EXISTS cycles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  state TEXT NOT NULL,
  direction TEXT NOT NULL,
  exchange_long TEXT NOT NULL,
  exchange_short TEXT NOT NULL,
  long_size REAL,
  short_size REAL,
  long_entry_price REAL,
  short_entry_price REAL,
  long_close_pnl REAL DEFAULT 0.0,
  short_close_pnl REAL DEFAULT 0.0,
  long_funding_pnl REAL DEFAULT 0.0,
  short_funding_pnl REAL DEFAULT 0.0,
  leverage INTEGER,
  opened_at TEXT,
  closed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- Active position (at most one is_active=1 enforced by partial unique index)
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_id INTEGER NOT NULL REFERENCES cycles(id),
  symbol TEXT NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1,
  exchange_long TEXT NOT NULL,
  exchange_short TEXT NOT NULL,
  opened_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_positions_active ON positions(is_active);

-- Per-leg details (one row per exchange leg of an active position)
CREATE TABLE IF NOT EXISTS position_legs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id INTEGER NOT NULL REFERENCES positions(id),
  exchange_id TEXT NOT NULL,
  side TEXT NOT NULL,
  size REAL NOT NULL,
  entry_price REAL NOT NULL,
  unrealized_pnl REAL DEFAULT 0.0,
  close_price REAL,
  market_id TEXT,
  opened_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_legs_position ON position_legs(position_id);

-- Funding rate snapshots per position leg (recorded each HOLDING check interval)
CREATE TABLE IF NOT EXISTS funding_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id INTEGER NOT NULL REFERENCES positions(id),
  exchange_id TEXT NOT NULL,
  rate REAL NOT NULL,
  apr REAL NOT NULL,
  recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_funding_snap_pos ON funding_snapshots(position_id);

-- Actual funding payments settled by exchanges (deduplicated by ts+exchange)
CREATE TABLE IF NOT EXISTS funding_payments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id INTEGER NOT NULL REFERENCES positions(id),
  exchange_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  amount REAL NOT NULL,
  rate REAL NOT NULL,
  UNIQUE(position_id, exchange_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_funding_pay_pos ON funding_payments(position_id);

-- Complete order audit trail: one row per exchange order (OPEN or CLOSE leg)
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_id INTEGER NOT NULL REFERENCES cycles(id),
  position_id INTEGER REFERENCES positions(id),
  exchange_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  action TEXT NOT NULL,          -- 'OPEN' or 'CLOSE'
  side TEXT NOT NULL,            -- 'buy' or 'sell'
  order_id TEXT,                 -- exchange-assigned order ID
  order_price REAL NOT NULL,     -- limit price submitted
  fill_price REAL,               -- actual fill price (from exchange)
  size REAL NOT NULL,
  notional REAL,                 -- fill_price * size
  fee REAL DEFAULT 0.0,          -- trading fee in quote currency
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_cycle ON orders(cycle_id);
