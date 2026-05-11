#!/usr/bin/env python3
"""Shark 2.0 — 真实模拟量化交易机器人。手续费、滑点、资金费率、合约最大杠杆全部实盘规格。"""

import asyncio, os, sys, time, json, secrets, uuid, math
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import aiohttp

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
_log = logging.getLogger(__name__)
try:
    from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
except ImportError: pass

from persistence.dialogue_store import DialogueStore, resolve_sync_psycopg_url
from persistence.bridge import PersistenceBridge, create_redis
from persistence.repository import AccountRepository
from persistence.session import create_engine_and_sessionmaker
from persistence.redis_rate_limit import fixed_window_allow

_storage_bridge: Optional[PersistenceBridge] = None


async def _wait_gate_rl(name: str = "gateio_rest", limit: int = 30, window_sec: int = 1) -> None:
    """Redis 固定窗口限流；未配置 Redis 时直接通过。"""
    global _storage_bridge
    br = _storage_bridge
    if not br or not br.redis:
        return
    while True:
        if await fixed_window_allow(br.redis, name=name, limit=limit, window_sec=window_sec):
            return
        await asyncio.sleep(0.05)

from dialogue_ammo import (
    dialogue_ammo_loop,
    pop_line,
    seed_offline_dialogue_if_needed,
    set_dialogue_store,
    trade_category_for_close,
    trade_category_for_open,
)
from character_voice import character_llm_config, fetch_loli_dialogue

# 导入AI策略
try:
    from ai_strategy import get_ai_targets, apply_ai_targets
    AI_ENABLED = True
except ImportError:
    AI_ENABLED = False

# 导入AI仓位管理
try:
    from ai_position import AIPositionManager
    AI_POSITION_ENABLED = True
except ImportError:
    AI_POSITION_ENABLED = False

# 导入震荡检测器
try:
    from oscillation import OscillationDetector
    OSC_ENABLED = True
except ImportError:
    OSC_ENABLED = False

# 导入双轨策略
try:
    from dual_strategy import get_config, is_stable, get_capital_limit, is_high_vol_alt
    DUAL_STRATEGY = True
except ImportError:
    DUAL_STRATEGY = False
    def get_config(s): return {}
    def is_stable(s): return False
    def get_capital_limit(b, s): return b
    def is_high_vol_alt(s): return True

# K线缓存（自进化引擎依赖）
try:
    from kline_cache import KlineCache, init_kline_cache, get_kline_cache
    from market_regime import RegimeDetector, REGIME_CONFIG, init_detector, get_detector
    from trade_reflector import Reflector, LossReason
    KLINE_ENABLED = True
except ImportError:
    KLINE_ENABLED = False

# 多交易所价格聚合
try:
    from multi_exchange import MultiExchangeFeed, init_multi_feed, get_multi_feed
    MULTI_ENABLED = True
except ImportError:
    MULTI_ENABLED = False

# ═══════════════════════════════════════════════════════════════════════
# 手续费 / 滑点 / 真实参数
# ═══════════════════════════════════════════════════════════════════════
TAKER_FEE = 0.0005        # Gate.io taker 费率 0.05%
MAKER_FEE = 0.0002        # Gate.io maker 费率 0.02%
SLIPPAGE_MAX = 0.0003     # 最大滑点 0.03%
COOLDOWN_SEC = 10         # 同币对冷却 10s
TRADE_INTERVAL = 1        # 交易循环间隔 1s（200ms盘口匹配）
TP_PCT = 2.0              # 止盈 2%（微利策略：5x手续费即走）
SL_PCT = -6.0             # 止损 -6%
TIMEOUT_SEC = 300         # 超时平仓 5min
MAX_TOTAL_EXPOSURE = 0.95 # 最大总风险敞口 95%

# ═══════════════════════════════════════════════════════════════════════
# 合约规格获取
# ═══════════════════════════════════════════════════════════════════════
@dataclass
class ContractSpec:
    symbol: str
    leverage_max: int = 100
    order_size_min: float = 1
    quanto_multiplier: float = 1
    mark_price: float = 0
    funding_rate: float = 0
    funding_next_apply: float = 0
    taker_fee: float = 0.00075  # Gate.io 默认 taker 费率
    maker_fee: float = -0.0001  # Gate.io 默认 maker 费率（负=返佣）

_contract_cache: Dict[str, ContractSpec] = {}

async def fetch_contract_specs() -> Dict[str, ContractSpec]:
    """获取所有 USDT 合约规格：最大杠杆、最小下单量、标记价格、资金费率。"""
    await _wait_gate_rl()
    url = "https://api.gateio.ws/api/v4/futures/usdt/contracts"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()

    specs = {}
    for c in data:
        sym = str(c.get("name", "")).replace("_USDT", "/USDT")
        if not sym or "/USDT" not in sym:
            continue
        specs[sym] = ContractSpec(
            symbol=sym,
            leverage_max=min(int(c.get("leverage_max", 100) or 100), 125),
            order_size_min=float(c.get("order_size_min", 1) or 1),
            quanto_multiplier=float(c.get("quanto_multiplier", 1) or 1),
            mark_price=float(c.get("mark_price", 0) or 0),
            funding_rate=float(c.get("funding_rate", 0) or 0),
            funding_next_apply=float(c.get("funding_next_apply", 0) or 0),
            taker_fee=float(c.get("taker_fee_rate", 0.00075) or 0.00075),
            maker_fee=float(c.get("maker_fee_rate", -0.0001) or -0.0001),
        )
    return specs


# ═══════════════════════════════════════════════════════════════════════
# 动态交易对发现
# ═══════════════════════════════════════════════════════════════════════
async def fetch_top_symbols(n: int = 30, min_vol: float = 30000) -> List[str]:
    """Fetch top N USDT perpetual symbols ranked by volatility * volume."""
    await _wait_gate_rl()
    url = "https://api.gateio.ws/api/v4/futures/usdt/tickers"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()

    scored = []
    for t in data:
        try:
            vol = float(t.get("volume_24h_quote", 0) or 0)
            chg = abs(float(t.get("change_percentage", 0) or 0))
            sym = str(t.get("contract", "") or "")
            if vol < min_vol or not sym.endswith("_USDT"):
                continue
            score = vol * (1 + chg)
            scored.append((sym.replace("_USDT", "/USDT"), score, vol, chg))
        except Exception as e:
            _log.debug("ticker row skipped: %s", e)
            continue

    scored.sort(key=lambda x: x[1], reverse=True)
    return [s[0] for s in scored[:n]]


# ═══════════════════════════════════════════════════════════════════════
# 行情数据
# ═══════════════════════════════════════════════════════════════════════
@dataclass
class LiveTicker:
    symbol: str; price: float = 0; volume_24h: float = 0; change_pct: float = 0
    funding_rate: float = 0; mark_price: float = 0

class MarketDataFeed:
    def __init__(self): self._cache: Dict[str, LiveTicker] = {}

    async def refresh(self, symbols: List[str]):
        if not isinstance(symbols, (list, tuple)):
            symbols = []
        url = "https://api.gateio.ws/api/v4/futures/usdt/tickers"
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
        tickers = {}
        for t in data:
            sym = str(t.get("contract","")).replace("_USDT","/USDT")
            if sym in symbols:
                tickers[sym] = LiveTicker(
                    symbol=sym,
                    price=float(t.get("last",0) or 0),
                    volume_24h=float(t.get("volume_24h_quote",0) or 0),
                    change_pct=float(t.get("change_percentage",0) or 0),
                    funding_rate=float(t.get("funding_rate", 0) or 0),
                    mark_price=float(t.get("mark_price", 0) or 0),
                )
        self._cache = tickers

    def get_prices(self) -> Dict[str, float]:
        return {s: t.price for s, t in self._cache.items()}

    def get_changes(self) -> Dict[str, float]:
        return {s: t.change_pct for s, t in self._cache.items()}

    def get_funding_rates(self) -> Dict[str, float]:
        return {s: t.funding_rate for s, t in self._cache.items()}

    def get_mark_prices(self) -> Dict[str, float]:
        return {s: t.mark_price for s, t in self._cache.items()}


# ═══════════════════════════════════════════════════════════════════════
# API Server
# ═══════════════════════════════════════════════════════════════════════
from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket
from starlette import status as http_status
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
import uvicorn

from observability.context import REQUEST_ID_CTX, RequestIdMiddleware, configure_logging

app = FastAPI(title="Shark 2.0")
app.add_middleware(RequestIdMiddleware)
_state = {"equity": 100.0, "balance": 100.0, "free_cash": 100.0, "initial_capital": 100.0,
          "unrealized_pnl": 0.0, "realized_pnl": 0.0, "win_rate": 0.0,
          "positions": 0, "safety_blocked": False, "symbols": [], "symbol_count": 0, "trades": 0, "wins": 0,
          "position_list": [], "trade_history": [], "total_fees": 0.0, "total_slippage": 0.0, "margin_locked": 0.0}


def _finite_float(v, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _sanitize_ws_value(v):
    """保证 JSON 无 NaN/Inf，避免浏览器 JSON.parse 整包失败导致前端不更新。"""
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, float):
        return _finite_float(v)
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return {str(k): _sanitize_ws_value(val) for k, val in v.items()}
    if isinstance(v, (list, tuple)):
        return [_sanitize_ws_value(x) for x in v]
    return str(v)


