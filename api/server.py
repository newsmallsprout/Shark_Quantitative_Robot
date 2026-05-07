"""
Shark 2.0 — FastAPI Server.
Provides REST API + WebSocket for the frontend dashboard.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict

from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Shark 2.0", version="2.0.0")

# Global references (set by create_api_server)
_orchestrator = None
_safety = None
_exchange = None


@app.get("/api/health")
async def health():
    return {"ok": True, "version": "2.0.0", "time": time.time()}


@app.get("/api/status")
async def status():
    status_data = {"mode": "paper", "running": True, "time": time.time()}

    if _orchestrator:
        status_data["orchestrator"] = _orchestrator.get_status()
    if _safety:
        status_data["safety"] = {
            "blocked": _safety.get_blocked_breakers(),
            "breakers": len(_safety._breakers),
        }
    if _exchange:
        status_data["account"] = getattr(_exchange, "get_status", lambda: {})()
        status_data["positions"] = [
            {
                "symbol": p.symbol,
                "side": p.side,
                "size": p.size,
                "entry_price": p.entry_price,
                "leverage": p.leverage,
                "margin": p.margin_used,
                "unrealized_pnl": p.unrealized_pnl,
                "pnl_pct": (p.unrealized_pnl / max(p.margin_used, 1e-9)) * 100,
            }
            for p in getattr(_exchange, "positions", {}).values()
        ]

    return status_data


@app.get("/api/control/{action}")
async def control(action: str):
    """Control actions: pause, resume, stop"""
    if action == "pause" and _orchestrator:
        _orchestrator._running = False
        return {"status": "paused"}
    elif action == "resume" and _orchestrator:
        _orchestrator._running = True
        return {"status": "resumed"}
    return {"status": "unknown_action"}


# WebSocket for real-time updates
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    while True:
        try:
            status = await _get_live_status()
            await websocket.send_json(status)
            await asyncio.sleep(1)
        except Exception:
            break


async def _get_live_status() -> Dict:
    data = {"time": time.time()}
    if _exchange:
        data["equity"] = getattr(_exchange, "equity", 0)
        data["balance"] = getattr(_exchange, "balance", 0)
        data["positions"] = getattr(_exchange, "position_count", 0)
        data["realized_pnl"] = getattr(_exchange, "realized_pnl", 0)
        data["win_rate"] = getattr(_exchange, "win_rate", 0)
    if _safety:
        data["safety_blocked"] = bool(_safety.get_blocked_breakers())
    return data


# Serve static dashboard
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML


async def create_api_server(orchestrator, safety, exchange, port: int = 8002):
    """Initialize globals and start uvicorn."""
    global _orchestrator, _safety, _exchange
    _orchestrator = orchestrator
    _safety = safety
    _exchange = exchange

    import uvicorn
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()


# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🦈 Shark 2.0</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0e17;color:#e0e0e0;font-family:'SF Mono','Fira Code',monospace;padding:20px}
.header{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;padding-bottom:16px;border-bottom:1px solid #1a2030}
.header h1{font-size:24px;color:#00d4ff}
.status-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.status-dot.green{background:#00ff88}
.status-dot.red{background:#ff4444}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}
.card{background:#111827;border:1px solid #1e293b;border-radius:8px;padding:16px}
.card h2{font-size:14px;color:#64748b;margin-bottom:8px;text-transform:uppercase;letter-spacing:1px}
.card .value{font-size:28px;font-weight:700}
.value.positive{color:#00ff88}
.value.negative{color:#ff4444}
.value.neutral{color:#00d4ff}
table{width:100%;border-collapse:collapse}
th,td{padding:8px 12px;text-align:left;font-size:13px}
th{color:#64748b;border-bottom:1px solid #1e293b}
td{border-bottom:1px solid #0f1727}
td.positive{color:#00ff88}
td.negative{color:#ff4444}
.footer{margin-top:24px;padding-top:16px;border-top:1px solid #1a2030;font-size:12px;color:#475569}
</style>
</head>
<body>
<div class="header">
  <h1>🦈 Shark 2.0</h1>
  <div>
    <span class="status-dot green" id="dot"></span>
    <span id="mode">Paper Trading</span>
  </div>
</div>

<div class="grid" id="kpis"></div>

<h2 style="margin:20px 0 12px;color:#64748b;font-size:14px;text-transform:uppercase;letter-spacing:1px">Positions</h2>
<table id="positions-table">
  <thead><tr><th>Symbol</th><th>Side</th><th>Size</th><th>Entry</th><th>Lev</th><th>PnL%</th><th>Unrealized</th></tr></thead>
  <tbody id="positions-body"></tbody>
</table>

<div class="footer">
  Shark 2.0 · AI-Driven Multi-Strategy Bot · <span id="update-time">--</span>
</div>

<script>
const WS_URL = `ws://${location.host}/ws`;
let ws;

function connect() {
  ws = new WebSocket(WS_URL);
  ws.onmessage = (e) => {
    const d = JSON.parse(e.data);
    updateDashboard(d);
  };
  ws.onclose = () => setTimeout(connect, 2000);
  ws.onerror = () => ws.close();
}

function updateDashboard(d) {
  document.getElementById('dot').className = 'status-dot ' + (d.safety_blocked ? 'red' : 'green');
  document.getElementById('update-time').textContent = new Date(d.time*1000).toLocaleTimeString();

  const kpis = document.getElementById('kpis');
  const equityClass = d.equity >= 100 ? 'positive' : 'negative';
  const pnlClass = d.realized_pnl >= 0 ? 'positive' : 'negative';
  kpis.innerHTML = `
    <div class="card"><h2>Equity</h2><div class="value ${equityClass}">$${d.equity?.toFixed(2) || '--'}</div></div>
    <div class="card"><h2>Balance</h2><div class="value neutral">$${d.balance?.toFixed(2) || '--'}</div></div>
    <div class="card"><h2>Realized PnL</h2><div class="value ${pnlClass}">${d.realized_pnl >= 0 ? '+' : ''}${d.realized_pnl?.toFixed(4) || '--'}</div></div>
    <div class="card"><h2>Win Rate</h2><div class="value neutral">${(d.win_rate*100)?.toFixed(1) || '--'}%</div></div>
    <div class="card"><h2>Positions</h2><div class="value neutral">${d.positions || 0}</div></div>
    <div class="card"><h2>Safety</h2><div class="value ${d.safety_blocked ? 'negative' : 'positive'}">${d.safety_blocked ? 'BLOCKED' : 'OK'}</div></div>
  `;
}

connect();
</script>
</body>
</html>"""
