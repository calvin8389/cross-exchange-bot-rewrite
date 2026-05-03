# cross-exchange-bot-rewrite

Python + asyncio rewrite skeleton for a cross-exchange delta-neutral rotation bot.

## What you can run now (M0/M1)
- SQLite (aiosqlite) store with `bot_kv` + `events`
- Lighter WebSocket background services (no official SDK):
  - `user_stats/{ACCOUNT_INDEX}` for balance
  - `ticker/{MARKET_ID}` for best bid/ask
- A demo `python -m src.main` that prints snapshots and appends them to SQLite.

## Install
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configure
```bash
export LIGHTER_WS_URL=wss://mainnet.zklighter.elliot.ai/stream
export ACCOUNT_INDEX=0
export MARKET_ID=0
```

## Run
```bash
python -m src.main
```

## Outputs
- Console prints every 10 seconds
- `bot.sqlite3` created in project root

## Docs
- See `rewrite_guide.md` for the consolidated rewrite plan and protocol notes.
