"""Live monitoring dashboard for the cross-exchange bot.

Usage:
  python -m src.dashboard          # starts on http://localhost:8080
  python -m src.dashboard --port 9090 --db bot.sqlite3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp.web as web
import aiosqlite

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cross-Exchange Bot — Dashboard</title>
<style>
  :root {
    --bg: #0d1117; --panel: #161b22; --border: #30363d;
    --text: #c9d1d9; --muted: #8b949e; --green: #3fb950;
    --red: #f85149; --yellow: #d2991d; --blue: #58a6ff;
    --accent: #1f6feb; --row-even: #161b22; --row-hover: #1c2129;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'SF Mono', 'Monaco', 'Courier New', monospace;
    background: var(--bg); color: var(--text); font-size: 13px; line-height: 1.4;
  }
  .topbar {
    display: flex; align-items: center; gap: 24px;
    padding: 12px 20px; background: var(--panel); border-bottom: 1px solid var(--border);
  }
  .topbar .state {
    font-size: 14px; font-weight: 700; padding: 4px 12px; border-radius: 4px;
  }
  .state-IDLE, .state-WAITING { background: #1b3a2a; color: var(--green); }
  .state-ANALYZING, .state-HOLDING { background: #1a2e3a; color: var(--blue); }
  .state-OPENING, .state-CLOSING { background: #3a351a; color: var(--yellow); }
  .state-ERROR { background: #3a1a1a; color: var(--red); animation: blink 1s infinite; }
  @keyframes blink { 50% { opacity: 0.5; } }
  .topbar .info { color: var(--muted); }
  .topbar .info b { color: var(--text); }
  .grid {
    display: grid; grid-template-columns: 1fr 2fr 1fr;
    gap: 1px; background: var(--border); height: calc(100vh - 53px);
  }
  .panel {
    background: var(--panel); padding: 16px; overflow-y: auto;
  }
  .panel h2 {
    font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px;
    color: var(--muted); margin-bottom: 12px; padding-bottom: 8px;
    border-bottom: 1px solid var(--border);
  }
  .scan-row {
    display: flex; justify-content: space-between; align-items: center;
    padding: 6px 8px; border-bottom: 1px solid var(--border);
  }
  .scan-row:nth-child(even) { background: var(--row-even); }
  .scan-row:hover { background: var(--row-hover); }
  .scan-row .sym { font-weight: 600; color: var(--blue); width: 60px; }
  .scan-row .pair { color: var(--muted); width: 80px; font-size: 11px; }
  .scan-row .apr { width: 70px; text-align: right; }
  .scan-row .spread { width: 60px; text-align: right; color: var(--muted); font-size: 11px; }
  .apr-positive { color: var(--green); }
  .position-card {
    border: 1px solid var(--border); border-radius: 6px; padding: 14px;
    margin-bottom: 14px; background: var(--bg);
  }
  .position-card:last-child { margin-bottom: 0; }
  .position-card .header {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 10px; padding-bottom: 8px; border-bottom: 1px solid var(--border);
  }
  .position-card .symbol { font-size: 16px; font-weight: 700; color: var(--blue); }
  .position-card .net-apr { font-size: 18px; font-weight: 700; }
  .position-card .legs { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .leg { padding: 8px; border-radius: 4px; }
  .leg-long { background: rgba(63,185,80,0.08); border: 1px solid rgba(63,185,80,0.2); }
  .leg-short { background: rgba(248,81,73,0.08); border: 1px solid rgba(248,81,73,0.2); }
  .leg .label { font-size: 11px; color: var(--muted); }
  .leg .value { font-size: 13px; font-weight: 600; }
  .leg-long .value { color: var(--green); }
  .leg-short .value { color: var(--red); }
  .event-row {
    padding: 4px 8px; border-bottom: 1px solid var(--border); font-size: 11px;
    display: flex; gap: 8px;
  }
  .event-row:nth-child(even) { background: var(--row-even); }
  .event-ts { color: var(--muted); min-width: 70px; }
  .event-type { min-width: 100px; font-weight: 600; }
  .level-error { color: var(--red); }
  .level-warning { color: var(--yellow); }
  .level-info { color: var(--muted); }
  .event-msg { flex: 1; }
  .empty { color: var(--muted); font-style: italic; padding: 20px; text-align: center; }
  .pnl { font-size: 11px; color: var(--muted); }
  .pnl-positive { color: var(--green); }
  .pnl-negative { color: var(--red); }
  .refresh { font-size: 10px; color: var(--muted); }
</style>
</head>
<body>
<div class="topbar" id="topbar">
  <div class="state" id="bot-state">—</div>
  <div class="info">Exchanges: <b id="ex-count">—</b></div>
  <div class="info">Positions: <b id="pos-count">—</b></div>
  <div class="info">Total PnL: <b id="total-pnl">—</b></div>
  <div class="info">Uptime: <b id="uptime">—</b></div>
  <div class="refresh" id="refresh-time"></div>
</div>
<div class="grid">
  <div class="panel" id="scan-panel">
    <h2>📊 Last Scan</h2>
    <div id="scan-content"></div>
  </div>
  <div class="panel" id="positions-panel">
    <h2>💼 Positions</h2>
    <div id="positions-content"></div>
  </div>
  <div class="panel" id="events-panel">
    <h2>📋 Events / Alerts</h2>
    <div id="events-content"></div>
  </div>
</div>
<script>
const API = '/api/state';

function fmtUSD(n) {
  if (n == null) return '—';
  const v = Number(n);
  const cls = v >= 0 ? 'pnl-positive' : 'pnl-negative';
  return `<span class="${cls}">$${v.toFixed(2)}</span>`;
}

function fmtApr(n) {
  if (n == null) return '—';
  const v = Number(n);
  const cls = v >= 0 ? 'apr-positive' : '';
  return `<span class="${cls}">${v.toFixed(2)}%</span>`;
}

function renderTopbar(data) {
  const s = data.state || '—';
  const el = document.getElementById('bot-state');
  el.textContent = s;
  el.className = 'state state-' + s;
  document.getElementById('ex-count').textContent = (data.exchanges || []).join(', ');
  document.getElementById('pos-count').textContent = (data.positions || []).length;
  const totalPnl = (data.positions || []).reduce((s, p) => s + (p.total_pnl || 0), 0);
  document.getElementById('total-pnl').innerHTML = fmtUSD(totalPnl);
  if (data.started_at) {
    const sec = Math.floor((Date.now() - new Date(data.started_at + 'Z').getTime()) / 1000);
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    document.getElementById('uptime').textContent = `${h}h ${m}m ${s}s`;
  }
  document.getElementById('refresh-time').textContent = new Date().toLocaleTimeString();
}

function renderScan(data) {
  const el = document.getElementById('scan-content');
  const scan = data.last_scan || [];
  if (!scan.length) { el.innerHTML = '<div class="empty">No scan data yet</div>'; return; }
  let h = '';
  scan.forEach(o => {
    h += `<div class="scan-row">
      <span class="sym">${o.symbol}</span>
      <span class="pair">${o.long_exchange}/${o.short_exchange}</span>
      <span class="apr">${fmtApr(o.net_apr)}</span>
      <span class="spread">spread ${o.spread_pct?.toFixed(1)}%</span>
    </div>`;
  });
  el.innerHTML = h;
}

function renderPositions(data) {
  const el = document.getElementById('positions-content');
  const positions = data.positions || [];
  if (!positions.length) { el.innerHTML = '<div class="empty">No open positions</div>'; return; }
  let h = '';
  positions.forEach(p => {
    const legs = p.legs || [];
    h += `<div class="position-card">
      <div class="header">
        <span class="symbol">${p.symbol}</span>
        <span class="net-apr">${fmtApr(p.net_apr)} APR</span>
      </div>
      <div class="legs">`;
    legs.forEach(leg => {
      const cls = leg.side === 'long' ? 'leg-long' : 'leg-short';
      const sideLabel = leg.side === 'long' ? 'LONG' : 'SHORT';
      h += `<div class="leg ${cls}">
        <div class="label">${leg.exchange_id} · ${sideLabel}</div>
        <div class="value">${leg.size?.toFixed(4)} @ ${leg.entry_price?.toFixed(2)}</div>
        <div class="pnl">uPnL ${fmtUSD(leg.unrealized_pnl)} · rate ${leg.current_rate?.toFixed(6)} · APR ${leg.current_apr?.toFixed(2)}%</div>
      </div>`;
    });
    h += `</div></div>`;
  });
  el.innerHTML = h;
}

function renderEvents(data) {
  const el = document.getElementById('events-content');
  const events = data.recent_events || [];
  if (!events.length) { el.innerHTML = '<div class="empty">No events</div>'; return; }
  let h = '';
  events.forEach(e => {
    const ts = e.ts ? e.ts.slice(11, 19) : '';
    h += `<div class="event-row">
      <span class="event-ts">${ts}</span>
      <span class="event-type level-${e.level}">${e.event_type}</span>
      <span class="event-msg">${e.message || ''}</span>
    </div>`;
  });
  el.innerHTML = h;
}

async function refresh() {
  try {
    const r = await fetch(API);
    const data = await r.json();
    renderTopbar(data);
    renderScan(data);
    renderPositions(data);
    renderEvents(data);
  } catch(err) {
    console.error('fetch error:', err);
  }
}

refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>"""


