"""API 路由 + WebSocket + Dashboard HTML。"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette import status as http_status

from observability.context import REQUEST_ID_CTX, RequestIdMiddleware
from utils.license import (
    LicenseMiddleware,
    init_license_middleware,
)

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent

app = FastAPI(title="Shark 2.0")
init_license_middleware(ROOT)
app.add_middleware(RequestIdMiddleware)
app.add_middleware(LicenseMiddleware)


def _default_paper_trading_enabled() -> bool:
    if os.environ.get("SHARK_MODE", "paper").strip().lower() != "paper":
        return False
    raw = os.environ.get("SHARK_AUTO_START_PAPER", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


_state = {
    "equity": 500.0, "balance": 500.0, "free_cash": 500.0, "initial_capital": 500.0,
    "unrealized_pnl": 0.0, "realized_pnl": 0.0, "win_rate": 0.0,
    "positions": 0, "safety_blocked": False, "fuse_reason": "", "live_api_ok": True,
    "last_tick_block": None, "symbols": [], "symbol_count": 0, "trades": 0, "wins": 0,
    "position_list": [], "trade_history": [], "total_fees": 0.0, "total_slippage": 0.0,
    "margin_locked": 0.0,
    "paper_trading": _default_paper_trading_enabled(), "live_trading": False, "shark_mode": "paper",
    "dynamic_high_vol_alts": [],
    "planning_status": {"active": False, "phase": "idle", "message": "等待计划刷新", "done": 0, "total": 0},
    "strategy_profile": {
        "stable_capital_pct": 0.60,
        "alt_capital_pct": 0.40,
        "stable_profile": "主流中长线重仓，BTC/ETH/SOL 三仓按60%资金桶分配，严格命中计划入场带才开",
        "alt_profile": "动态热门高波动山寨，方向趋势没坏可扛，10分钟全量刷新",
        "alt_plan_ttl_sec": 120,
    },
}


def get_state() -> dict:
    return _state


# ═══ helpers ═══

def _shark_api_token_configured() -> Optional[str]:
    t = os.environ.get("SHARK_API_TOKEN", "").strip()
    return t if t else None


def _bearer_matches(got: str, expected: str) -> bool:
    import secrets
    if got == "" or expected == "":
        return False
    if len(got) != len(expected):
        return False
    return secrets.compare_digest(got.encode("utf-8"), expected.encode("utf-8"))


async def require_api_token(authorization: Optional[str] = Header(None)) -> None:
    expected = _shark_api_token_configured()
    if not expected:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=http_status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    got = authorization[7:].strip()
    if not _bearer_matches(got, expected):
        raise HTTPException(status_code=http_status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


import math


def _finite_float(v, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _sanitize_ws_value(v):
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, float):
        if math.isfinite(v):
            return v
        return 0.0
    if isinstance(v, (list, tuple)):
        return [_sanitize_ws_value(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _sanitize_ws_value(vv) for k, vv in v.items()}
    return v


def _state_for_websocket() -> dict:
    return _sanitize_ws_value({k: v for k, v in _state.items() if not k.startswith('_')})


# ═══ routes ═══

@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/bootstrap.js")
async def api_bootstrap_js():
    exp = _shark_api_token_configured()
    lic_enabled = os.environ.get("SHARK_LICENSE_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    lines = [
        "window.__SHARK_API_TOKEN__=%s;" % json.dumps(exp or ""),
        "window.__SHARK_LICENSE_ENABLED__=%s;\n" % json.dumps(lic_enabled),
    ]
    body = "\n".join(lines)
    return Response(content=body, media_type="application/javascript", headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/api/snapshot")
async def api_snapshot(token: Optional[str] = Query(None)):
    exp = _shark_api_token_configured()
    if exp:
        if not token:
            raise HTTPException(status_code=401, detail="Unauthorized")
        if not _bearer_matches(str(token).strip(), exp):
            raise HTTPException(status_code=401, detail="Unauthorized")
    return _state_for_websocket()


@app.get("/api/status")
async def status(_: None = Depends(require_api_token)):
    return _state


@app.get("/api/paper/status")
async def paper_status(_: None = Depends(require_api_token)):
    return {"active": True, "trading_enabled": _state.get("paper_trading", False)}


@app.post("/api/paper/toggle")
async def paper_toggle(_: None = Depends(require_api_token)):
    new_val = not _state.get("paper_trading", False)
    _state["paper_trading"] = new_val
    if not new_val:
        _state["paper_close_all"] = True
    return {"trading_enabled": new_val}


@app.post("/api/paper/reset")
async def paper_reset(request: Request, _: None = Depends(require_api_token)):
    try:
        body = await request.json()
        capital = float(body.get("capital", 500))
    except Exception:
        capital = 500.0
    capital = max(50.0, min(1000000.0, capital))
    _state["paper_reset_request"] = {"capital": capital}
    return {"ok": True, "capital": capital}


@app.get("/api/license/check")
async def license_check(request: Request):
    from utils.license import _verify_license_redis, license_from_request
    token = license_from_request(request)
    if not token:
        return {"ok": False, "reason": "missing license"}
    ok, reason = _verify_license_redis(token)
    return {"ok": ok, "reason": reason}


@app.post("/api/license/login")
async def license_login(request: Request):
    try:
        body = await request.json()
        token = str(body.get("license", "")).strip()
    except Exception:
        return {"ok": False, "reason": "请提供 license"}
    if not token:
        return {"ok": False, "reason": "license 不能为空"}
    from utils.license import _verify_license_redis
    ok, reason = _verify_license_redis(token)
    return {"ok": ok, "reason": reason}


@app.post("/api/shark/mode")
async def set_shark_mode(request: Request, _: None = Depends(require_api_token)):
    try:
        body = await request.json()
        new_mode = str(body.get("mode", "")).strip().lower()
    except Exception:
        return {"error": '请提供 {"mode": "paper"|"live"}'}
    if new_mode not in ("paper", "live"):
        return {"error": "mode 必须是 paper 或 live"}
    _state["shark_mode"] = new_mode
    _state["switch_mode_request"] = new_mode
    if new_mode == "live":
        if "live" not in _state:
            _state["live"] = {"active": False, "trading_enabled": False}
        _state["live"]["active"] = True
        _state["paper_trading"] = False
    else:
        _state["paper_trading"] = _default_paper_trading_enabled()
        _state["live_trading"] = False
        if "live" in _state:
            _state["live"]["active"] = False
    return {"ok": True, "mode": new_mode}


@app.get("/api/history")
async def trade_history(offset: int = 0, limit: int = 50, _: None = Depends(require_api_token)):
    trades = _state.get("trade_history", [])
    total = len(trades)
    page = list(reversed(trades))[offset:offset + limit]
    return {"trades": page, "total": total, "offset": offset, "limit": limit}


@app.get("/health")
async def health_check():
    from execution.prod_utils import build_health_check
    pg = _state.get("_plan_gate")
    return build_health_check(plan_gate=pg, positions=_state.get("positions", 0))


@app.get("/api/plans")
async def plans_dashboard():
    pg = _state.get("_plan_gate")
    plans = {}
    fuse_info = None
    if pg:
        try:
            plans = dict(pg.get_all_plans())
            raw = getattr(pg, "fuse_reason", "") or ""
            if raw:
                fuse_info = str(raw)
        except Exception:
            pass
    return {"plans": plans, "fuse": fuse_info, "total": len(plans)}


@app.get("/plans")
async def plans_full_page():
    return HTMLResponse(_PLANS_FULL_PAGE)


@app.get("/api/evo/pending")
async def evo_pending(_: None = Depends(require_api_token)):
    return _state.get("evo_pending", [])


@app.post("/api/evo/approve/{change_id}")
async def evo_approve(change_id: int, _: None = Depends(require_api_token)):
    pending = _state.get("evo_pending", [])
    target = None
    for c in pending:
        if c.get("id") == change_id:
            target = c
            break
    if not target:
        return {"error": f"修改 #{change_id} 不存在"}
    _state["evo_pending"] = [c for c in pending if c.get("id") != change_id]
    _state["evo_apply"] = target
    return {"ok": True}


@app.post("/api/evo/reject/{change_id}")
async def evo_reject(change_id: int, _: None = Depends(require_api_token)):
    pending = _state.get("evo_pending", [])
    target = None
    for c in pending:
        if c.get("id") == change_id:
            target = c
            break
    if not target:
        return {"error": f"修改 #{change_id} 不存在"}
    _state["evo_pending"] = [c for c in pending if c.get("id") != change_id]
    _state.setdefault("evo_cooldown_queue", []).append({
        "type": target.get("type", ""),
        "until": time.time() + 300,
    })
    return {"ok": True}


@app.get("/api/evo/metrics")
async def evo_metrics(_: None = Depends(require_api_token)):
    return {"gen": 0, "population": 0, "best_score": 0}


@app.get("/api/live/status")
async def live_status(_: None = Depends(require_api_token)):
    live = _state.get("live", {"active": False, "trading_enabled": False})
    return live


@app.post("/api/live/toggle")
async def live_toggle(_: None = Depends(require_api_token)):
    live = _state.setdefault("live", {"active": False, "trading_enabled": False})
    live["trading_enabled"] = not live.get("trading_enabled", False)
    if not live["trading_enabled"]:
        _state["live_close_all"] = True
    _state["live_trading"] = live["trading_enabled"]
    return {"trading_enabled": live["trading_enabled"]}


# ═══ WebSocket ═══

@app.websocket("/ws")
async def ws(
    websocket: WebSocket,
    token: Optional[str] = Query(default=None),
    device_mac: Optional[str] = Query(default=None),
):
    await websocket.accept()
    hdr = websocket.headers.get("x-request-id") or websocket.headers.get("X-Request-ID")
    hdr = hdr.strip() if hdr else ""
    rid = (hdr if hdr else str(uuid.uuid4()))[:128]
    REQUEST_ID_CTX.set(rid)
    try:
        while True:
            await asyncio.sleep(1)
            try:
                payload = json.dumps(_state_for_websocket(), default=str, ensure_ascii=False)
            except Exception:
                continue
            try:
                await websocket.send_text(payload)
            except Exception:
                break
    except Exception:
        pass


import asyncio


# ═══ Dashboard HTML ═══

_DASHBOARD = """<!DOCTYPE html>
<html lang="zh">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Shark 2.0</title>
<style>body{font:14px system-ui;background:#0a0e17;color:#e0e0e0;margin:0;padding:16px}</style>
</head><body><h1>Shark 2.0</h1><p>正在加载...</p>
<script src="/api/bootstrap.js"></script>
</body></html>"""

_PLANS_FULL_PAGE = """<!DOCTYPE html>
<html lang="zh">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Shark RangePlan</title>
<style>*{margin:0;padding:0;box-sizing:border-box}body{background:#0a0e17;color:#e0e0e0;font:14px/1.5 system-ui;padding:16px}</style>
</head><body><h1>RangePlan</h1><div id="grid">加载中...</div>
<script>fetch('/api/plans').then(r=>r.json()).then(d=>{
document.getElementById('grid').innerHTML=JSON.stringify(d.plans||{},null,2).replace(/\\n/g,'<br>')}).catch(e=>{document.getElementById('grid').innerHTML='API错误: '+e.message})</script>
</body></html>"""

_PLAN_PANEL_SCRIPT = """
<script>
(function(){
var p=document.createElement('div');
p.id='shark-plan-panel';
p.style.cssText='position:fixed;bottom:12px;right:12px;z-index:99999;background:rgba(10,10,30,0.92);border:1px solid rgba(0,255,200,0.3);border-radius:10px;padding:10px 14px;font:11px/1.5 monospace;color:#0f8;min-width:240px;max-height:300px;overflow-y:auto;backdrop-filter:blur(8px);';
document.body.appendChild(p);
function refresh(){fetch('/api/plans').then(r=>r.json()).then(d=>{
var h='<b>Plans ('+(d.total||0)+')</b><br>';
var plans=d.plans||{};
for(var k in plans){var pl=plans[k];h+=k+' Lv'+pl.leverage+'x @'+pl.plan_price+'<br>'}
p.innerHTML=h;}).catch(function(e){p.innerHTML='<div style=color:#f44>Plan API错误</div>'})}
setInterval(refresh,5000);refresh();
})();
</script>
"""


def _ensure_bootstrap_script(html: str) -> str:
    if "bootstrap.js" in html:
        return html
    if '<script type="module"' in html:
        return html.replace(
            '<script type="module"',
            '<script src="/api/bootstrap.js"></script>\n  <script type="module"',
            1,
        )
    if "</body>" in html:
        return html.replace(
            "</body>",
            '  <script src="/api/bootstrap.js"></script>\n</body>',
            1,
        )
    return html


def _inject_plan_panel(html: str) -> str:
    if "</body>" in html:
        return html.replace("</body>", _PLAN_PANEL_SCRIPT + "\n</body>", 1)
    return html + _PLAN_PANEL_SCRIPT


@app.get("/")
async def index():
    react_index = ROOT / "web" / "dist" / "index.html"
    if react_index.exists():
        html = react_index.read_text()
        html = _ensure_bootstrap_script(html)
        html = _inject_plan_panel(html)
        return HTMLResponse(html)
    return HTMLResponse(_DASHBOARD)


def register_static_mounts():
    _react_dist = ROOT / "web" / "dist"
    if _react_dist.exists() and (_react_dist / "assets").exists():
        app.mount("/assets", StaticFiles(directory=str(_react_dist / "assets")), name="react_assets")
    _public_dir = ROOT / "web" / "public"
    if _public_dir.exists():
        app.mount("/public", StaticFiles(directory=str(_public_dir)), name="public")
    _static_dir = ROOT / "static"
    if _static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")
    _pet_video_dir = ROOT / "web" / "video"
    if _pet_video_dir.exists():
        app.mount("/video", StaticFiles(directory=str(_pet_video_dir)), name="video")

register_static_mounts()