def _state_for_websocket() -> dict:
    return _sanitize_ws_value(dict(_state))


def _position_list_for_state(runner: "StrategyRunner", prices: Dict[str, float]) -> List[dict]:
    out: List[dict] = []
    for sym, pos in runner.positions.items():
        px = float(pos.get("entry", 0) or 0)
        if sym in prices:
            px = _finite_float(prices[sym], px)
        unrealized = runner._gross_pnl_usd(sym, pos, px)
        pnl_pct = unrealized / max(pos["margin"], 1e-9) * 100
        out.append({
            "symbol": sym,
            "side": pos["side"],
            "size": _finite_float(pos["size"]),
            "entry_price": _finite_float(pos["entry"]),
            "leverage": _finite_float(pos["leverage"]),
            "margin": _finite_float(pos["margin"]),
            "unrealized_pnl": _finite_float(unrealized),
            "pnl_pct": _finite_float(pnl_pct),
            "current_price": px,
        })
    return out


def _trade_history_for_state(runner: "StrategyRunner") -> List[dict]:
    rows: List[dict] = []
    for t in runner._trade_history[-200:]:
        rows.append({
            "symbol": t.get("symbol", ""),
            "side": t.get("side", ""),
            "entry_price": _finite_float(t.get("entry_price")),
            "exit_price": _finite_float(t.get("exit_price")),
            "size": _finite_float(t.get("size")),
            "leverage": _finite_float(t.get("leverage")),
            "margin": _finite_float(t.get("margin")),
            "realized_pnl": _finite_float(t.get("realized_pnl")),
            "pnl_pct": _finite_float(t.get("pnl_pct")),
            "reason": str(t.get("reason", "")),
            "fee_open": _finite_float(t.get("fee_open")),
            "fee_close": _finite_float(t.get("fee_close")),
            "gross_pnl": _finite_float(t.get("gross_pnl")),
            "opened_at": _finite_float(t.get("opened_at")),
            "closed_at": _finite_float(t.get("closed_at")),
        })
    return rows


def _shark_api_token_configured() -> Optional[str]:
    t = os.environ.get("SHARK_API_TOKEN", "").strip()
    return t if t else None


def _bearer_matches(got: str, expected: str) -> bool:
    if got == "" or expected == "":
        return False
    if len(got) != len(expected):
        return False
    return secrets.compare_digest(got.encode("utf-8"), expected.encode("utf-8"))


