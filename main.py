#!/usr/bin/env python3
"""Shark 2.0 — 真实模拟量化交易机器人。手续费、滑点、资金费率、合约最大杠杆全部实盘规格。"""

import asyncio, os, sys, time, json, secrets, uuid, math, random
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import aiohttp
from fastapi import Request

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

# 开仓方向：plan = 仅 Redis RangePlan（默认，与 SlowLoop 一致）；ai = DeepSeek 预取缓存
SHARK_SIGNAL_SOURCE = os.environ.get("SHARK_SIGNAL_SOURCE", "plan").strip().lower()

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
    from online_learner import OnlineLearner, FeatureExtractor, compute_reward
    from live_engine import LiveEngine, create_live_engine
    from signal_engine import SignalEngine
    KLINE_ENABLED = True
except ImportError:
    KLINE_ENABLED = False

# 多交易所价格聚合
# ═══════════════════════════════════════════════════════════════════════
# 手续费 / 滑点 / 真实参数
# ═══════════════════════════════════════════════════════════════════════
TAKER_FEE = 0.0005        # Gate.io taker 费率 0.05%
MAKER_FEE = 0.0002        # Gate.io maker 费率 0.02%
SLIPPAGE_MAX = 0.0003     # 最大滑点 0.03%
# 连续止损熔断：第 4 笔止损触发（streak>3）；新开单冷却阶梯；非止损平仓重置档位
_FUSE_SL_STREAK_LIMIT = 3
_FUSE_COOLDOWNS_SEC = (60.0, 180.0, 300.0, 86400.0)
TRADE_INTERVAL = 1        # 交易循环间隔 1s（200ms盘口匹配）
# TP_PCT = 2.0              # 已废弃：止盈改为 ATR 动态计算
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
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        _log.warning(
                            "MarketDataFeed: tickers HTTP %s, keep previous cache",
                            resp.status,
                        )
                        return
                    # 部分 CDN/错误页 Content-Type 非 json，仍尝试解析
                    data = await resp.json(content_type=None)
        except asyncio.TimeoutError:
            _log.warning("MarketDataFeed: tickers timeout, keep previous cache")
            return
        except aiohttp.ClientError as e:
            _log.warning("MarketDataFeed: tickers client error %s, keep previous cache", e)
            return
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            _log.warning("MarketDataFeed: tickers invalid JSON %s, keep previous cache", e)
            return
        if not isinstance(data, list):
            _log.warning(
                "MarketDataFeed: tickers payload is %s not list, keep previous cache",
                type(data).__name__,
            )
            return
        tickers = {}
        for t in data:
            if not isinstance(t, dict):
                continue
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
from observability.device_lock import (
    DeviceLockMiddleware,
    bootstrap_client_mac_js_value,
    init_device_lock,
    websocket_connection_allowed,
)

app = FastAPI(title="Shark 2.0")
init_device_lock(ROOT)
app.add_middleware(RequestIdMiddleware)
app.add_middleware(DeviceLockMiddleware)
_state = {"equity": 100.0, "balance": 100.0, "free_cash": 100.0, "initial_capital": 100.0,
          "unrealized_pnl": 0.0, "realized_pnl": 0.0, "win_rate": 0.0,
          "positions": 0, "safety_blocked": False, "symbols": [], "symbol_count": 0, "trades": 0, "wins": 0,
          "position_list": [], "trade_history": [], "total_fees": 0.0, "total_slippage": 0.0, "margin_locked": 0.0,
          "paper_trading": False, "live_trading": False, "shark_mode": "paper"}


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
    mac = bootstrap_client_mac_js_value()
    lines = [
        "window.__SHARK_API_TOKEN__=%s;" % json.dumps(exp or ""),
        "window.__SHARK_CLIENT_MAC__=%s;\n" % json.dumps(mac or ""),
    ]
    body = "\n".join(lines)
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


@app.get("/api/evo/pending")
async def evo_pending(_: None = Depends(require_api_token)):
    """待审批的进化修改列表"""
    return {"changes": _state.get("evo_pending", [])}

@app.post("/api/evo/approve/{change_id}")
async def evo_approve(change_id: int, _: None = Depends(require_api_token)):
    """审批通过一个进化修改"""
    pending = _state.get("evo_pending", [])
    change = next((c for c in pending if c.get("id") == change_id), None)
    if not change:
        return {"error": f"修改 #{change_id} 不存在"}
    # 应用修改（实际调用在 StrategyRunner 中）
    _state["evo_apply"] = change
    _state["evo_pending"] = [c for c in pending if c.get("id") != change_id]
    # 审批后冷却：直接写入 _state 消除竞态
    cd = _state.setdefault("evo_cooldowns", {})
    cd[change["type"]] = time.time() + 300
    # 同时保留队列给 tick 同步到 runner._evo_cooldown_types
    _state.setdefault("evo_cooldown_queue", []).append({
        "type": change["type"], "until": time.time() + 300
    })
    return {"ok": True, "applied": change["type"], "id": change_id}

@app.post("/api/evo/reject/{change_id}")
async def evo_reject(change_id: int, _: None = Depends(require_api_token)):
    """拒绝一个进化修改"""
    pending = _state.get("evo_pending", [])
    rejected = next((c for c in pending if c.get("id") == change_id), None)
    if not rejected:
        return {"error": f"修改 #{change_id} 不存在"}
    _state["evo_pending"] = [c for c in pending if c.get("id") != change_id]
    # 记录被拒绝的类型供冷却
    cd = _state.setdefault("evo_cooldowns", {})
    cd[rejected["type"]] = time.time() + 300
    print(f"[进化审批] 已拒绝 #{change_id} ({rejected['type']})，冷却5分钟", flush=True)
    _state.setdefault("evo_cooldown_queue", []).append({
        "type": rejected["type"], "until": time.time() + 300
    })
    return {"ok": True, "rejected": change_id}