async def _get_db(db_path: str) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    return conn


async def _query_state(conn: aiosqlite.Connection) -> dict:
    """Build the full dashboard state from the database."""

    # Bot state + started_at
    state = "UNKNOWN"
    started_at = None
    row = await conn.execute("SELECT key, value, updated_at FROM bot_kv WHERE key IN ('state', 'started_at')")
    async for r in row:
        if r["key"] == "state":
            state = r["value"]
        elif r["key"] == "started_at":
            started_at = r["value"]

    # Active positions with legs + latest funding rates
    positions = []
    pos_rows = await conn.execute_fetchall(
        "SELECT * FROM positions WHERE is_active=1 ORDER BY opened_at DESC"
    )
    for pos in pos_rows:
        legs = []
        leg_rows = await conn.execute_fetchall(
            "SELECT * FROM position_legs WHERE position_id=? ORDER BY side",
            (pos["id"],),
        )
        total_pnl = 0.0
        for leg in leg_rows:
            # Latest funding snapshot for this leg
            snap_row = await conn.execute_fetchall(
                "SELECT rate, apr FROM funding_snapshots "
                "WHERE position_id=? AND exchange_id=? ORDER BY recorded_at DESC LIMIT 1",
                (pos["id"], leg["exchange_id"]),
            )
            current_rate = snap_row[0]["rate"] if snap_row else None
            current_apr = snap_row[0]["apr"] if snap_row else None
            total_pnl += leg["unrealized_pnl"] or 0.0
            legs.append({
                "exchange_id": leg["exchange_id"],
                "side": leg["side"],
                "size": leg["size"],
                "entry_price": leg["entry_price"],
                "unrealized_pnl": leg["unrealized_pnl"],
                "current_rate": current_rate,
                "current_apr": current_apr,
            })

        # Net APR from latest snapshot
        net_apr = None
        if legs and len(legs) == 2:
            long_apr = legs[0].get("current_apr") or 0
            short_apr = legs[1].get("current_apr") or 0
            net_apr = abs(long_apr - short_apr)

        positions.append({
            "id": pos["id"],
            "symbol": pos["symbol"],
            "exchange_long": pos["exchange_long"],
            "exchange_short": pos["exchange_short"],
            "opened_at": pos["opened_at"],
            "legs": legs,
            "net_apr": net_apr,
            "total_pnl": total_pnl,
        })

    # Last scan result
    scan_row = await conn.execute_fetchall(
        "SELECT data_json, ts FROM events WHERE event_type='SCAN_RESULT' "
        "ORDER BY ts DESC LIMIT 1"
    )
    last_scan = []
    if scan_row:
        data = json.loads(scan_row[0]["data_json"])
        candidates = data.get("candidates", [])
        for c in candidates[:10]:
            last_scan.append({
                "symbol": c.get("symbol", ""),
                "long_exchange": c.get("long_exchange", c.get("pair", "").split("/")[0] if "pair" in c else ""),
                "short_exchange": c.get("short_exchange", c.get("pair", "").split("/")[1] if "pair" in c else ""),
                "net_apr": c.get("net_apr"),
                "spread_pct": c.get("spread_pct", c.get("spread")),
            })

    # Recent important events
    important_types = (
        "OPENING_START", "OPENING_ROLLBACK", "CLOSING_START", "CLOSING_FAILED",
        "ERROR_STATE", "RECOVERY_UNHEDGED", "SCAN_RESULT",
        "POSITION_OPENED", "POSITION_CLOSED",
    )
    type_filter = ",".join(f"'{t}'" for t in important_types)
    event_rows = await conn.execute_fetchall(
        f"SELECT ts, level, event_type, data_json, message FROM events "
        f"WHERE event_type IN ({type_filter}) "
        f"ORDER BY ts DESC LIMIT 60"
    )
    recent_events = []
    for ev in event_rows:
        msg = ev["message"] or ""
        if not msg:
            try:
                data = json.loads(ev["data_json"])
                if ev["event_type"] == "SCAN_RESULT":
                    n = len(data.get("candidates", []))
                    msg = f"{n} candidates found"
                elif "symbol" in data:
                    msg = f"{data.get('symbol', '')} {data.get('direction', '')}"
            except Exception:
                pass
        recent_events.append({
            "ts": ev["ts"],
            "level": ev["level"],
            "event_type": ev["event_type"],
            "message": msg,
        })

    # Active exchange list
    exchanges = []
    for pos in positions:
        for leg in pos["legs"]:
            eid = leg["exchange_id"]
            if eid not in exchanges:
                exchanges.append(eid)

    return {
        "state": state,
        "started_at": started_at,
        "exchanges": sorted(exchanges) if exchanges else [],
        "positions": positions,
        "last_scan": last_scan,
        "recent_events": recent_events[-50:],
    }