async def require_api_token(authorization: Optional[str] = Header(None)) -> None:
    """若设置 SHARK_API_TOKEN，则要求 Authorization: Bearer <token>。"""
    expected = _shark_api_token_configured()
    if not expected:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=http_status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    got = authorization[7:].strip()
    if not _bearer_matches(got, expected):
        raise HTTPException(status_code=http_status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

@app.get("/api/health")
async def health(): return {"ok": True}


@app.get("/api/bootstrap.js")
async def api_bootstrap_js():
    """运行时注入与 SHARK_API_TOKEN 一致的仪表板密钥（Docker 构建阶段无法读取 .env 中的 VITE_*）。"""
    exp = _shark_api_token_configured()
    body = "window.__SHARK_API_TOKEN__=%s;\n" % (json.dumps(exp or ""))
    return Response(
        content=body,
        media_type="application/javascript",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/api/snapshot")
async def api_snapshot(token: Optional[str] = Query(None)):
    """看板完整快照（与 WS 同源 JSON）。未设置 SHARK_API_TOKEN 时开放；设置时须带与 /ws 相同的 ?token=。"""
    exp = _shark_api_token_configured()
    if exp:
        if not token or not _bearer_matches(token, exp):
            raise HTTPException(status_code=http_status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    return _state_for_websocket()


@app.get("/api/status")
async def status(_: None = Depends(require_api_token)): return _state

@app.get("/api/history")
async def trade_history(
    offset: int = 0,
    limit: int = 50,
    _: None = Depends(require_api_token),
):
    trades = _state.get("trade_history", [])
    total = len(trades)
    page = list(reversed(trades))[offset:offset + limit]
    return {"trades": page, "total": total, "offset": offset, "limit": limit}

@app.websocket("/ws")
async def ws(
    websocket: WebSocket,
    token: Optional[str] = Query(default=None),
):
    hdr = websocket.headers.get("x-request-id") or websocket.headers.get("X-Request-ID")
    hdr = hdr.strip() if hdr else ""
    rid = (hdr if hdr else str(uuid.uuid4()))[:128]
    ctx_tok = REQUEST_ID_CTX.set(rid)
    try:
        exp = _shark_api_token_configured()
        await websocket.accept()
        if exp:
            if not token or not _bearer_matches(token, exp):
                await websocket.close(code=1008)
                return
        await websocket.send_json(_state_for_websocket())
        while True:
            try:
                await websocket.send_json(_state_for_websocket())
                await asyncio.sleep(1)
            except Exception as e:
                _log.debug("ws send loop ended: %s", e)
                break
    finally:
        REQUEST_ID_CTX.reset(ctx_tok)


def _ensure_bootstrap_script(html: str) -> str:
    """旧版构建产物未含 /api/bootstrap.js 时补上一行，避免 SHARK_API_TOKEN 与 VITE 构建不一致导致断连。"""
    if "bootstrap.js" in html:
        return html
    if '<script type="module"' in html:
        return html.replace(
            "<script type=\"module\"",
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


@app.get("/", response_class=HTMLResponse)
async def index():
    react_index = ROOT / "web" / "dist" / "index.html"
    if react_index.exists():
        return HTMLResponse(_ensure_bootstrap_script(react_index.read_text()))
    return HTMLResponse(DASHBOARD)
# Mount React static assets if available
_react_dist = ROOT / "web" / "dist"
if _react_dist.exists() and (_react_dist / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(_react_dist / "assets")), name="react_assets")

# Mount public static assets (background images)
_public_dir = ROOT / "web" / "public"
if _public_dir.exists():
    app.mount("/public", StaticFiles(directory=str(_public_dir)), name="public")

# 宠物舱 MP4：依次尝试 web/video → 仓库根 video → 构建产物 dist/video
_pet_video_dir = next(
    (
        p
        for p in (
            ROOT / "web" / "video",
            ROOT / "video",
            ROOT / "web" / "dist" / "video",
        )
        if p.is_dir()
    ),
    None,
)
if _pet_video_dir is not None:
    app.mount("/video", StaticFiles(directory=str(_pet_video_dir)), name="pet_video")

DASHBOARD = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>🦈 Shark 2.0</title>
<style>*{margin:0;padding:0;box-sizing:border-box}body{background:#0a0e17;color:#e0e0e0;font-family:system-ui,sans-serif;padding:20px}
.header{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;padding-bottom:16px;border-bottom:1px solid #1a2030}
.header h1{font-size:24px;color:#00d4ff}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.dot.on{background:#00ff88;animation:pulse 2s infinite}.dot.off{background:#ff4444}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:20px}
.card{background:#111827;border:1px solid #1e293b;border-radius:8px;padding:14px}
.card h2{font-size:11px;color:#64748b;margin-bottom:6px;text-transform:uppercase;letter-spacing:1px}
.val{font-size:24px;font-weight:700}.val.up{color:#00ff88}.val.down{color:#ff4444}.val.mid{color:#00d4ff}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:8px 12px;text-align:left}
th{color:#64748b;border-bottom:1px solid #1e293b;font-size:11px;text-transform:uppercase}
td{border-bottom:1px solid #0f1727}.up{color:#00ff88}.down{color:#ff4444}
.log{background:#111827;border:1px solid #1e293b;border-radius:8px;padding:12px;margin-top:16px;max-height:200px;overflow-y:auto;font-size:12px;color:#94a3b8;font-family:monospace}
.log .warn{color:#f59e0b}.log .good{color:#00ff88}.log .bad{color:#ff4444}
</style></head><body>
<div class="header"><h1>🦈 Shark 2.0</h1><div><span class="dot on" id="dot"></span><span id="status">Running</span></div></div>
<div class="grid" id="kpi"></div>
<div class="card" style="margin-bottom:16px"><h2>Monitoring</h2><div class="log" id="log">Waiting for data...</div></div>
<script>
const ws=new WebSocket(`ws://${location.host}/ws`);
ws.onmessage=e=>{const d=JSON.parse(e.data);
 document.getElementById('dot').className='dot '+(d.safety_blocked?'off':'on');
 document.getElementById('status').textContent=d.safety_blocked?'BLOCKED':'Running';
 const eq=d.equity>=100?'up':'down';
 const pnl=d.realized_pnl>=0?'up':'down';
 document.getElementById('kpi').innerHTML=`
<div class="card"><h2>Equity</h2><div class="val ${eq}">$${d.equity?.toFixed(2)}</div></div>
<div class="card"><h2>Realized PnL</h2><div class="val ${pnl}">${d.realized_pnl>=0?'+':''}${d.realized_pnl?.toFixed(4)}</div></div>
<div class="card"><h2>Win Rate</h2><div class="val mid">${(d.win_rate*100)?.toFixed(1)}%</div></div>
<div class="card"><h2>Trades</h2><div class="val mid">${d.trades}</div></div>
<div class="card"><h2>Positions</h2><div class="val mid">${d.positions}</div></div>
<div class="card"><h2>Symbols</h2><div class="val mid">${d.symbol_count ?? d.symbols?.length ?? d.symbols ?? 0}</div></div>`;
};
ws.onclose=()=>setTimeout(()=>location.reload(),2000);
</script></body></html>"""


# ═══════════════════════════════════════════════════════════════════════
# 开仓质量过滤
# ═══════════════════════════════════════════════════════════════════════
MIN_VOLUME = 2000000      # 24h 最低成交量 200万
MIN_CHANGE = 1.5          # 最小 24h 涨跌幅 1.5%
MAX_CHANGE = 35.0         # 最大 24h 涨跌幅 35%
MIN_PRICE = 0.01          # 最低价格 $0.01
MAX_POSITIONS = 0          # 0=不限制，有信号就开
MARGIN_PCT = 0.005        # 每仓保证金占权益 0.5%
MAX_MARGIN_PER_POS = 5.0  # 单仓最大保证金

# 看板娘事件序号（前端可对齐最新一条）
_character_event_seq = 0


async def _apply_loli_speech(ev: Dict[str, Any]) -> None:
    """开/平仓后异步拉一句 LLM 台词，覆盖 pop_line 兜底；CHARACTER_LLM=0 或无密钥则跳过。"""
    if os.environ.get("CHARACTER_LLM", "").strip() == "0":
        return
    url, key, model = character_llm_config()
    if not url or not key:
        return
    seq = ev.get("_seq")
    try:
        async with aiohttp.ClientSession() as session:
            out = await fetch_loli_dialogue(session, url, key, model, ev)
    except Exception as e:
        _log.debug("fetch_loli_dialogue failed: %s", e)
        return
    if not out:
        return
    cur = _state.get("character_event")
    if not isinstance(cur, dict) or cur.get("_seq") != seq:
        return
    merged = dict(cur)
    merged["Speech_Text"] = out["Speech"]
    ac = out.get("Action") or ""
    if ac:
        merged["Action_Code"] = ac
    _state["character_event"] = merged


def _schedule_loli_speech(ev: Dict[str, Any]) -> None:
    try:
        asyncio.get_running_loop().create_task(_apply_loli_speech(dict(ev)))
    except RuntimeError:
        pass


# ═══════════════════════════════════════════════════════════════════════
class StrategyRunner:
    def __init__(self, initial_balance=10000.0, persistence: Optional[PersistenceBridge] = None):
        self._initial_capital = float(initial_balance)
        self.balance = initial_balance
        self.equity = initial_balance
        self.static_equity = initial_balance      # 已实现权益（不含浮盈）
        self.peak_static_equity = initial_balance  # static_equity 历史峰值
        self.positions: Dict[str, dict] = {}
        self.realized_pnl = 0.0
        self.gross_realized = 0.0  # 毛利累计（不含手续费）
        self.trades = 0          # 总开仓次数
        self.closed_trades = 0   # 总平仓次数
        self.wins = 0            # 盈利平仓次数
        self.total_fees = 0.0
        self.total_slippage = 0.0
        self._cooldowns: Dict[str, float] = {}
        self._log: List[str] = []
        self._trade_history: List[dict] = []
        self._contract_specs: Dict[str, ContractSpec] = {}
        self._ai_cooldowns: Dict[str, float] = {}
        self._oscillator = OscillationDetector() if OSC_ENABLED else None
        self._osc_avg_count: Dict[str, int] = {}  # 每币对补仓次数
        self._ai_signal_cache: Dict[str, dict] = {}  # sym -> {plan, timestamp}
        self._open_timestamps: list = []  # 开仓时间戳
        self._evolve_tick = 0  # 自进化计数器
        self._regime_cache: Dict[str, dict] = {}  # sym → {regime, diag, cfg} 行情上下文
        self._evolve_patterns: list = []  # 检测到的模式
        self._reflector = Reflector() if KLINE_ENABLED else None  # 止损反思器
        self._evo_margin_mult = 1.0      # 进化保证金倍率
        self._evo_skip_alts = False       # 进化暂停山寨
        self._evo_cooldown_bonus = 0      # 进化额外冷却
        self._persistence = persistence

    def _get_maker_fee(self, sym: str) -> float:
        """从合约API获取实时maker费率"""
        spec = self._contract_specs.get(sym)
        if spec and spec.maker_fee < 0:
            return abs(spec.maker_fee)  # 负费率=返佣
        if spec:
            return spec.maker_fee
        return MAKER_FEE

    async def _fetch_ai_plan(self, sym: str, px: float, funding: float,
                             change: float, vol: float):
        """异步获取AI多层仓位计划（限流：每币对5分钟一次）"""
        now = time.time()
        last_ai = self._ai_cooldowns.get(sym, 0)
        if now - last_ai < 300:  # 5分钟冷却
            return
        self._ai_cooldowns[sym] = now
        
        try:
            plan = await get_ai_targets(sym, px, funding, change, vol)
            if plan and plan.get("targets"):
                # 存入信号缓存（开仓前用）
                self._ai_signal_cache[sym] = {"plan": plan, "ts": now}
                if sym in self.positions:
                    pos = self.positions[sym]
                    # 存储完整AI计划（多层仓位管理用）
                    pos["ai_plan"] = plan
                    pos["ai_targets"] = plan["targets"]
                    pos["ai_stop"] = plan.get("stop_loss")
                    pos["ai_entry"] = plan.get("entry_price", px)
                    conf = plan.get("confidence", 0)
                    rr = plan.get("risk_reward", 0)
                    print(f"[AI] {sym} 置信{conf} 盈亏比{rr:.1f} "
                          f"支撑{plan.get('supports',[])} 阻力{plan.get('resistances',[])}")
            else:
                # 否决/HOLD/无计划：清缓存，避免 120s 内沿用过期 LONG/SHORT
                self._ai_signal_cache.pop(sym, None)
        except Exception as e:
            pass

    def update_contracts(self, specs: Dict[str, ContractSpec]):
        self._contract_specs = specs

    def _persist_margin_delta(
        self,
        prices: Dict[str, float],
        sym: str,
        pos: dict,
        delta_free_cash: float,
        event_type: str,
        note: str,
    ) -> None:
        if not self._persistence or not self._persistence.enabled_db():
            return
        oid = pos.get("order_id")
        if isinstance(oid, str):
            try:
                oid = uuid.UUID(oid)
            except ValueError:
                oid = None
        self._persistence.on_balance_adjustment(
            self,
            prices,
            event_type=event_type,
            delta_free_cash=delta_free_cash,
            sym=sym,
            note=note,
            order_id=oid,
        )

    def _quanto_for(self, sym: str) -> float:
        sp = self._contract_specs.get(sym)
        return float(sp.quanto_multiplier) if sp else 1.0

    def _check_evolution(self):

    def _strategic_entry(self, sym: str, side: str, px: float, regime_value: str) -> float:
        """根据行情类型计算策略性入场价，而非市价盲入。
        趋势市回调入场，震荡市边界入场，突破市追入。"""
        try:
            kc = get_kline_cache() if KLINE_ENABLED else None
            if not kc:
                return px

            highs, lows = kc.get_high_low(sym, "5m")
            closes = kc.get_close(sym, "5m")
            if len(closes) < 10:
                return px

            hh = max(highs[-20:])   # 20根K线高点
            ll = min(lows[-20:])     # 20根K线低点
            ema9 = kc.ema(sym, 9, "5m")
            ema21 = kc.ema(sym, 21, "5m")
            rng = hh - ll

            # ── 强趋势：回调/反弹到EMA入场 ──
            if "strong_trend" in regime_value:
                if "up" in regime_value and side == "long":
                    # 做多：等回调到EMA9，不超过现价1%
                    target = ema9 if ema9 < px else px * 0.995
                    return max(target, px * 0.99)
                elif "down" in regime_value and side == "short":
                    target = ema9 if ema9 > px else px * 1.005
                    return min(target, px * 1.01)

            # ── 弱趋势：小幅偏向趋势方向 ──
            elif "weak_trend" in regime_value:
                if "up" in regime_value and side == "long":
                    return px * 0.997  # 微回调入场
                elif "down" in regime_value and side == "short":
                    return px * 1.003

            # ── 震荡：靠边界入场（做多近支撑，做空近阻力）──
            elif "ranging" in regime_value:
                if side == "long":
                    # 靠近区间下沿30%处
                    target = ll + rng * 0.3
                    return max(target, px * 0.985)  # 最多偏1.5%
                else:
                    target = hh - rng * 0.3
                    return min(target, px * 1.015)

            # ── 突破：追在突破点上方 ──
            elif "breakout" in regime_value:
                if "up" in regime_value and side == "long":
                    return min(hh * 1.002, px * 1.01)  # 略高于前高，但不超过1%
                elif "down" in regime_value and side == "short":
                    return max(ll * 0.998, px * 0.99)

            # ── 乱震/默认：市价 ──
            return px

        except Exception:
            return px
        """实时自进化：分析交易模式 → 即时调整参数"""
        recent = self._trade_history[-30:]
        if len(recent) < 3:
            return
        
        # 心跳：确认引擎在跑
        stops = sum(1 for t in recent if "止损" in t.get("reason", ""))
        wins = sum(1 for t in recent if t["realized_pnl"] > 0)
        consecutive = 0
        for t in reversed(recent):
            if t["realized_pnl"] <= 0:
                consecutive += 1
            else:
                break

        # 连亏3+ → 缩减仓位
        if consecutive >= 3:
            self._evo_margin_mult = max(0.25, 1.0 - consecutive * 0.15)
            print(f"[进化] 连亏{consecutive}笔，保证金缩至{self._evo_margin_mult:.0%}", flush=True)

        # 止盈后恢复仓位
        if consecutive == 0 and self._evo_margin_mult < 1.0:
            self._evo_margin_mult = min(1.0, self._evo_margin_mult + 0.1)
            if self._evo_margin_mult >= 1.0:
                print("[进化] 仓位恢复正常", flush=True)

        # 止损率过高 → 放宽止损
        stops = sum(1 for t in recent if "止损" in t.get("reason", ""))
        if len(recent) >= 5 and stops > len(recent) * 0.35:
            if self._reflector:
                self._reflector.stop_boost = min(4, self._reflector.stop_boost + 1.0)
            print(f"[进化] 止损率{stops}/{len(recent)}={stops/len(recent):.0%}，止损+1%", flush=True)

        # 连亏5+ → 暂停山寨
        if consecutive >= 5:
            self._evo_skip_alts = True
            print(f"[进化] 连亏{consecutive}笔，暂停山寨开仓", flush=True)
        elif consecutive == 0:
            self._evo_skip_alts = False

        # 盈利后放宽AI门槛
        wins_recent = sum(1 for t in recent[-10:] if t["realized_pnl"] > 0)
        if wins_recent >= 6 and self._reflector and self._reflector.ai_boost > 0:
            self._reflector.ai_boost = max(0, self._reflector.ai_boost - 3)
            print(f"[进化] 近10笔胜{wins_recent}，AI阈值恢复-3", flush=True)

        # 反思器调整（更激进：5笔亏损就触发）
        if self._reflector and self._reflector.total_losses >= 5:
            adj = self._reflector.maybe_adjust()
            if adj:
                print(f"[进化-反思] {adj}", flush=True)
        
        # 引擎心跳（每次检查都打印关键指标）
        _stops = sum(1 for t in recent if "止损" in t.get("reason", ""))
        _wins = sum(1 for t in recent if t["realized_pnl"] > 0)
        print(f"[进化] tick={self._evolve_tick} 交易{len(recent)}笔 连亏{consecutive} 止损率{_stops}/{len(recent)} "
              f"保证金×{self._evo_margin_mult:.1f} AI阈值+{self._reflector.ai_boost if self._reflector else 0}", flush=True)

    def _gross_pnl_usd(self, sym: str, pos: dict, px: float) -> float:
        """合约张数 × 面值 × 价差 → USDT 毛利（与 Gate 线性 USDT 本位一致）。"""
        q = self._quanto_for(sym)
        if pos["side"] == "long":
            return pos["size"] * q * (px - pos["entry"])
        return pos["size"] * q * (pos["entry"] - px)

    def _est_fee_usd(self, sym: str, pos: dict, px: float, fee_rounds: float = 3.0) -> float:
        """按当前名义估算平仓侧手续费倍数（与止盈里原 *3 口径一致）。"""
        q = self._quanto_for(sym)
        fee_r = self._get_maker_fee(sym)
        return pos["size"] * q * px * fee_r * fee_rounds

    def _take_profit_net_ok(self, sym: str, pos: dict, px: float, fee_rounds: float = 3.0) -> bool:
        """毛利扣估算手续费后仍有意义，避免 net > est_fee 的翻倍门槛锁死大单止盈。"""
        gross = self._gross_pnl_usd(sym, pos, px)
        est = self._est_fee_usd(sym, pos, px, fee_rounds)
        net = gross - est
        return net >= max(0.05, 0.25 * est)

    async def tick(self, prices: Dict[str, float], volumes: Dict[str, float],
                   changes: Dict[str, float], funding_rates: Dict[str, float],
                   mark_prices: Dict[str, float] = None):
        now = time.time()

        # 清理过期冷却
        self._cooldowns = {k: v for k, v in self._cooldowns.items() if now < v}

        # ── 自进化实时检测（每50 tick）──
        self._evolve_tick += 1
        if self._evolve_tick % 20 == 0 and len(self._trade_history) >= 3:
            self._check_evolution()

        # 喂震荡检测器
        if self._oscillator:
            for sym, px in prices.items():
                if px > 0:
                    self._oscillator.feed(sym, px)

        # 检查持仓：动态止损 / 移动止盈 / 浮盈加仓
        for sym in list(self.positions):
            pos = self.positions[sym]
            px = prices.get(sym, 0)
            if px <= 0: continue

            if pos["side"] == "long":
                pnl_pct = (px - pos["entry"]) / pos["entry"] * pos["leverage"] * 100
                price_move = (px - pos["entry"]) / pos["entry"]
            else:
                pnl_pct = (pos["entry"] - px) / pos["entry"] * pos["leverage"] * 100
                price_move = (pos["entry"] - px) / pos["entry"]

            # 获取策略配置
            cfg = get_config(sym)
            is_st = is_stable(sym)
            
            # 行情止损覆盖（开仓时判定的行情类型）
            _rc = self._regime_cache.get(sym, {}).get("cfg", {})
            _stop_ov = _rc.get("stop_pct")
            _tp_ov = _rc.get("tp_pct")
            
            # 波动率（24h变化% → 动态止损宽度）
            vol_chg = abs(pos.get("vol_chg", 3.0))
            # 止损：行情优先 > 策略默认
            _base_sl = _stop_ov if _stop_ov is not None else cfg.get("stop_loss_base", -6.0)
            # 反思器止损放宽（累积发现止损过紧）
            _sl_boost = self._reflector.stop_boost if self._reflector else 0
            sl_base = _base_sl - _sl_boost  # 更负=更宽
            sl_max = cfg.get("stop_loss_max", -200.0)
            dyn_sl = -max(abs(sl_base), min(abs(sl_max), vol_chg * 2.0))
            # 移动止盈
            trail_trigger = cfg.get("trail_trigger", vol_chg * 0.8)
            trail_ratio = cfg.get("trail_pct", 0.3)

            # 更新最高盈利
            if pnl_pct > pos.get("best_pnl", -999):
                pos["best_pnl"] = pnl_pct
                pos["best_price"] = px

            best_pnl = pos.get("best_pnl", pnl_pct)

            # ── 浮亏补仓（震荡模式均价拉低） ──
            if self._oscillator and pnl_pct < -3.0:
                pside = pos["side"]
                should_add, add_ratio, reason = self._oscillator.should_avg_down(
                    sym, px, pos["entry"], pside, pnl_pct)
                if should_add:
                    avg_count = self._osc_avg_count.get(sym, 0)
                    if avg_count < 2:  # 最多补 2 次
                        add_m = pos["margin"] * add_ratio
                        if add_m >= 0.3 and self.balance > add_m + pos["margin"]:
                            q_av = self._quanto_for(sym)
                            add_s = (add_m * pos["leverage"]) / max(q_av * px, 1e-9)
                            pos["margin"] += add_m
                            self.balance -= add_m
                            pos["size"] += add_s
                            pos["entry"] = (pos["entry"] * (pos["size"] - add_s) + px * add_s) / pos["size"]
                            self._osc_avg_count[sym] = avg_count + 1
                            self.trades += 1
                            print(f"[补仓] {sym} {reason} 均价→{pos['entry']:.4f} 余{self._osc_avg_count[sym]}/2")
                            self._persist_margin_delta(prices, sym, pos, -add_m, "margin_add", f"osc_avg:{reason}")

            # ── AI 多层仓位管理（主逻辑） ──
            ai_plan = pos.get("ai_plan")
            if ai_plan:
                pside = pos["side"]
                # 1. AI 止损（含方向校验）
                ai_sl = ai_plan.get("stop_loss")
                if ai_sl:
                    # 方向校验：做多止损应在 entry 下方，做空在上方
                    sl_valid = (pside == "long" and ai_sl < pos["entry"]) or \
                               (pside == "short" and ai_sl > pos["entry"])
                    sl_hit = (pside == "long" and px <= ai_sl) or (pside == "short" and px >= ai_sl)
                    if sl_valid and sl_hit:
                        self._close_position(sym, px, f"AI止损{ai_sl:.2f}", pnl_pct, prices)
                        continue
                    elif not sl_valid and sl_hit:
                        # 止损价在盈利方向 → 当作止盈触发
                        self._close_position(sym, px, f"AI目标{ai_sl:.2f}", pnl_pct, prices)
                        continue

                # 2. AI 防守区
                def_zone = ai_plan.get("add_zone", {})
                if def_zone:
                    dz_price = def_zone.get("price", 0)
                    in_defense = (pside == "long" and px <= dz_price) or (pside == "short" and px >= dz_price)
                    if in_defense and pnl_pct < 0 and not pos.get("defense_used"):
                        # 成交量判断：缩量补仓，放量减仓
                        sym_vol = volumes.get(sym, 0)
                        avg_vols = [volumes.get(s, 0) for s in list(volumes.keys())[:20]]
                        med_vol = sorted(avg_vols)[len(avg_vols)//2] if avg_vols else sym_vol
                        vol_ratio = sym_vol / max(med_vol, 1)
                        if vol_ratio < 1.2:  # 缩量 → 补仓
                            add_m = min(pos["margin"] * 0.3, self.balance * 0.02, 2.0)
                            if add_m >= 0.3 and self.balance > add_m + pos["margin"]:
                                q_df = self._quanto_for(sym)
                                add_s = (add_m * pos["leverage"]) / max(q_df * px, 1e-9)
                                pos["margin"] += add_m
                                self.balance -= add_m
                                pos["size"] += add_s
                                pos["entry"] = (pos["entry"] * (pos["size"] - add_s) + px * add_s) / pos["size"]
                                pos["defense_used"] = True
                                self.trades += 1
                                print(f"[AI防守] {sym} 缩量补仓 {add_m:.2f}@ {px:.4f}")
                                self._persist_margin_delta(prices, sym, pos, -add_m, "margin_add", "ai_defense_add")
                        else:  # 放量 → 减仓
                            reduce_ratio = 0.3
                            reduce_s = pos["size"] * reduce_ratio
                            pos["size"] -= reduce_s
                            pos["margin"] *= (1 - reduce_ratio)
                            pos["defense_used"] = True
                            print(f"[AI防守] {sym} 放量减仓 {reduce_ratio*100:.0f}%")

                # 3. AI 目标层（按价格排序）
                targets = sorted(ai_plan.get("targets", []), key=lambda t: t.get("price", 0))
                for t in targets:
                    tp = t.get("price", 0)
                    act_type = t.get("action", "take_profit")
                    ratio = t.get("ratio", 0.5)
                    hit = (pside == "long" and px >= tp) or (pside == "short" and px <= tp)
                    if not hit: continue
                    layer_key = f"layer_{tp:.0f}"
                    if pos.get(layer_key): continue  # 已执行

                    if act_type == "pyramid_add" and pos.get("pyramid_count", 0) < 4:
                        # ── 利润垫加仓：先收割再博弈 ──
                        # 步骤A：平掉 30% 底仓落袋利润
                        harvest_ratio = 0.3
                        harvest_size = pos["size"] * harvest_ratio
                        qh = self._quanto_for(sym)
                        harvest_pnl = (
                            harvest_size * qh * (px - pos["entry"])
                            if pside == "long"
                            else harvest_size * qh * (pos["entry"] - px)
                        )
                        # 扣 Maker 手续费
                        fee_r = self._get_maker_fee(sym)
                        harvest_fee = harvest_size * qh * px * fee_r
                        net_harvest = harvest_pnl - harvest_fee
                        
                        if net_harvest <= 0:
                            continue  # 不够覆盖手续费，不动作
                        
                        # 执行收割：减仓 + 入账利润
                        pos["size"] -= harvest_size
                        pos["margin"] *= (1 - harvest_ratio)
                        self.balance += net_harvest
                        self.realized_pnl += net_harvest
                        self.total_fees += harvest_fee
                        self.closed_trades += 1
                        if net_harvest > 0: self.wins += 1
                        self._persist_margin_delta(prices, sym, pos, net_harvest, "partial_realize", "ai_harvest_30pct")
                        
                        print(f"[AI收割] {sym} 平{harvest_ratio*100:.0f}%落袋 ${net_harvest:+.4f}")
                        
                        # 步骤B：用利润作最大回撤额度加仓
                        add_margin = min(net_harvest * 2, pos["margin"] * 0.5, self.balance * 0.05)
                        if add_margin >= 0.3 and self.balance > add_margin:
                            add_size = (add_margin * pos["leverage"]) / max(qh * px, 1e-9)
                            pos["margin"] += add_margin
                            self.balance -= add_margin
                            pos["size"] += add_size
                            open_fee_est = add_size * qh * px * fee_r
                            self.balance -= open_fee_est
                            self.total_fees += open_fee_est
                            pos["entry"] = (pos["entry"] * (pos["size"] - add_size) + px * add_size) / pos["size"]
                            pos["pyramid_count"] = pos.get("pyramid_count", 0) + 1
                            self.trades += 1
                            
                            # 全局止损移至初始开仓价（保本）
                            pos["trailing_stop"] = pos.get("ai_entry", pos["entry"])
                            pos[layer_key] = True
                            print(f"[AI加仓] {sym} 用利润${net_harvest:.4f} 加仓${add_margin:.2f} @{px:.4f} 止损→保本")
                            self._persist_margin_delta(
                                prices, sym, pos, -(add_margin + open_fee_est), "margin_add", "ai_pyramid_profit_add"
                            )
                    elif act_type == "take_profit":
                        if ratio >= 0.8:  # 终极止盈 → 全平
                            fee_r = self._get_maker_fee(sym)
                            qp = self._quanto_for(sym)
                            est_fee = pos["size"] * qp * px * fee_r * 2
                            net_pnl = pos["margin"] * pnl_pct / 100 - est_fee
                            if net_pnl > est_fee * 5:  # 微利即走
                                self._close_position(sym, px, f"AI终极止盈{tp:.2f}", pnl_pct, prices)
                                continue
                        elif ratio > 0 and pnl_pct > 0:  # 部分止盈：有利润就行
                            reduce_s = pos["size"] * ratio
                            pos["size"] -= reduce_s
                            pos["margin"] *= (1 - ratio)
                            pos[layer_key] = True
                            pos["trailing_stop"] = px * 0.99 if pside == "long" else px * 1.01
                            print(f"[AI止盈] {sym} 部分{ratio*100:.0f}% @{px:.4f} 余{pos['size']:.4f}")

            # ── 现有逻辑兜底 ──
            # 浮盈加仓后检查AI目标价
            ai_targets = pos.get("ai_targets")
            if ai_targets:
                actions = apply_ai_targets(pos, px, ai_targets, sym, self)
                for act in actions:
                    if act["type"] == "take_profit":
                        # 微利即走：用统一手续费校验
                        if self._take_profit_net_ok(sym, pos, px):
                            self._close_position(sym, px, f"AI目标{act['price']:.2f}", pnl_pct, prices)
                            break
                    elif act["type"] == "pyramid_add" and pos.get("pyramid_count", 0) < 3:
                        add_m = pos["margin"] * 0.5
                        if add_m >= 0.5 and self.balance > add_m:
                            q_at = self._quanto_for(sym)
                            add_s = (add_m * pos["leverage"]) / max(q_at * px, 1e-9)
                            pos["margin"] += add_m
                            self.balance -= add_m
                            pos["size"] += add_s
                            pos["pyramid_count"] = pos.get("pyramid_count", 0) + 1
                            self.trades += 1
                            # 加仓不单独扣费，费用已含在开仓中
                            self._persist_margin_delta(prices, sym, pos, -add_m, "margin_add", "ai_targets_pyramid")
            # 金字塔加仓（仅主流币）
            pyramid_max = cfg.get("pyramid_levels", 0)
            if pyramid_max > 0 and pnl_pct > vol_chg and pos.get("pyramid_count", 0) < pyramid_max:
                funding = funding_rates.get(sym, 0)
                signal_valid = (
                    (pos["side"] == "short" and funding > 0.0001) or
                    (pos["side"] == "long" and funding < -0.0001) or
                    abs(funding) <= 0.0001  # 中性信号维持原方向
                )
                if signal_valid and self.balance > pos["margin"] * 1.2:
                    add_margin = pos["margin"] * 0.5
                    if add_margin >= 0.5 and self.balance > add_margin:
                        q_py = self._quanto_for(sym)
                        add_size = (add_margin * pos["leverage"]) / max(q_py * px, 1e-9)
                        pos["margin"] += add_margin
                        self.balance -= add_margin
                        pos["size"] += add_size
                        pos["pyramid_count"] = pos.get("pyramid_count", 0) + 1
                        pos["entry"] = (pos["entry"] * (pos["size"] - add_size) + px * add_size) / pos["size"]
                        self.trades += 1
                        self._persist_margin_delta(prices, sym, pos, -add_margin, "margin_add", "funding_pyramid")

            # 移动止盈：从最高点回撤，覆盖手续费
            trail_bar = trail_trigger if not is_st else min(trail_trigger, 5.0)
            if best_pnl > trail_bar:
                trail_pct = abs(dyn_sl) * trail_ratio
                if pnl_pct < best_pnl - trail_pct and pnl_pct > 0:
                    if self._take_profit_net_ok(sym, pos, px):
                        self._close_position(sym, px, "移动止盈", pnl_pct, prices)
                        continue

            # 固定止盈（兜底）：主流用「波动 × 系数」但封顶，避免 vol_chg 大时永远达不到线
            if is_st:
                fixed_tp_pct = max(4.0, min(vol_chg * 0.85, 6.5))
            else:
                fixed_tp_pct = vol_chg * 1.5
            if pnl_pct >= fixed_tp_pct and self._take_profit_net_ok(sym, pos, px):
                self._close_position(sym, px, "止盈", pnl_pct, prices)
                continue

            # 动态止损
            if pnl_pct <= dyn_sl:
                self._close_position(sym, px, "止损", pnl_pct, prices)
                continue

            # 超时平仓已禁用

        # 计算当前总风险敞口
        total_margin = sum(p["margin"] for p in self.positions.values())
        # self.balance 已是扣除锁定保证金后的可支配资金，不再减 total_margin
        available = self.balance
        if available <= 0:
            self._update_state(prices)
            return

        # 开仓：对所有符合条件的币对尽可能开单
        scored = []
        for sym in prices:
            if sym in self._cooldowns: continue
            if sym in self.positions: continue

            vol = volumes.get(sym, 0)
            chg_abs = abs(changes.get(sym, 0))
            
            # 用策略配置过滤（不是全局常量）
            cfg = get_config(sym)
            min_vol = cfg.get("min_volume", MIN_VOLUME)
            min_chg = cfg.get("min_change", MIN_CHANGE)
            max_chg = cfg.get("max_change", MAX_CHANGE)
            
            if vol < min_vol: continue
            if chg_abs < min_chg: continue
            if chg_abs > max_chg: continue
            if prices.get(sym, 0) < MIN_PRICE: continue

            # 评分 = 成交量 * 资金费率极端度（信号越强分越高）
            fr_strength = abs(funding_rates.get(sym, 0)) * 10000
            score = vol * (1 + chg_abs / 100) * (1 + fr_strength)
            scored.append((sym, score, vol, chg_abs))

        scored.sort(key=lambda x: x[1], reverse=True)
        # BTC/ETH 优先尝试开仓（分数排序后仍插到队首）
        scored_stable = [s for s in scored if is_stable(s[0])]
        scored_alt = [s for s in scored if not is_stable(s[0])]
        scored = scored_stable + scored_alt

        # 预取AI计划：对前N个币对并行拉取AI信号（开仓前缓存就位）
        prefetch_tasks = []
        for psym, _, _, _ in scored[:35]:
            if len(prefetch_tasks) >= 4:
                break
            if now - self._ai_cooldowns.get(psym, 0) < 300:
                continue
            prefetch_tasks.append(
                self._fetch_ai_plan(psym, prices[psym],
                                    funding_rates.get(psym, 0),
                                    changes.get(psym, 0),
                                    volumes.get(psym, 0)))
        if prefetch_tasks:
            await asyncio.gather(*prefetch_tasks, return_exceptions=True)

        opened = 0
        for sym, score, vol, chg_abs in scored:
            # 进化引擎：连亏时暂停山寨
            if self._evo_skip_alts and not is_stable(sym):
                continue
            # 总敞口限制（余额 * 95%）
            if total_margin >= self.balance * MAX_TOTAL_EXPOSURE:
                break
            # 持仓数限制（0=不限制）
            if MAX_POSITIONS > 0 and len(self.positions) >= MAX_POSITIONS:
                break

            px = prices[sym]
            change = changes.get(sym, 0)

            # 取合约最大杠杆
            spec = self._contract_specs.get(sym)
            if spec is None and is_stable(sym):
                print(
                    f"[DEBUG-BTC] {sym} 无合约规格，使用默认杠杆/面值",
                    flush=True,
                )
            max_lev = spec.leverage_max if spec else 50

            # 杠杆：合约最大 * 波动衰减（平滑连续）
            # chg_abs=1% → full leverage, chg_abs=50% → 30% of max
            lev_factor = max(0.25, 1.0 / (1 + chg_abs / 25))
            lev = max(1, int(max_lev * lev_factor))

            # 保证金：余额动态比例 × 波动衰减（低波大仓，高波小仓）
            cfg = get_config(sym)
            
            # ── 行情检测：多因子判定行情类型，每币对独立判断 ──
            _regime = None
            _regime_cfg = {}
            if KLINE_ENABLED:
                try:
                    detector = get_detector()
                    if detector:
                        _regime, _diag = detector.detect(sym)
                        _regime_cfg = REGIME_CONFIG.get(_regime, {})
                        # 乱震/死水 → 不开仓
                        if _regime_cfg.get("allowed_dir") is None:
                            continue
                        # 缓存行情上下文
                        self._regime_cache[sym] = {
                            "regime": _regime.value,
                            "diag": _diag,
                            "cfg": _regime_cfg,
                        }
                except Exception:
                    pass
            
            base_pct = cfg.get("margin_pct", 0.01)
            # 行情调整保证金倍率
            if _regime_cfg.get("margin_mult"):
                base_pct *= _regime_cfg["margin_mult"]
            # 主流币额外加成：趋势中放大，震荡中也保持体量
            if is_stable(sym):
                base_pct *= 1.3
            vol_factor = max(0.4, 1.5 / (1 + chg_abs / 12))
            margin = self.balance * base_pct * vol_factor * self._evo_margin_mult

            # 检查最小下单量（考虑 quanto_multiplier）
            quanto = spec.quanto_multiplier if spec else 1.0
            size = (margin * lev) / max(quanto * px, 1e-9)
            bumped_for_min = False
            if spec and size < spec.order_size_min:
                size = spec.order_size_min
                margin = (size * quanto * px) / lev
                bumped_for_min = True

            # 山寨：仅当「为凑最小张数」把保证金抬得过高时跳过（避免误伤正常 0.2%～数% 仓）
            if bumped_for_min and not is_stable(sym):
                alt_ceiling = max(5.0, self.balance * 0.06)
                if margin > alt_ceiling:
                    continue

            # Maker 手续费
            fee_rate_maker = abs(spec.maker_fee) if spec and spec.maker_fee < 0 else TAKER_FEE * 0.2
            fee = size * quanto * px * fee_rate_maker

            # 余额不够就跳过
            if margin + fee > self.balance:
                continue

            # 双轨资金上限：只统计同桶（主流/山寨）已用保证金，避免 BTC+ETH 占仓后山寨永不开单
            cap = get_capital_limit(self.balance, sym)
            st_bucket = is_stable(sym)
            bucket_used = sum(
                p["margin"] for s, p in self.positions.items()
                if is_stable(s) == st_bucket
            )
            if bucket_used + margin > cap:
                continue

            entry_price = px  # 默认市价，策略入场在方向确定后调整

            # 策略类型持仓限制
            max_pos = cfg.get("max_positions", 0)
            if max_pos > 0:
                same_type = sum(1 for s in self.positions if is_stable(s) == is_stable(sym))
                if same_type >= max_pos:
                    continue

            # 方向信号：纯AI委员会，无兜底
            funding = funding_rates.get(sym, 0)
            mark = mark_prices.get(sym, 0) if mark_prices else 0
            price_div = (px - mark) / mark * 100 if mark > 0 else 0
            
            # ── 层级1：AI 信号缓存（最高优先级） ──
            ai_cache = self._ai_signal_cache.get(sym)
            ai_dir_raw = ""
            ai_confidence = 0
            if ai_cache and now - ai_cache.get("ts", 0) < 180:
                ai_plan = ai_cache.get("plan", {})
                ai_dir_raw = (ai_plan.get("direction") or "").strip().upper()
                ai_confidence = float(ai_plan.get("confidence", 0) or 0)
            _ai_conf_min = 45 + (self._reflector.ai_boost if self._reflector else 0)
            ai_use = ai_dir_raw in ("LONG", "SHORT") and ai_confidence >= _ai_conf_min

            # ── 方向信号判定 ──
            if ai_use:
                side = "long" if ai_dir_raw == "LONG" else "short"
                signal_src = f"AI多维 信{int(ai_confidence)}"
            else:
                # ── 多方信号兜底（5因子投票，≥3票同向 + 非费率票≥1）──
                fb_votes = {"long": 0, "short": 0}
                fb_tags = []
                
                # 因子1: 资金费率均值回归
                if abs(funding) > 0.0005:
                    d = "short" if funding > 0 else "long"
                    fb_votes[d] += 1; fb_tags.append(f"费率{funding*100:+.3f}%→{d}")
                
                # 因子2: RSI超买超卖 (5m)
                try:
                    kc = get_kline_cache()
                    if kc:
                        r = kc.rsi(sym, period=14, interval="5m")
                        if 0 < r < 35:
                            fb_votes["long"] += 1; fb_tags.append(f"RSI={r:.0f}超卖")
                        elif r > 65:
                            fb_votes["short"] += 1; fb_tags.append(f"RSI={r:.0f}超买")
                except Exception: pass
                
                # 因子3: 多交易所共识
                if MULTI_ENABLED:
                    try:
                        feed_m = get_multi_feed()
                        if feed_m:
                            ms = feed_m.direction_signal(sym)
                            if ms['divergence'] <= 0.5 and ms['bias'] != 'neutral':
                                fb_votes[ms['bias']] += 1; fb_tags.append(f"多所→{ms['bias']}")
                    except Exception: pass
                
                # 因子4: ADX趋势
                try:
                    kc = get_kline_cache()
                    if kc:
                        a = kc.adx(sym, period=14, interval="1m")
                        t = kc.ma_trend(sym, fast=9, slow=21, interval="1m")
                        if a > 20 and t in ("up", "down"):
                            d2 = "long" if t == "up" else "short"
                            fb_votes[d2] += 1; fb_tags.append(f"ADX={a:.0f}趋势{t}")
                except Exception: pass
                
                # 因子5: 成交量+价格动量
                mv = cfg.get("min_volume", 500000)
                mc = cfg.get("min_change", 1.5)
                if vol > mv * 2 and abs(change) > mc * 2:
                    d3 = "long" if change > 0 else "short"
                    fb_votes[d3] += 1; fb_tags.append(f"量价{change:+.1f}%")
                
                best_dir = max(fb_votes, key=fb_votes.get)
                best_cnt = fb_votes[best_dir]
                non_fee = sum(1 for t in fb_tags if not t.startswith("费率"))
                
                # 主流币放宽：≥1非费率票即开；山寨：≥2票且非费率≥1
                if is_stable(sym):
                    ok = non_fee >= 1 and best_dir in ("long", "short")
                else:
                    ok = best_cnt >= 2 and non_fee >= 1 and best_dir in ("long", "short")
                
                if ok:
                    side = best_dir
                    signal_src = f"多方兜底 {'|'.join(fb_tags)} ✓{best_cnt}"
                else:
                    continue

            # ── 多交易所方向确认（自进化v2）──
            if MULTI_ENABLED:
                try:
                    feed_m = get_multi_feed()
                    if feed_m:
                        sig = feed_m.direction_signal(sym)
                        # 交易所间价差过大 → 方向不确定 → 跳过
                        if sig['divergence'] > 0.5:
                            continue
                        # 多交易所共识方向与开仓方向相反 → 跳过
                        if sig['bias'] != 'neutral' and sig['bias'] != side:
                            if ai_use:
                                if ai_confidence < 70:
                                    continue  # AI信心不足时相信多交易所
                            elif sig.get('confidence', 0) > 40:
                                continue  # 兜底信号时多交易所信心>40则跳过
                except Exception as e:
                    _log.warning("multi_exchange direction_signal failed for %s: %s", sym, e)

            # ── 行情方向约束：趋势行情禁止逆势开仓 ──
            _rc = self._regime_cache.get(sym, {}).get("cfg", {})
            _allowed = _rc.get("allowed_dir")
            if _allowed and _allowed != "both" and side != _allowed:
                continue  # 趋势方向不符 → 不逆势

            # 行情止损/止盈覆盖
            _stop_override = _rc.get("stop_pct")
            _tp_override = _rc.get("tp_pct")
            _pyramid_override = _rc.get("pyramid")

            # 策略入场价：根据行情类型+方向智能定位
            _rv = _regime.value if _regime else "unknown"
            entry_price = self._strategic_entry(sym, side, entry_price, _rv)

            self.positions[sym] = {
                "side": side, "entry": entry_price, "size": size,
                "leverage": lev, "margin": margin, "opened": now,
                "fee_open": fee, "vol_chg": chg_abs,
                "best_pnl": -999, "pyramid_count": 0,
                "ai_targets": None,
                "order_id": uuid.uuid4(),
                "signal_src": signal_src,
                "ai_confidence": ai_confidence if ai_use else 0,
            }
            
            # AI分析（异步，不阻塞开仓）
            if AI_ENABLED:
                asyncio.create_task(self._fetch_ai_plan(sym, px, funding_rates.get(sym, 0),
                                        changes.get(sym, 0), vol))
            self._cooldowns[sym] = now + COOLDOWN_SEC
            self.trades += 1
            total_margin += margin

            fee_str = f" 手续费={fee:.4f}" if fee > 0.0001 else ""
            stype = "主流" if is_stable(sym) else "山寨"
            msg = f"[开仓-{stype}] {sym} {side.upper()} @ {entry_price:.4f} 保证金={margin:.2f} 杠杆={lev}x 信号={signal_src}"
            if _regime:
                msg += f" 行情={_regime.value}"
            if _stop_override:
                msg += f" SL={_stop_override}%"
            if _tp_override:
                msg += f" TP={_tp_override}%"
            
            # 所有检查通过，扣费开仓（冻结保证金 + 手续费）
            self.balance -= margin + fee
            self.total_fees += fee
            if self._persistence and self._persistence.enabled_db():
                oid = self.positions[sym]["order_id"]
                self._persistence.on_position_open(
                    self, prices,
                    order_id=oid,
                    sym=sym, side=side, entry_price=entry_price,
                    size=size, margin=margin, lev=float(lev), fee=fee, opened_ts=now,
                )
            
            self._log.append(msg)
            print(msg, flush=True)
            
            # Alpha角色事件：开仓（短台词 + 可选 LLM 暴走润色）
            side_cn = "多" if side == "long" else "空"
            global _character_event_seq
            _character_event_seq += 1
            seq = _character_event_seq
            speech0 = pop_line(trade_category_for_open())
            ev_open = {
                "Event_Type": f"开仓_{sym}_{side_cn}",
                "Action_Code": "action_sword_draw" if side == "long" else "action_hammer_down",
                "Facial_Expression": "confident",
                "Emotion_Index": 35,
                "Speech_Text": speech0,
                "symbol": sym,
                "side": side,
                "_seq": seq,
            }
            _state["character_event"] = ev_open
            _schedule_loli_speech(ev_open)

            opened += 1

        self._update_state(prices)

    def _close_position(self, sym, px, reason, pnl_pct, prices=None):
        pos = self.positions.pop(sym)
        oid = pos.get("order_id")
        bal_before = self.balance
        self._osc_avg_count.pop(sym, None)  # 清补仓计数

        spec = self._contract_specs.get(sym)
        q = self._quanto_for(sym)
        gross = self._gross_pnl_usd(sym, pos, px)
        fee_rate_maker = abs(spec.maker_fee) if spec and spec.maker_fee < 0 else TAKER_FEE * 0.2
        fee_close = pos["size"] * q * px * fee_rate_maker
        fee_open = pos.get("fee_open", 0)
        gross_pnl = gross  # 毛利（不含手续费）
        realized = gross - fee_open - fee_close  # 含全部手续费的净利

        self.total_fees += fee_close
        print(
            f"[DEBUG费用] 毛利={gross:.6f} 平仓费={fee_close:.6f} 净利={realized:.6f} "
            f"balance={self.balance:.2f} total_fees={self.total_fees:.4f}",
            flush=True,
        )

        self.balance += pos["margin"] + gross - fee_close
        self.realized_pnl += realized
        self.gross_realized += gross  # 毛利累计（不含手续费），用于余额展示
        self.closed_trades += 1
        if realized > 0:
            self.wins += 1

        # 更新 static_equity（平仓后 = 已实现的真实权益，剔除浮盈）
        if prices:
            self._recalc_equity(prices)
        self.static_equity = self.equity
        if self.static_equity > self.peak_static_equity:
            self.peak_static_equity = self.static_equity

        # 记录到交易历史
        closed_ts = time.time()
        self._trade_history.append({
            "symbol": sym, "side": pos["side"],
            "entry_price": pos["entry"], "exit_price": px,
            "size": pos["size"], "leverage": pos["leverage"],
            "margin": pos["margin"], "realized_pnl": realized,
            "pnl_pct": pnl_pct, "reason": reason,
            "fee_open": pos.get("fee_open", 0),
            "fee_close": fee_close,
            "gross_pnl": gross,
            "opened_at": pos["opened"], "closed_at": closed_ts,
            "signal_src": pos.get("signal_src", ""),
            "ai_confidence": pos.get("ai_confidence", 0),
        })
        if self._persistence and self._persistence.enabled_db() and oid:
            ou = oid if isinstance(oid, uuid.UUID) else uuid.UUID(str(oid))
            self._persistence.on_position_close(
                self,
                prices,
                order_id=ou,
                trade_id=uuid.uuid4(),
                sym=sym,
                side=pos["side"],
                entry_price=float(pos["entry"]),
                exit_price=float(px),
                size=float(pos["size"]),
                leverage=float(pos["leverage"]),
                margin=float(pos["margin"]),
                gross_pnl=gross_pnl,
                fee_open=float(fee_open),
                fee_close=float(fee_close),
                realized=float(realized),
                pnl_pct=float(pnl_pct),
                reason=reason,
                opened_ts=float(pos["opened"]),
                closed_ts=float(closed_ts),
                free_cash_before_release=bal_before,
            )

        msg = (
            f"[平仓] {sym} {reason} 盈亏={realized:+.4f} ({pnl_pct:+.1f}%) "
            f"余额={self.balance:.2f} static_equity={self.static_equity:.2f} 累计手续费={self.total_fees:.4f}"
        )
        self._log.append(msg)
        print(msg, flush=True)

        # ── 止损反思：多维分析亏损原因 ──
        if self._reflector and realized < 0:
            self._reflector.analyze(sym, pos, realized, pnl_pct, reason, px,
                                    self._regime_cache, None)

        # Alpha角色事件：平仓
        is_tp = "止盈" in reason
        is_big_win = realized > 1.0
        pnl_abs = abs(realized)
        global _character_event_seq
        _character_event_seq += 1
        seq = _character_event_seq
        if is_tp:
            speech0 = pop_line("profit")
        else:
            speech0 = pop_line(trade_category_for_close(reason, realized))
        ev_close = {
            "Event_Type": f"{'止盈' if is_tp else '止损'}_{sym}",
            "Action_Code": (
                ("action_catch_coin" if is_big_win else "action_fist_pump")
                if is_tp
                else ("action_adjust_glasses" if pnl_abs < 0.5 else "action_shield_up")
            ),
            "Facial_Expression": "excited" if is_tp and is_big_win else ("relaxed" if is_tp else "serious"),
            "Emotion_Index": 20 if is_tp else 65,
            "Speech_Text": speech0,
            "Evolution_Log": (
                f"止盈记录: {sym} +{realized:.4f} ({pnl_pct:+.1f}%)"
                if is_tp
                else f"止损分析: {sym} {realized:.4f} ({pnl_pct:+.1f}%) → 因子权重微调中"
            ),
            "symbol": sym,
            "side": pos["side"],
            "pnl": realized,
            "pnl_pct": pnl_pct,
            "_seq": seq,
        }
        _state["character_event"] = ev_close
        _schedule_loli_speech(ev_close)

        # 平仓后冷却，避免立即重开（止损更长）
        cooldown_sec = 120 if reason == "止损" else 30
        self._cooldowns[sym] = time.time() + cooldown_sec

    def _recalc_equity(self, prices):
        locked = sum(p["margin"] for p in self.positions.values())
        unrealized = 0.0
        for sym, pos in self.positions.items():
            px = prices.get(sym, pos["entry"])
            unrealized += self._gross_pnl_usd(sym, pos, px)
        self.equity = self.balance + locked + unrealized

    def _fund_snapshot(self, prices: Dict[str, float]) -> Dict[str, float]:
        self._recalc_equity(prices)
        locked = sum(p["margin"] for p in self.positions.values())
        uc = self._initial_capital
        total_balance = uc + self.gross_realized - self.total_fees
        unrealized = self.equity - self.balance - locked
        return {
            "equity": self.equity,
            "free_cash": self.balance,
            "total_balance": total_balance,
            "margin_locked": locked,
            "unrealized": unrealized,
        }

    def _update_state(self, prices):
        self._recalc_equity(prices)
        locked = sum(p["margin"] for p in self.positions.values())
        uc = self._initial_capital
        # 余额 = 初始资金 + 毛利累计 - 总手续费（用户公式）
        total_balance = uc + self.gross_realized - self.total_fees
        unrealized = self.equity - self.balance - locked
        
        _state["equity"] = self.equity
        _state["balance"] = total_balance  # 总资金净额
        _state["free_cash"] = self.balance  # 可用余额
        _state["initial_capital"] = uc
        _state["unrealized_pnl"] = unrealized
        _state["realized_pnl"] = self.realized_pnl
        _state["win_rate"] = self.wins / max(self.closed_trades, 1)  # 基于已平仓
        _state["positions"] = len(self.positions)
        _state["trades"] = self.trades
        _state["wins"] = self.wins
        _state["symbol_count"] = len(prices)
        _state["total_fees"] = self.total_fees
        _state["gross_realized"] = self.gross_realized  # 毛利累计
        _state["total_slippage"] = self.total_slippage
        _state["trade_history"] = _trade_history_for_state(self)
        _state["margin_locked"] = locked
        _state["position_list"] = _position_list_for_state(self, prices)

        # 反思器状态供API/战报使用
        if self._reflector:
            _state["reflect"] = {
                "summary": self._reflector.summary(),
                "ai_boost": self._reflector.ai_boost,
                "stop_boost": self._reflector.stop_boost,
            }

        if self._persistence:
            self._persistence.schedule_state_redis(
                {k: _state[k] for k in (
                    "equity", "balance", "free_cash", "initial_capital",
                    "unrealized_pnl", "realized_pnl", "win_rate", "positions",
                    "trades", "wins", "total_fees", "gross_realized", "margin_locked",
                    "symbol_count",
                ) if k in _state}
            )
# ═══════════════════════════════════════════════════════════════════════
# 价格推送循环
# ═══════════════════════════════════════════════════════════════════════
async def price_feed_loop(feed: MarketDataFeed, runner: StrategyRunner, interval: int = 2):
    _state["live_prices"] = {}
    while True:
        try:
            symbols = _state.get("symbols", [])
            if isinstance(symbols, int):
                symbols = []
            if symbols:
                await feed.refresh(symbols)
            prices = dict(feed.get_prices())
            # 持仓币对必须参与权益重算（否则不在本轮 watchlist 时用入场价占位）
            for psym, pos in runner.positions.items():
                if psym not in prices:
                    prices[psym] = float(pos.get("entry", 0) or 0)
            changes = feed.get_changes()
            runner._update_state(prices)
            _state["live_prices"] = {
                sym: {"price": px, "change": changes.get(sym, 0)}
                for sym, px in prices.items()
            }
        except Exception as e:
            print(f"[价格推送错误] {e}", flush=True)
        await asyncio.sleep(interval)


# ═══════════════════════════════════════════════════════════════════════
# 主交易循环
# ═══════════════════════════════════════════════════════════════════════
async def trading_loop(feed: MarketDataFeed, runner: StrategyRunner,
                       interval: int = TRADE_INTERVAL):
    # 启动时获取合约规格
    print("📡 获取合约规格...")
    try:
        specs = await fetch_contract_specs()
        runner.update_contracts(specs)
        print(f"📡 合约规格加载完成: {len(specs)} 个合约", flush=True)
    except Exception as e:
        print(f"[警告] 合约规格获取失败: {e}，使用默认值", flush=True)

    _kline_inited = False
    _tick = 0
    while True:
        try:
            _tick += 1
            symbols = await fetch_top_symbols(n=30)
            # 强制加入 BTC/ETH，山寨只保留高波动精选
            MUST_HAVE = ["BTC/USDT", "ETH/USDT"]
            for m in MUST_HAVE:
                if m not in symbols:
                    symbols.insert(0, m)
            # 过滤山寨：只保留高波动精选
            symbols = [s for s in symbols if is_stable(s) or is_high_vol_alt(s)]
            # 去重保持顺序
            seen = set()
            symbols = [s for s in symbols if not (s in seen or seen.add(s))]
            await feed.refresh(symbols)
            
            # 初始化K线缓存（首次）
            if not _kline_inited:
                try:
                    await init_kline_cache(symbols)
                    print(f"📊 K线缓存初始化完成: {len(symbols)} 个币对", flush=True)
                    # 初始化行情检测器（依赖K线缓存）
                    kc = get_kline_cache()
                    if kc:
                        init_detector(kc)
                        print(f"🔍 行情检测器就绪", flush=True)
                    _kline_inited = True
                except Exception as e:
                    print(f"[警告] K线缓存初始化失败: {e}", flush=True)
            
            # 定期刷新K线（每60s更新一次，保持RSI/ADX新鲜）
            if _kline_inited and _tick % 10 == 0:
                try:
                    kc = get_kline_cache()
                    if kc:
                        for s in symbols:
                            await kc.update(s)
                except Exception:
                    pass
            prices = feed.get_prices()
            volumes = {s: t.volume_24h for s, t in feed._cache.items()}
            changes = feed.get_changes()
            funding_rates = feed.get_funding_rates()
            mark_prices = feed.get_mark_prices()
            await runner.tick(prices, volumes, changes, funding_rates, mark_prices)
            _state["symbols"] = list(symbols)

        except Exception as e:
            _log.error("交易循环: %s", e, exc_info=True)
        await asyncio.sleep(interval)


# ═══════════════════════════════════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════════════════════════════════
async def main():
    configure_logging()
    set_dialogue_store(DialogueStore(resolve_sync_psycopg_url()))
    seed_offline_dialogue_if_needed()
    global _storage_bridge
    _, session_factory = create_engine_and_sessionmaker()
    repo = AccountRepository(session_factory) if session_factory else None
    redis_url = os.environ.get("SHARK_REDIS_URL", "").strip()
    redis_client = await create_redis(redis_url) if redis_url else None
    _storage_bridge = PersistenceBridge(repository=repo, redis_client=redis_client)
    if repo:
        _log.info("Postgres persistence enabled (orders/trades/balance_logs)")
    if redis_client:
        _log.info("Redis enabled (state cache + gate REST rate limit)")
    port = int(os.environ.get("SHARK_HTTP_PORT", "80"))
    feed = MarketDataFeed()
    runner = StrategyRunner(initial_balance=200.0, persistence=_storage_bridge)
    _state["initial_capital"] = runner._initial_capital
    _state["free_cash"] = runner.balance
    _state["balance"] = runner.balance
    _state["equity"] = runner.equity

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)

    print(f"\U0001f988 Shark 2.0 启动成功 :{port}", flush=True)
    
    # 多交易所价格聚合
    multi_feed_task = None
    if MULTI_ENABLED:
        from multi_exchange import init_multi_feed, get_multi_feed
        
        async def multi_loop():
            feed_m = get_multi_feed()
            while True:
                try:
                    await feed_m.refresh()
                except Exception as e:
                    _log.debug("multi_feed refresh: %s", e)
                await asyncio.sleep(5)
        
        await init_multi_feed()
        multi_feed_task = asyncio.create_task(multi_loop())
        print("[多交易所] 价格聚合已启动 (Binance/Bybit/OKX/Gate)", flush=True)
    
    await asyncio.gather(
        server.serve(),
        trading_loop(feed, runner),
        price_feed_loop(feed, runner, interval=1),
        dialogue_ammo_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