@app.get("/api/evo/metrics")
async def evo_metrics(_: None = Depends(require_api_token)):
    """进化层奖励分解：优先读 C++ evolver 写入的 shark:evo:metrics；无 Redis 时用当前看板 trade_history 本地计算。"""
    global _storage_bridge
    if _storage_bridge and _storage_bridge.redis:
        try:
            raw = await _storage_bridge.redis.get("shark:evo:metrics")
            if raw:
                s = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
                return json.loads(s)
        except Exception as e:
            _log.debug("evo_metrics redis: %s", e)
    from evolution.reward_signal import compute_reward_breakdown

    ic = float(_state.get("initial_capital") or 200.0)
    return compute_reward_breakdown(_state.get("trade_history") or [], initial_equity=ic)


@app.get("/api/status")
async def status(_: None = Depends(require_api_token)): return _state

@app.get("/api/live/status")
async def live_status(_: None = Depends(require_api_token)):
    """实盘状态"""
    live_data = _state.get("live", {})
    if not live_data.get("active"):
        return {"active": False, "trading_enabled": False}
    return live_data

@app.post("/api/live/toggle")
async def live_toggle(_: None = Depends(require_api_token)):
    """切换实盘交易开关"""
    live_data = _state.get("live", {})
    if not live_data.get("active"):
        return {"error": "实盘引擎未激活"}
    new_val = not _state.get("live_trading", False)
    _state["live_trading"] = new_val
    _state["live"]["trading_enabled"] = new_val
    if not new_val:
        # 停止交易 → 标记平掉所有持仓
        _state["live_close_all"] = True
    return {"trading_enabled": new_val}

@app.get("/api/paper/status")
async def paper_status(_: None = Depends(require_api_token)):
    """模拟盘状态"""
    return {"active": True, "trading_enabled": _state.get("paper_trading", False)}

@app.post("/api/paper/toggle")
async def paper_toggle(_: None = Depends(require_api_token)):
    """切换模拟盘交易开关"""
    new_val = not _state.get("paper_trading", False)
    _state["paper_trading"] = new_val
    if not new_val:
        _state["paper_close_all"] = True
    return {"trading_enabled": new_val}

@app.post("/api/shark/mode")
async def set_shark_mode(request: Request, _: None = Depends(require_api_token)):
    """切换模拟盘/实盘模式"""
    try:
        body = await request.json()
        new_mode = str(body.get("mode", "")).strip().lower()
    except Exception:
        return {"error": "请提供 {\"mode\": \"paper\"|\"live\"}"}
    if new_mode not in ("paper", "live"):
        return {"error": "mode 必须是 paper 或 live"}
    # 通过 _state 通知 tick 循环切换模式 + 标志位即时生效
    _state["shark_mode"] = new_mode
    _state["switch_mode_request"] = new_mode
    if new_mode == "live":
        if "live" not in _state:
            _state["live"] = {"active": False, "trading_enabled": False}
        _state["live"]["active"] = True
        _state["paper_trading"] = False
    else:
        _state["paper_trading"] = False
        _state["live_trading"] = False
        if "live" in _state:
            _state["live"]["active"] = False
    return {"ok": True, "mode": new_mode}

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

@app.get("/api/plans")
async def plans_dashboard():
    """计划看板：直接读取 Redis 中所有 RangePlan + 熔断状态"""
    pg = _state.get("_plan_gate")
    fuse_info = None
    plans = {}

    if pg and pg.is_fused:
        fuse_info = {
            "triggered": True,
            "remaining": pg.fuse_remaining,
            "reason": pg.fuse_reason,
        }

    # 直接从 Redis 读取所有计划
    redis_client = _state.get("_redis_client")
    if redis_client:
        try:
            async for key in redis_client.scan_iter(match="shark:plan:*", count=100):
                sym = key.replace("shark:plan:", "")
                raw = await redis_client.get(key)
                if raw:
                    plan = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
                    plans[sym] = plan
        except Exception:
            pass

    return {"plans": plans, "fuse": fuse_info, "_plan_count": len(plans)}

@app.websocket("/ws")
async def ws(
    websocket: WebSocket,
    token: Optional[str] = Query(default=None),
    device_mac: Optional[str] = Query(default=None),
):
    scope = websocket.scope
    _client = scope.get("client")
    _host = _client[0] if _client else ""
    _hdrs = list(scope.get("headers") or [])
    _ws_ok, _ws_reason = websocket_connection_allowed(_host, _hdrs, device_mac)
    if not _ws_ok:
        _log.warning("[device_lock] ws 拒绝 %s", _ws_reason)
        await websocket.close(code=1008)
        return
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