async def handle_state(request: web.Request) -> web.Response:
    db_path = request.app["db_path"]
    conn = await _get_db(db_path)
    try:
        state = await _query_state(conn)
        return web.json_response(state, dumps=lambda o: json.dumps(o, default=str))
    finally:
        await conn.close()


async def handle_index(request: web.Request) -> web.Response:
    return web.Response(text=DASHBOARD_HTML, content_type="text/html")


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    """WebSocket endpoint: pushes state every 3 seconds."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    db_path = request.app["db_path"]
    try:
        while True:
            conn = await _get_db(db_path)
            try:
                state = await _query_state(conn)
                await ws.send_json(state, dumps=lambda o: json.dumps(o, default=str))
            finally:
                await conn.close()
            await asyncio.sleep(3)
    except Exception:
        pass
    return ws


def build_app(db_path: str) -> web.Application:
    app = web.Application()
    app["db_path"] = db_path
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/state", handle_state)
    app.router.add_get("/ws", handle_ws)
    return app


def main():
    parser = argparse.ArgumentParser(description="Cross-exchange bot dashboard")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port (default: 8080)")
    parser.add_argument("--db", default="bot.sqlite3", help="Path to SQLite DB (default: bot.sqlite3)")
    args = parser.parse_args()

    db_path = os.path.abspath(args.db)
    if not os.path.exists(db_path):
        print(f"Database not found: {db_path}")
        print("Start the bot first: python -m src.main")
        return

    print(f"Dashboard → http://localhost:{args.port}")
    print(f"Database  ← {db_path}")
    web.run_app(build_app(db_path), port=args.port, print=lambda *a: None)


if __name__ == "__main__":
    main()
