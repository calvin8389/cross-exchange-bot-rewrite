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

-- Cycle ledger (one row per open→close round)
CREATE TABLE IF NOT EXISTS cycles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  state TEXT NOT NULL,
  direction TEXT NOT NULL,
  edgex_size REAL,
  lighter_size REAL,
  edgex_entry_price REAL,
  lighter_entry_price REAL,
  opened_at TEXT,
  closed_at TEXT,
  edgex_close_pnl REAL,
  lighter_close_pnl REAL,
  leverage INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- Active position (at most one is_active=1 enforced by partial unique index)
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_id INTEGER NOT NULL REFERENCES cycles(id),
  symbol TEXT NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1,
  edgex_contract_id TEXT,
  edgex_side TEXT,
  edgex_size REAL,
  edgex_entry_price REAL,
  edgex_unrealized_pnl REAL,
  lighter_market_id INTEGER,
  lighter_side TEXT,
  lighter_size REAL,
  lighter_entry_price REAL,
  lighter_unrealized_pnl REAL,
  stop_loss_price REAL,
  target_close_at TEXT,
  opened_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_positions_active ON positions(is_active);
CREATE UNIQUE INDEX IF NOT EXISTS idx_positions_one_active ON positions(is_active) WHERE is_active = 1;