_PLAN_PANEL_SCRIPT = """
<script>
(function(){var p;function panel(){
if(p)return p;p=document.createElement('div');p.id='plan-panel'
p.style.cssText='position:fixed;bottom:12px;right:12px;z-index:99999;background:rgba(10,10,30,0.92);border:1px solid rgba(0,255,200,0.3);border-radius:10px;padding:10px 14px;font:11px/1.5 monospace;color:#0f8;min-width:220px;max-height:280px;overflow-y:auto;backdrop-filter:blur(8px);'
document.body.appendChild(p);return p}
function f(n){var x=Number(n);if(!Number.isFinite(x))return '--';var ax=Math.abs(x);if(ax===0)return '0';if(ax>=1000)return x.toFixed(1);if(ax>=1)return x.toFixed(4);if(ax>=0.01)return x.toFixed(6);return x.toFixed(8)}
function ft(n){if(!n)return'';var m=Math.floor(n/60),s=Math.floor(n%60);return m+':'+s.toString().padStart(2,'0')}
function badge(b){return b==='long'?'<b style=color:#0f0>LONG</b>':b==='short'?'<b style=color:#f44>SHORT</b>':'<span style=color:#888>--</span>'}
function rsk(lv){return lv>=2?'<span style=color:red>⚠ </span>':lv>=1?'<span style=color:#fa0>⚡</span>':''}
function render(){
var pd=panel()
fetch('/api/plans').then(function(r){return r.json()}).then(function(d){
var plans=d.plans||{},fuse=d.fuse,ks=Object.keys(plans).sort()
var h=[]
if(ks.length===0){h.push('<div style=color:#888>等待 SlowLoop 生成计划...</div>')}
else{
var n=Math.min(ks.length,8)
h.push('<table style=width:100%;border-collapse:collapse>')
for(var i=0;i<n;i++){var sym=ks[i],p=plans[sym]
h.push('<tr><td>'+sym.replace('/USDT','')+'</td><td>'+badge(p.bias)+'</td><td>'+f(p.entry_zone_low)+'~'+f(p.entry_zone_high)+'</td><td>'+rsk(p.news_risk_level)+'</td></tr>')
h.push('<tr><td colspan=4 style=font-size:9px;color:#555>区间 '+f(p.range_low)+'~'+f(p.range_high)+' SL'+f(p.stop_loss)+' '+p.macro_regime+'</td></tr>')}
if(ks.length>n)h.push('<tr><td colspan=4 style=font-size:9px;color:#555>... 还有'+(ks.length-n)+'个计划</td></tr>')
h.push('</table>')}
if(fuse&&fuse.triggered)h.push('<div style=color:red;font-weight:bold;margin-top:4px>⛔ 熔断中 '+ft(fuse.remaining)+' '+fuse.reason+'</div>')
else h.push('<div style=font-size:9px;color:#555;margin-top:4px>'+ks.length+'个计划 · 无熔断</div>')
pd.innerHTML='<div style=font-weight:bold;color:#0ff;margin-bottom:4px>📊 RangePlan</div>'+h.join('')
}).catch(function(e){panel().innerHTML='<div style=color:#f44>📊 Plan API 错误</div>'})}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',function(){render();setInterval(render,5000)})
else{render();setInterval(render,5000)}
})()
</script>"""

def _inject_plan_panel(html: str) -> str:
    """注入计划看板浮动面板（右下角）"""
    if "</body>" in html:
        return html.replace("</body>", _PLAN_PANEL_SCRIPT + "\n</body>", 1)
    return html + _PLAN_PANEL_SCRIPT


@app.get("/", response_class=HTMLResponse)
async def index():
    react_index = ROOT / "web" / "dist" / "index.html"
    if react_index.exists():
        html = react_index.read_text()
        html = _ensure_bootstrap_script(html)
        html = _inject_plan_panel(html)
        return HTMLResponse(html)
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
        self._fuse_sl_streak: Dict[str, int] = {}      # 单币对连续止损笔数
        self._fuse_escalation: Dict[str, int] = {}    # 熔断档位 0..3，非止损平掉后清零
        self._fuse_block_until: Dict[str, float] = {}  # 该币对禁止新开仓直到该时间戳
        self._log: List[str] = []
        self._trade_history: List[dict] = []
        self._contract_specs: Dict[str, ContractSpec] = {}
        self._oscillator = OscillationDetector() if OSC_ENABLED else None
        self._osc_avg_count: Dict[str, int] = {}  # 每币对补仓次数
        self._ai_signal_cache: Dict[str, dict] = {}  # sym -> {plan, timestamp}
        self._open_timestamps: list = []  # 开仓时间戳
        self._regime_cache: Dict[str, dict] = {}  # sym → {regime, diag, cfg} 行情上下文
        self._reflector = Reflector() if KLINE_ENABLED else None  # 止损反思器
        self._learner = OnlineLearner() if KLINE_ENABLED else None  # 在线学习器
        self._signal_engine = SignalEngine()  # 信号决策引擎
        self._live = create_live_engine()  # 实盘引擎（paper模式返回None）
        self._live_trading_enabled = False  # 默认不开实盘，需前端手动开启
        self._paper_trading_enabled = False # 默认不开模拟盘，需前端手动开启
        self._warmup_ticks = 0              # 启动预热计数器
        self._warmup_done = False           # 预热完成标志
        self._pending_evo_changes = []       # 待审批的进化修改
        self._evo_cooldown_types: Dict[str, float] = {}  # type → cooldown_until
        self._evo_change_id = 0             # 修改ID计数器
        self._evo_margin_mult = 1.0         # 进化保证金倍率
        self._evo_skip_alts = False         # 进化暂停山寨
        self._evo_cooldown_bonus = 0        # 进化额外冷却
        self._persistence = persistence
        self._plan_gate = None  # FastLoop 门禁，由 main() 注入

    def switch_mode(self, mode: str) -> dict:
        """运行时切换 paper/live 模式，重新初始化实盘引擎"""
        mode = mode.strip().lower()
        if mode not in ("paper", "live"):
            return {"error": f"无效模式: {mode}"}
        if mode == "live":
            engine = create_live_engine(mode="live")
            if engine is None:
                return {"error": "实盘引擎初始化失败，请检查 GATE_API_KEY/SECRET 和网络"}
            # 保存纸盘余额
            self._paper_balance = self.balance
            self._paper_equity = self.equity
            # 清空纸盘统计
            self.realized_pnl = 0.0
            self.gross_realized = 0.0
            self.total_fees = 0.0
            self.total_slippage = 0.0
            self.wins = 0
            self.closed_trades = 0
            _state["realized_pnl"] = 0.0
            _state["gross_realized"] = 0.0
            _state["total_fees"] = 0.0
            _state["win_rate"] = 0.0
            self._live = engine
            self._live_trading_enabled = False
            try:
                self.balance = engine.get_balance()
                self._initial_capital = self.balance
                _state["initial_capital"] = self.balance
                _state["balance"] = self.balance
                _state["free_cash"] = self.balance
                _state["equity"] = self.balance
            except Exception:
                pass
            print(f"🔥 已切换到实盘模式 (余额=${self.balance:.2f})", flush=True)
        else:
            self._live = None
            self._live_trading_enabled = False
            # 恢复纸盘余额+清空实盘统计
            if hasattr(self, '_paper_balance'):
                self.balance = self._paper_balance
                self.equity = self._paper_equity
                self._initial_capital = 200.0
                self.gross_realized = 0.0
                self.total_fees = 0.0
                self.realized_pnl = 0.0
                _state["balance"] = self.balance
                _state["equity"] = self.equity
                _state["free_cash"] = self.balance
                _state["initial_capital"] = 200.0
            print(f"📋 已切换到模拟盘模式 (余额=${self.balance:.2f})", flush=True)
        return {"ok": True, "mode": mode, "balance": self.balance}

    def _get_maker_fee(self, sym: str) -> float:
        """从合约API获取实时maker费率"""
        spec = self._contract_specs.get(sym)
        if spec and spec.maker_fee < 0:
            return abs(spec.maker_fee)  # 负费率=返佣
        if spec:
            return spec.maker_fee
        return MAKER_FEE

    async def _ai_reflect(self, sym, pos, realized, pnl_pct, reason, px, local_tags):
        """AI深度诊断亏损原因 → 多维度调整策略"""
        try:
            import aiohttp
            prompt = self._reflector.build_ai_prompt(sym, pos, realized, pnl_pct, reason, px,
                                                      self._regime_cache, local_tags)
            # 优先用 DeepSeek（便宜快速）
            api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("QWEN_KEY") or os.environ.get("VOLC_KEY")
            if not api_key:
                return
            endpoint = "https://api.deepseek.com/v1/chat/completions"
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            payload = {
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3, "max_tokens": 400,
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(endpoint, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()
                    content = data["choices"][0]["message"]["content"]
            # 提取JSON
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                result = json.loads(content[start:end])
                adj = result.get("adjustments", {})
                if adj:
                    msg = self._reflector.apply_ai_adjustments(adj)
                    if msg:
                        print(f"[AI调整] {msg}", flush=True)
                    self._reflector.ai_insights.append({
                        "sym": sym, "ts": time.time(),
                        "cause": result.get("root_cause", ""),
                        "adjustments": adj,
                        "confidence": result.get("confidence", 0),
                    })
        except Exception as e:
            print(f"[AI反思] 调用失败: {e}", flush=True)

    async def _fetch_ai_plan(self, sym: str, px: float, funding: float,
                             change: float, vol: float):
        """异步获取AI多层仓位计划（无限流；由上游信号与熔断控制开仓）"""
        now = time.time()
        try:
            pack = await get_ai_targets(sym, px, change, vol, funding)
            plan = pack[0] if isinstance(pack, (list, tuple)) and pack else None
            if isinstance(plan, dict) and plan.get("targets"):
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

    def merge_evo_suggestion(self, change: dict) -> None:
        """合并 C++ evolution 建议：同 type 仅保留一条，保留 id/params 与 Redis 一致便于 approve。
        审批/拒绝后的冷却期内（5min）同类型建议直接丢弃。"""
        ctype = str(change.get("type") or "unknown")
        
        # ── 冷却期检查（内存 + _state 双检，消除 approve/reject 竞态）──
        now = time.time()
        cooldown_until = self._evo_cooldown_types.get(ctype, 0)
        state_cd = (_state.get("evo_cooldowns") or {}).get(ctype, 0)
        if state_cd > cooldown_until:
            cooldown_until = state_cd
        if now < cooldown_until:
            return  # 冷却中，丢弃
        raw_id = change.get("id")
        cid: Optional[int] = None
        if raw_id is not None:
            try:
                cid = int(raw_id)
            except (TypeError, ValueError):
                cid = None
        if cid is None:
            self._evo_change_id += 1
            cid = self._evo_change_id
        else:
            self._evo_change_id = max(self._evo_change_id, cid)
        # 操作 _state["evo_pending"]（唯一真相源），同时同步 runner 用于前端展示
        pending = _state.setdefault("evo_pending", [])
        _state["evo_pending"] = [c for c in pending if c.get("type") != ctype]
        params = change.get("params")
        if not isinstance(params, dict):
            params = {}
        created = change.get("created_at")
        try:
            created_f = float(created) if created is not None else time.time()
        except (TypeError, ValueError):
            created_f = time.time()
        _state["evo_pending"].append({
            "id": cid,
            "type": ctype,
            "description": str(change.get("description") or ""),
            "params": params,
            "created_at": created_f,
        })

    def _apply_evo_change(self, change: dict):
        """应用审批通过的进化修改"""
        ct = change.get("type", "")
        params = change.get("params", {})
        if ct == "margin_mult":
            self._evo_margin_mult = params.get("value", self._evo_margin_mult)
            print(f"[进化] 保证金倍率 → {self._evo_margin_mult}", flush=True)
        elif ct == "skip_alts":
            self._evo_skip_alts = params.get("value", self._evo_skip_alts)
            print(f"[进化] 暂停山寨 → {self._evo_skip_alts}", flush=True)
        elif ct == "cooldown_bonus":
            self._evo_cooldown_bonus = params.get("value", self._evo_cooldown_bonus)
            print(f"[进化] 额外冷却 → {self._evo_cooldown_bonus}", flush=True)
        elif ct == "ai_threshold":
            # 更新 Reflector 的 AI 阈值
            if self._reflector:
                self._reflector.ai_boost = params.get("value", self._reflector.ai_boost)
            print(f"[进化] AI阈值 → {params.get('value')}", flush=True)
        elif ct == "ga_best_params":
            # Go RL 引擎 GA 最优参数
            if "margin_pct" in params:
                self._evo_margin_mult = params["margin_pct"] / 0.02  # 转换为倍率
            if "stop_atr_mult" in params and self._reflector:
                self._reflector.stop_boost = params["stop_atr_mult"]
            if "max_drawdown_limit" in params:
                pass  # 记录但不自动应用（需人工确认）
            print(f"[进化] GA最优参数已应用 (fitness={params.get('fitness','?')})", flush=True)
        else:
            print(f"[进化] 未知类型 {ct}，跳过", flush=True)

    def _strategic_entry(self, sym: str, side: str, px: float, regime_value: str) -> float:
        """根据行情类型计算策略性入场价：趋势市回调入场，震荡市边界入场，突破市追入"""
        try:
            kc = get_kline_cache() if KLINE_ENABLED else None
            if not kc:
                return px

            highs, lows = kc.get_high_low(sym, "5m")
            closes = kc.get_close(sym, "5m")
            if len(closes) < 10:
                return px

            hh = max(highs[-20:])
            ll = min(lows[-20:])
            ema9 = kc.ema(sym, 9, "5m")
            rng = hh - ll

            if "strong_trend" in regime_value:
                if "up" in regime_value and side == "long":
                    target = ema9 if ema9 < px else px * 0.995
                    return max(target, px * 0.99)
                elif "down" in regime_value and side == "short":
                    target = ema9 if ema9 > px else px * 1.005
                    return min(target, px * 1.01)

            elif "weak_trend" in regime_value:
                if "up" in regime_value and side == "long":
                    return px * 0.997
                elif "down" in regime_value and side == "short":
                    return px * 1.003

            elif "ranging" in regime_value:
                if side == "long":
                    target = ll + rng * 0.3
                    return max(target, px * 0.985)
                else:
                    target = hh - rng * 0.3
                    return min(target, px * 1.015)

            elif "breakout" in regime_value:
                if "up" in regime_value and side == "long":
                    return min(hh * 1.002, px * 1.01)
                elif "down" in regime_value and side == "short":
                    return max(ll * 0.998, px * 0.99)

            return px
        except Exception:
            return px

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

    def _apply_stop_loss_fuse(self, sym: str, reason: str) -> None:
        """单币对：连续止损次数 > _FUSE_SL_STREAK_LIMIT 后阶梯禁止新开（60s→180s→300s→24h）；非止损平仓重置连亏与档位。"""
        r = str(reason)
        is_sl = "止损" in r and "止盈" not in r
        now = time.time()
        n_tiers = len(_FUSE_COOLDOWNS_SEC)
        if is_sl:
            st = self._fuse_sl_streak.get(sym, 0) + 1
            self._fuse_sl_streak[sym] = st
            if st > _FUSE_SL_STREAK_LIMIT:
                tier = min(self._fuse_escalation.get(sym, 0), n_tiers - 1)
                dur = _FUSE_COOLDOWNS_SEC[tier]
                self._fuse_block_until[sym] = now + dur
                self._fuse_escalation[sym] = min(tier + 1, n_tiers - 1)
                self._fuse_sl_streak[sym] = 0
                dlab = f"{dur/3600:.0f}h" if dur >= 3600 else f"{int(dur)}s"
                print(
                    f"[熔断] {sym} 连续止损>{_FUSE_SL_STREAK_LIMIT}次 → {dlab} 内禁止新开 "
                    f"(阶梯 {tier + 1}/{n_tiers})",
                    flush=True,
                )
        else:
            self._fuse_sl_streak[sym] = 0
            self._fuse_escalation[sym] = 0

    async def tick(self, prices: Dict[str, float], volumes: Dict[str, float],
                   changes: Dict[str, float], funding_rates: Dict[str, float],
                   mark_prices: Dict[str, float] = None):
        now = time.time()

        # 同步实盘/模拟盘开关
        self._live_trading_enabled = _state.get("live_trading", False)
        self._paper_trading_enabled = _state.get("paper_trading", False)

        # 处理审批通过的进化修改
        evo_apply = _state.pop("evo_apply", None)
        if evo_apply:
            self._apply_evo_change(evo_apply)

        # 处理进化冷却队列（审批/拒绝后5分钟不重复推送同类型）
        for item in list(_state.pop("evo_cooldown_queue", [])):
            self._evo_cooldown_types[item["type"]] = item["until"]

        # 处理模式切换请求（前端点击切换 paper/live）
        switch_req = _state.pop("switch_mode_request", None)
        if switch_req:
            result = self.switch_mode(switch_req)
            if "error" in result:
                print(f"[模式切换] 失败: {result['error']}", flush=True)

        # 发布价格到 Redis（Go matcher 撮合用，必须在开仓前）
        try:
            import redis as _rp
            _r = _rp.from_url(os.environ.get("SHARK_REDIS_URL", "redis://redis:6379/0"))
            for sym, px in prices.items():
                if px > 0:
                    _r.set(f"shark:price:{sym}", px, ex=10)
        except Exception:
            pass

        # 停止交易 → 平掉所有持仓
        if _state.pop("live_close_all", False):
            for sym in list(self.positions):
                px = prices.get(sym, 0)
                if px > 0:
                    self._close_position(sym, px, "手动停止", 0, prices)
            print("[实盘] 已平掉所有持仓", flush=True)
        if _state.pop("paper_close_all", False):
            for sym in list(self.positions):
                px = prices.get(sym, 0)
                if px > 0:
                    self._close_position(sym, px, "手动停止(模拟)", 0, prices)
            print("[模拟] 已平掉所有持仓", flush=True)

        # 清理已过期的单币对熔断窗口
        self._fuse_block_until = {k: v for k, v in self._fuse_block_until.items() if now < v}

        # 偶尔飙句骚话调节气氛（5%概率/tick，不在交易时触发）
        if len(self.positions) == 0 and random.random() < 0.05:
            speech = pop_line("boring")
            if speech:
                _state["character_event"] = {
                    "Event_Type": "闲聊", "Speech_Text": speech,
                    "Facial_Expression": "idle", "Emotion_Index": 5,
                }

        # ── 实盘安全 + 持仓同步 ──
        if self._live and self._live.active:
            if not self._live.is_healthy:
                return  # 熔断：暂停交易
            # 每60秒同步一次交易所持仓
            if now - self._live._last_sync > 60:
                real = self._live.sync_positions()
                # 对账：paper有但交易所没有 → 警告
                for sym in list(self.positions):
                    if sym not in real:
                        _log.warning("⚠ 对账异常: %s paper有持仓但交易所无", sym)

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
            _stop_mult = _rc.get("stop_atr_mult", 2.0)
            _tp_mult = _rc.get("tp_atr_mult", 3.0)
            
            # ── ATR 实时止损/止盈（5分钟ATR，避免1分钟噪声）──
            vol_chg = abs(pos.get("vol_chg", 3.0))
            atr_pct = 0.0
            try:
                kc = get_kline_cache() if KLINE_ENABLED else None
                if kc:
                    atr_val = kc.atr(sym, period=14, interval="5m")
                    if atr_val > 0 and px > 0:
                        atr_pct = atr_val / px * 100
            except Exception:
                pass
            if atr_pct <= 0:
                atr_pct = vol_chg * 0.3  # 日波动30% ≈ 5m ATR
            
            # 止损地板：不低于 2%（确保手续费+滑点有空间，不是噪音止损）
            sl_raw = atr_pct * _stop_mult
            sl_floor = 2.0  # 最低 2% 价格止损
            sl_raw = max(sl_raw, sl_floor)
            # 反思器额外放宽
            _sl_boost = self._reflector.stop_boost if self._reflector else 0
            dyn_sl = -(sl_raw + _sl_boost)
            dyn_sl = max(dyn_sl, -25.0)  # 上限 25%
            
            # 止盈 = ATR% × 行情倍率（不低于地板 3%）
            dyn_tp = max(atr_pct * _tp_mult, 3.0)
            
            # 移动止盈：ATR × 3 触发或最低 4%
            trail_trigger = max(atr_pct * 3.0, 4.0)
            trail_ratio = 0.3

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

            # ── ATR动态止盈（无固定值）──
            if pnl_pct >= dyn_tp and self._take_profit_net_ok(sym, pos, px):
                self._close_position(sym, px, "ATR止盈", pnl_pct, prices)
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
        # ── 开关检查：实盘/模拟盘都需手动开启 ──
        _is_live_mode = self._live and self._live.active
        if _is_live_mode and not self._live_trading_enabled:
            self._update_state(prices)
            return  # 实盘模式但开关关闭，不交易
        if not _is_live_mode and not self._paper_trading_enabled:
            self._update_state(prices)
            return  # 模拟盘模式但开关关闭，不交易

        # ── Fuse 熔断检查（1分钟波动>3% → PAUSED 5分钟）──
        if self._plan_gate:
            fuse_reason = self._plan_gate.check_fuse(prices)
            if fuse_reason:
                self._update_state(prices)
                return  # 熔断中，跳过本tick

        # ── 启动预热：等K线+行情就绪后再开仓（持仓管理不受影响）──
        _can_open = True
        if not self._warmup_done:
            self._warmup_ticks += 1
            kc = get_kline_cache() if KLINE_ENABLED else None
            detector = get_detector() if KLINE_ENABLED else None
            if self._warmup_ticks >= 5 and kc and detector:
                self._warmup_done = True
                print(f"🔥 预热完成 (tick={self._warmup_ticks})，开始交易", flush=True)
            else:
                _can_open = False  # 跳过开仓，但持仓管理继续
        scored = []
        for sym in prices:
            if now < self._fuse_block_until.get(sym, 0):
                continue
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
        # 仅 ai 模式预取 LLM；plan 模式由 SignalEngine 读 RangePlan，避免每 tick 打爆 DeepSeek
        if SHARK_SIGNAL_SOURCE == "ai" and AI_ENABLED:
            prefetch_tasks = []
            for psym, _, _, _ in scored[:35]:
                if len(prefetch_tasks) >= 4:
                    break
                prefetch_tasks.append(
                    self._fetch_ai_plan(psym, prices[psym],
                                        funding_rates.get(psym, 0),
                                        changes.get(psym, 0),
                                        volumes.get(psym, 0)))
            if prefetch_tasks:
                await asyncio.gather(*prefetch_tasks, return_exceptions=True)

        opened = 0
        for sym, score, vol, chg_abs in scored:
            # 启动预热中 → 不开新仓
            if not _can_open:
                break
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

            # ── 信号决策（委托给 SignalEngine）──
            result = self._signal_engine.decide(
                self, sym, px, funding_rates.get(sym, 0),
                change, vol, cfg, now, self._regime_cache, _regime)
            if not result.side:
                continue
            side = result.side
            signal_src = result.signal_src
            ai_confidence = result.ai_confidence
            ai_use = result.ai_use
            _learner_feat = result.learner_feat
            _stop_mult = result.stop_mult
            _tp_mult = result.tp_mult

            # 策略入场价：根据行情类型+方向智能定位
            _rv = _regime.value if _regime else "unknown"
            entry_price = self._strategic_entry(sym, side, entry_price, _rv)

            # ── FastLoop 计划门禁：无计划/走廊外/熔断/方向不匹配 → 禁止开仓 ──
            if self._plan_gate:
                can, reason = self._plan_gate.can_open(sym, side, entry_price)
                if not can:
                    continue

            # ── 统一下单通道 → Redis → Go 执行器 ──
            _live_oid = None
            _is_live = self._live and self._live.active
            mode = "live" if (_is_live and self._live_trading_enabled) else "paper"
            
            _ct_size = max(1, int(size))
            cmd = json.dumps({
                "symbol": sym, "side": side, "size": _ct_size,
                "leverage": lev, "action": "open", "mode": mode,
            })
            try:
                import redis as _redis
                _r = _redis.from_url(os.environ.get("SHARK_REDIS_URL", "redis://redis:6379/0"))
                _r.publish("shark:orders:new", cmd)
                _live_oid = True
            except Exception as e:
                _log.error("Redis publish failed: %s", e)
                if mode == "live":
                    continue  # 实盘失败必须跳过
            

            self.positions[sym] = {
                "side": side, "entry": entry_price, "size": size,
                "leverage": lev, "margin": margin, "opened": now,
                "fee_open": fee, "vol_chg": chg_abs,
                "best_pnl": -999, "pyramid_count": 0,
                "ai_targets": None,
                "order_id": uuid.uuid4(),
                "signal_src": signal_src,
                "ai_confidence": ai_confidence if ai_use else 0,
                "_learner_feat": _learner_feat,
            }
            
            # AI分析（异步，不阻塞开仓）
            if AI_ENABLED and SHARK_SIGNAL_SOURCE == "ai":
                asyncio.create_task(self._fetch_ai_plan(sym, px,
                                        funding_rates.get(sym, 0),
                                        changes.get(sym, 0), vol))

            # 实盘记录
            if self._live and self._live.active and _live_oid:
                self._live.positions[sym] = LivePosition(
                    symbol=sym, side=side, size=int(size),
                    entry_price=entry_price, leverage=lev, margin=margin,
                    order_id=str(uuid.uuid4()), opened_at=now,
                )
            self.trades += 1
            total_margin += margin

            fee_str = f" 手续费={fee:.4f}" if fee > 0.0001 else ""
            stype = "主流" if is_stable(sym) else "山寨"
            msg = f"[开仓-{stype}] {sym} {side.upper()} @ {entry_price:.4f} 保证金={margin:.2f} 杠杆={lev}x 信号={signal_src}"
            if _regime:
                msg += f" 行情={_regime.value}"
            if _stop_mult:
                msg += f" SL=ATR×{_stop_mult}"
            if _tp_mult:
                msg += f" TP=ATR×{_tp_mult}"
            
            # 所有检查通过，扣费开仓
            if self._live and self._live.active and self._live_trading_enabled and _live_oid:
                # 实盘：余额从交易所同步
                try:
                    self.balance = self._live.get_balance()
                except Exception:
                    self.balance -= margin + fee
            else:
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
        # ── 实盘平仓 → Redis 发布给 Go 执行器 ──
        _live_close_ok = True
        _live_close_px = px
        if self._live and self._live.active and self._live_trading_enabled:
            lp = self.positions.get(sym)
            if lp:
                cmd = json.dumps({
                    "symbol": sym, "side": lp["side"], "action": "close",
                    "size": int(lp.get("size", 1)),
                })
                try:
                    import redis as _redis
                    _r = _redis.from_url(os.environ.get("SHARK_REDIS_URL", "redis://redis:6379/0"))
                    _r.publish("shark:orders:new", cmd)
                except Exception as e:
                    _log.error("Redis close publish failed: %s", e)
                    _live_close_ok = False
                    _log.error("🔥 实盘平仓失败 %s, 人工介入!", sym)

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
        # 余额更新：实盘从交易所同步，纸盘本地计算
        if _live_close_ok and self._live and self._live.active and self._live_trading_enabled:
            try:
                self.balance = self._live.get_balance()
            except Exception:
                self.balance += pos["margin"] + gross - fee_close
        else:
            self.balance += pos["margin"] + gross - fee_close
        print(
            f"[DEBUG费用] 毛利={gross:.6f} 平仓费={fee_close:.6f} 净利={realized:.6f} "
            f"balance={self.balance:.2f} total_fees={self.total_fees:.4f}",
            flush=True,
        )

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
        # 发布到 Redis 供 C++ 进化引擎消费
        try:
            import redis as _redis2
            _rr = _redis2.from_url(os.environ.get("SHARK_REDIS_URL", "redis://redis:6379/0"))
            _rr.lpush("shark:trade_history", json.dumps(self._trade_history[-1]))
            _rr.ltrim("shark:trade_history", 0, 199)
        except Exception:
            pass
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

        # ── 止损反思：多维分析亏损原因 → 立即调整下笔交易参数 ──
        if self._reflector and realized < 0:
            local_tags = self._reflector.analyze(sym, pos, realized, pnl_pct, reason, px,
                                    self._regime_cache, None)
            # 本地快速调整
            adj = self._reflector.maybe_adjust()
            if adj:
                print(f"[反思调整] {adj}", flush=True)
            # AI深度诊断（异步，每60秒最多一次，避免API轰炸）
            now_ts = time.time()
            if now_ts - self._reflector._last_ai_call > 60 and (
                os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("QWEN_KEY") or os.environ.get("VOLC_KEY")
            ):
                self._reflector._last_ai_call = now_ts
                asyncio.create_task(self._ai_reflect(sym, pos, realized, pnl_pct, reason, px, local_tags))

        # ── 在线学习：Q-Learning + ES更新 ──
        if self._learner:
            feat = pos.get("_learner_feat")
            if feat:
                # 构建下一状态特征（平仓时）
                try:
                    diag_now = self._regime_cache.get(sym, {}).get("diag", {})
                    ai_cache = self._ai_signal_cache.get(sym, {})
                    funding = 0  # 当前tick的费率
                    # 简化：用开仓时的特征变换作为下一状态
                    next_feat = feat[:]  # 简化处理
                    held = pos.get("closed_at", time.time()) - pos.get("opened_at", time.time())
                    was_stop = "止损" in reason
                    rc = self._regime_cache.get(sym, {})
                    regime_val = rc.get("regime", "unknown")
                    self._learner.on_trade_closed(
                        feat, next_feat, realized, pnl_pct,
                        was_stop, held, regime_val
                    )
                except Exception:
                    pass

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

        self._apply_stop_loss_fuse(sym, reason)

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

        # 实盘状态
        if self._live:
            _state["live"] = self._live.stats()
            _state["live"]["trading_enabled"] = self._live_trading_enabled
            try:
                _state["live"]["balance"] = self._live.get_balance()
            except Exception:
                _state["live"]["balance"] = 0
            # 实盘模式：余额走交易所，但初始资金不变
            if self._live.active:
                _state["balance"] = _state["live"]["balance"]
                _state["equity"] = _state["live"]["balance"]
                _state["free_cash"] = _state["live"]["balance"]
                _state["unrealized_pnl"] = 0
                _state["margin_locked"] = sum(
                    p.get("margin", 0) for p in self._live.sync_positions().values()
                ) if self._live else 0

        # 待审批的进化修改：_state 是唯一真相源，tick从_state同步
        self._pending_evo_changes = _state.get("evo_pending", [])

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
            detail = str(e).strip() or repr(e)
            print(f"[价格推送错误] {type(e).__name__}: {detail}", flush=True)
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
    # FastLoop 门禁注入
    if redis_client:
        from execution.plan_gate import PlanGate
        # PlanGate 需要 sync Redis 客户端（async 客户端在同步上下文中不可用）
        import redis as sync_redis
        sync_rdb = sync_redis.from_url(redis_url or "redis://redis:6379/0", decode_responses=True)
        runner._plan_gate = PlanGate(sync_rdb)
        _state["_plan_gate"] = runner._plan_gate
        _state["_redis_client"] = redis_client  # 供 Plans API 直接读取（async）
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
    
    # 启动状态一览
    _shark_mode = os.environ.get("SHARK_MODE", "paper").lower()
    _paper_state = "关闭" if not _state.get("paper_trading") else "开启"
    _live_state = "关闭" if not _state.get("live_trading") else "开启"
    print(f"📋 当前模式: {_shark_mode} | 模拟盘: {_paper_state} | 实盘: {_live_state}", flush=True)
    print(f"💡 提示: 前端点击「开始交易」后才开仓", flush=True)
    
    async def hydrate_evo_pending_from_redis() -> None:
        """进程重启后从 LPUSH 列表恢复待审批项（missed pub/sub）。LRANGE 0.. 为最新优先，反向合并使同 type 保留最新。"""
        if not redis_client:
            return
        try:
            items = await redis_client.lrange("shark:evo:list", 0, 49)
        except Exception as e:
            _log.warning("hydrate shark:evo:list: %s", e)
            return
        for raw in reversed(items or []):
            try:
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", errors="replace")
                runner.merge_evo_suggestion(json.loads(raw))
            except Exception as e:
                _log.debug("hydrate evo item: %s", e)

    # C++ 进化引擎订阅：监听 shark:evo:pending → 加入待审批队列
    async def evo_subscriber():
        if not redis_client:
            return
        pubsub = redis_client.pubsub()
        await pubsub.subscribe("shark:evo:pending")
        print("[进化订阅] 已订阅 shark:evo:pending", flush=True)
        async for msg in pubsub.listen():
            if msg["type"] != "message":
                continue
            try:
                raw = msg["data"]
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", errors="replace")
                change = json.loads(raw)
                runner.merge_evo_suggestion(change)
                print(f"[进化订阅] 收到建议 #{change.get('id')}: {change.get('type')}", flush=True)
            except Exception as e:
                _log.debug("evo_subscriber parse: %s", e)

    if redis_client:
        await hydrate_evo_pending_from_redis()
    evo_task = asyncio.create_task(evo_subscriber()) if redis_client else None

    # Go RL Agent 动作订阅
    async def rl_action_subscriber():
        if not redis_client:
            return
        pubsub = redis_client.pubsub()
        await pubsub.subscribe("shark:rl:action")
        print("[RL订阅] 已订阅 shark:rl:action", flush=True)
        async for msg in pubsub.listen():
            if msg["type"] != "message":
                continue
            try:
                raw = msg["data"]
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", errors="replace")
                action = json.loads(raw)
                if not runner._paper_trading_enabled and not runner._live_trading_enabled:
                    continue  # 交易未启用，跳过
                sym = action.get("symbol", "")
                side = action.get("side", "")
                if side in ("long", "short"):
                    # 实盘走 executor，模拟盘走 matcher
                    mode = "live" if (runner._live and runner._live.active) else "paper"
                    cmd = json.dumps({"symbol": sym, "side": side, "action": "open", "mode": mode, "source": "rl-agent"})
                    await redis_client.publish("shark:orders:new", cmd)
            except Exception as e:
                _log.debug("rl_action_subscriber: %s", e)

    rl_task = asyncio.create_task(rl_action_subscriber()) if redis_client else None

    await asyncio.gather(
        server.serve(),
        trading_loop(feed, runner),
        price_feed_loop(feed, runner, interval=1),
        dialogue_ammo_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
