#!/usr/bin/env python3
"""Shark 2.0 — 真实模拟量化交易机器人。手续费、滑点、资金费率、合约最大杠杆全部实盘规格。"""

import asyncio, os, random, sys, time, json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import aiohttp

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
try:
    from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
except ImportError: pass

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

# ═══════════════════════════════════════════════════════════════════════
# 手续费 / 滑点 / 真实参数
# ═══════════════════════════════════════════════════════════════════════
TAKER_FEE = 0.0005        # Gate.io taker 费率 0.05%
MAKER_FEE = 0.0002        # Gate.io maker 费率 0.02%
SLIPPAGE_MAX = 0.0003     # 最大滑点 0.03%
COOLDOWN_SEC = 10         # 同币对冷却 10s
TRADE_INTERVAL = 1        # 交易循环间隔 1s（200ms盘口匹配）
TP_PCT = 6.0              # 止盈 6%
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
        except: continue

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
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

app = FastAPI(title="Shark 2.0")
_state = {"equity": 100.0, "balance": 100.0, "realized_pnl": 0.0, "win_rate": 0.0,
          "positions": 0, "safety_blocked": False, "symbols": [], "trades": 0, "wins": 0,
          "position_list": [], "total_fees": 0.0, "total_slippage": 0.0, "margin_locked": 0.0}

@app.get("/api/health")
async def health(): return {"ok": True}

@app.get("/api/status")
async def status(): return _state

@app.get("/api/history")
async def trade_history(offset: int = 0, limit: int = 50):
    trades = _state.get("trade_history", [])
    total = len(trades)
    page = list(reversed(trades))[offset:offset + limit]
    return {"trades": page, "total": total, "offset": offset, "limit": limit}

@app.websocket("/ws")
async def ws(ws: WebSocket):
    await ws.accept()
    while True:
        try:
            await ws.send_json(_state)
            await asyncio.sleep(1)
        except: break

@app.get("/", response_class=HTMLResponse)
async def index():
    react_index = ROOT / "web" / "dist" / "index.html"
    if react_index.exists():
        return HTMLResponse(react_index.read_text())
    return HTMLResponse(DASHBOARD)
# Mount React static assets if available
_react_dist = ROOT / "web" / "dist"
if _react_dist.exists() and (_react_dist / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(_react_dist / "assets")), name="react_assets")

# Mount public static assets (background images)
_public_dir = ROOT / "web" / "public"
if _public_dir.exists():
    app.mount("/public", StaticFiles(directory=str(_public_dir)), name="public")

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
<div class="card"><h2>Symbols</h2><div class="val mid">${d.symbols||0}</div></div>`;
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


# ═══════════════════════════════════════════════════════════════════════
class StrategyRunner:
    def __init__(self, initial_balance=250.0):
        self.balance = initial_balance
        self.equity = initial_balance
        self.positions: Dict[str, dict] = {}
        self.realized_pnl = 0.0
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
        except Exception as e:
            pass

    def update_contracts(self, specs: Dict[str, ContractSpec]):
        self._contract_specs = specs

    async def tick(self, prices: Dict[str, float], volumes: Dict[str, float],
                   changes: Dict[str, float], funding_rates: Dict[str, float],
                   mark_prices: Dict[str, float] = None):
        now = time.time()

        # 清理过期冷却
        self._cooldowns = {k: v for k, v in self._cooldowns.items() if now < v}

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
            
            # 波动率（24h变化% → 动态止损宽度）
            vol_chg = abs(pos.get("vol_chg", 3.0))
            # 止损：稳定币宽、波动币紧
            sl_base = cfg.get("stop_loss_base", -6.0)
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
                            add_s = (add_m * pos["leverage"]) / max(px, 1e-9)
                            # 考虑quanto
                            spec = self._contract_specs.get(sym)
                            if spec:
                                quanto = spec.quanto_multiplier
                                add_s = add_s / max(quanto, 1e-9) if quanto < 1 else add_s
                            pos["margin"] += add_m
                            pos["size"] += add_s
                            pos["entry"] = (pos["entry"] * (pos["size"] - add_s) + px * add_s) / pos["size"]
                            self._osc_avg_count[sym] = avg_count + 1
                            self.balance -= add_m
                            self.trades += 1
                            print(f"[补仓] {sym} {reason} 均价→{pos['entry']:.4f} 余{self._osc_avg_count[sym]}/2")

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
                        self._close_position(sym, px, f"AI止损{ai_sl:.2f}", pnl_pct)
                        continue
                    elif not sl_valid and sl_hit:
                        # 止损价在盈利方向 → 当作止盈触发
                        self._close_position(sym, px, f"AI目标{ai_sl:.2f}", pnl_pct)
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
                                add_s = (add_m * pos["leverage"]) / max(px, 1e-9)
                                pos["margin"] += add_m
                                pos["size"] += add_s
                                pos["entry"] = (pos["entry"] * (pos["size"] - add_s) + px * add_s) / pos["size"]
                                pos["defense_used"] = True
                                self.trades += 1
                                print(f"[AI防守] {sym} 缩量补仓 {add_m:.2f}@ {px:.4f}")
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
                        harvest_pnl = harvest_size * (px - pos["entry"]) if pside == "long" else harvest_size * (pos["entry"] - px)
                        # 扣 Maker 手续费
                        fee_r = self._get_maker_fee(sym)
                        harvest_fee = harvest_size * px * fee_r
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
                        
                        print(f"[AI收割] {sym} 平{harvest_ratio*100:.0f}%落袋 ${net_harvest:+.4f}")
                        
                        # 步骤B：用利润作最大回撤额度加仓
                        add_margin = min(net_harvest * 2, pos["margin"] * 0.5, self.balance * 0.05)
                        if add_margin >= 0.3 and self.balance > add_margin:
                            add_size = (add_margin * pos["leverage"]) / max(px, 1e-9)
                            pos["margin"] += add_margin
                            pos["size"] += add_size
                            pos["entry"] = (pos["entry"] * (pos["size"] - add_size) + px * add_size) / pos["size"]
                            pos["pyramid_count"] = pos.get("pyramid_count", 0) + 1
                            self.trades += 1
                            self.balance -= add_margin * fee_r  # 开仓费
                            self.total_fees += add_margin * fee_r
                            
                            # 全局止损移至初始开仓价（保本）
                            pos["trailing_stop"] = pos.get("ai_entry", pos["entry"])
                            pos[layer_key] = True
                            print(f"[AI加仓] {sym} 用利润${net_harvest:.4f} 加仓${add_margin:.2f} @{px:.4f} 止损→保本")
                    elif act_type == "take_profit":
                        if ratio >= 0.8:  # 终极止盈 → 全平
                            fee_r = self._get_maker_fee(sym)
                            est_fee = pos["size"] * px * fee_r * 2
                            net_pnl = pos["margin"] * pnl_pct / 100 - est_fee
                            if net_pnl > 0.03:
                                self._close_position(sym, px, f"AI终极止盈{tp:.2f}", pnl_pct)
                                continue
                        elif ratio > 0 and pnl_pct > 1.0:  # 部分止盈
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
                        # 只有确实盈利才平仓
                        if pnl_pct > 1.0:  # 至少1%盈利
                            self._close_position(sym, px, f"AI目标{act['price']:.2f}", pnl_pct)
                            break
                    elif act["type"] == "pyramid_add" and pos.get("pyramid_count", 0) < 3:
                        add_m = pos["margin"] * 0.5
                        if add_m >= 0.5 and self.balance > add_m:
                            add_s = (add_m * pos["leverage"]) / max(px, 1e-9)
                            pos["margin"] += add_m
                            pos["size"] += add_s
                            pos["pyramid_count"] = pos.get("pyramid_count", 0) + 1
                            self.trades += 1
                            # 加仓不单独扣费，费用已含在开仓中
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
                        add_size = (add_margin * pos["leverage"]) / max(px, 1e-9)
                        pos["margin"] += add_margin
                        pos["size"] += add_size
                        pos["pyramid_count"] = pos.get("pyramid_count", 0) + 1
                        pos["entry"] = (pos["entry"] * (pos["size"] - add_size) + px * add_size) / pos["size"]
                        self.trades += 1

            # 移动止盈：从最高点回撤，覆盖手续费
            if best_pnl > trail_trigger:
                trail_pct = abs(dyn_sl) * trail_ratio
                if pnl_pct < best_pnl - trail_pct and pnl_pct > 0:
                    fee_r = self._get_maker_fee(sym)
                    est_fee = pos["size"] * px * fee_r * 3
                    net_pnl = pos["margin"] * pnl_pct / 100 - est_fee
                    if net_pnl > max(0.03, est_fee):
                        self._close_position(sym, px, "移动止盈", pnl_pct)
                        continue

            # 固定止盈（兜底）：净利必须超过手续费 3 倍
            if pnl_pct >= vol_chg * 1.5:
                fee_r = self._get_maker_fee(sym)
                est_fee = pos["size"] * px * fee_r * 3  # 双边费的 3 倍
                net_pnl = pos["margin"] * pnl_pct / 100 - est_fee
                if net_pnl > max(0.03, est_fee):  # 净利 > 5倍手续费
                    self._close_position(sym, px, "止盈", pnl_pct)
                    continue

            # 动态止损
            if pnl_pct <= dyn_sl:
                self._close_position(sym, px, "止损", pnl_pct)
                continue

            # 超时平仓
            if now - pos["opened"] > TIMEOUT_SEC:
                self._close_position(sym, px, "超时", pnl_pct)

        # 计算当前总风险敞口
        total_margin = sum(p["margin"] for p in self.positions.values())
        # 可用 = 余额 - 已锁定保证金
        available = self.balance - total_margin
        if available <= 0:
            self._recalc_equity(prices)
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

        opened = 0
        for sym, score, vol, chg_abs in scored:
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
            max_lev = spec.leverage_max if spec else 50

            # 杠杆：合约最大 * 波动衰减（平滑连续）
            # chg_abs=1% → full leverage, chg_abs=50% → 30% of max
            lev_factor = max(0.25, 1.0 / (1 + chg_abs / 25))
            lev = max(1, int(max_lev * lev_factor))

            # 保证金：纯波动率驱动（不再用固定比例）
            # 低波大仓、高波小仓，范围 $0.50 ~ $3.00
            vol_factor = max(0.3, min(2.0, 2.0 / (1 + chg_abs / 10)))
            base_margin = 1.0  # 基础保证金 $1
            margin = base_margin * vol_factor
            if margin < 0.5: margin = 0.5
            if margin > 3.0: margin = 3.0

            # 资金上限检查
            cap = get_capital_limit(self.balance, sym)
            if total_margin >= cap:
                continue

            # 检查最小下单量（考虑 quanto_multiplier）
            quanto = spec.quanto_multiplier if spec else 1.0
            size = (margin * lev) / max(quanto * px, 1e-9)
            if spec and size < spec.order_size_min:
                size = spec.order_size_min
                margin = (size * quanto * px) / lev

            # 最小下单量调整后跳过保证金过大的单子
            # 主流币不限制（BTC/ETH 高价值需要大保证金）
            if not is_stable(sym) and margin > 2.0:
                continue

            # Maker 手续费
            fee_rate_maker = abs(spec.maker_fee) if spec and spec.maker_fee < 0 else TAKER_FEE * 0.2
            fee = size * quanto * px * fee_rate_maker

            # 余额不够就跳过
            if margin + fee > self.balance:
                continue

            entry_price = px  # Maker 单无滑点

            # 策略类型持仓限制
            cfg = get_config(sym)
            max_pos = cfg.get("max_positions", 0)
            if max_pos > 0:
                same_type = sum(1 for s in self.positions if is_stable(s) == is_stable(sym))
                if same_type >= max_pos:
                    continue

            # 方向信号：AI 多维度优先 > 震荡检测 > 费率
            funding = funding_rates.get(sym, 0)
            mark = mark_prices.get(sym, 0) if mark_prices else 0
            price_div = (px - mark) / mark * 100 if mark > 0 else 0
            
            # ── 层级1：AI 信号缓存（最高优先级） ──
            ai_cache = self._ai_signal_cache.get(sym)
            ai_direction = None
            ai_confidence = 0
            if ai_cache and now - ai_cache.get("ts", 0) < 120:
                ai_plan = ai_cache.get("plan", {})
                ai_direction = (ai_plan.get("direction") or "").upper()
                ai_confidence = ai_plan.get("confidence", 0)
            
            # ── 层级2：震荡检测器 ──
            osc_side = None
            if self._oscillator and not ai_direction:
                osc_side, osc_conf, osc_reason = self._oscillator.get_oscillation_signal(
                    sym, px, funding)
                if osc_side:
                    signal_src = f"震荡{osc_reason} 信{osc_conf}"
            
            if ai_direction and ai_confidence >= 45:
                # AI 说了算
                side = "long" if ai_direction == "LONG" else "short"
                signal_src = f"AI多维 信{ai_confidence}"
            elif osc_side:
                side = osc_side
            else:
                # ── 层级3：费率/趋势兜底 ──
                if is_stable(sym):
                    # 主流币：顺势（费率正→做多，负→做空），无费率看涨跌
                    if funding > 0:
                        side, signal_src = "long", f"主流费率{funding*100:+.3f}%"
                    elif funding < 0:
                        side, signal_src = "short", f"主流费率{funding*100:+.3f}%"
                    else:
                        side, signal_src = "long" if change >= 0 else "short", "主流趋势"
                elif funding != 0:
                    side = "short" if funding > 0 else "long"
                    signal_src = f"费率{funding*100:+.3f}%"
                else:
                    side = "short" if change >= 0 else "long"
                    signal_src = "均值回归"

            self.positions[sym] = {
                "side": side, "entry": entry_price, "size": size,
                "leverage": lev, "margin": margin, "opened": now,
                "fee_open": fee, "vol_chg": chg_abs,
                "best_pnl": -999, "pyramid_count": 0,
                "ai_targets": None,
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
            
            # 所有检查通过，扣费开仓
            self.balance -= fee
            self.total_fees += fee
            
            self._log.append(msg)
            print(msg)

            opened += 1

        self._recalc_equity(prices)
        self._update_state(prices)

    def _close_position(self, sym, px, reason, pnl_pct):
        pos = self.positions.pop(sym)
        self._osc_avg_count.pop(sym, None)  # 清补仓计数
        realized = pos["margin"] * pnl_pct / 100

        # Maker 平仓费（返佣）
        spec = self._contract_specs.get(sym)
        fee_rate_maker = abs(spec.maker_fee) if spec and spec.maker_fee < 0 else TAKER_FEE * 0.2
        fee_close = pos["size"] * px * fee_rate_maker
        realized -= fee_close
        self.total_fees += fee_close
        print(f"[DEBUG费用] 平仓扣费 fee={fee_close:.6f} balance={self.balance:.2f} total_fees={self.total_fees:.4f}")

        # Maker 无滑点
        slip_close = 0

        self.balance += realized
        self.realized_pnl += realized
        self.closed_trades += 1
        if realized > 0: self.wins += 1

        # 记录到交易历史
        self._trade_history.append({
            "symbol": sym, "side": pos["side"],
            "entry_price": pos["entry"], "exit_price": px,
            "size": pos["size"], "leverage": pos["leverage"],
            "margin": pos["margin"], "realized_pnl": realized,
            "pnl_pct": pnl_pct, "reason": reason,
            "fee_open": pos.get("fee_open", 0),
            "fee_close": pos["size"] * px * (spec.taker_fee if spec else TAKER_FEE),
            "opened_at": pos["opened"], "closed_at": time.time(),
        })

        msg = f"[平仓] {sym} {reason} 盈亏={realized:+.4f} ({pnl_pct:+.1f}%) 余额={self.balance:.2f} 累计手续费={self.total_fees:.4f}"
        self._log.append(msg)
        print(msg)

        # 平仓后冷却，避免立即重开（止损更长）
        cooldown_sec = 120 if reason == "止损" else 30
        self._cooldowns[sym] = time.time() + cooldown_sec

    def _recalc_equity(self, prices):
        unrealized = 0
        for sym, pos in self.positions.items():
            px = prices.get(sym, pos["entry"])
            if pos["side"] == "long":
                unrealized += pos["size"] * (px - pos["entry"])
            else:
                unrealized += pos["size"] * (pos["entry"] - px)
        self.equity = self.balance + unrealized

    def _update_state(self, prices):
        _state["equity"] = self.equity
        _state["balance"] = self.balance
        _state["realized_pnl"] = self.realized_pnl
        _state["win_rate"] = self.wins / max(self.closed_trades, 1)  # 基于已平仓
        _state["positions"] = len(self.positions)
        _state["trades"] = self.trades
        _state["wins"] = self.wins
        _state["symbols"] = len(prices)
        _state["total_fees"] = self.total_fees
        _state["total_slippage"] = self.total_slippage
        _state["trade_history"] = self._trade_history[-200:]
        # 锁定保证金 = 权益 - 余额（近似，因为 unrealized PnL 影响）
        _state["margin_locked"] = sum(p["margin"] for p in self.positions.values())


# ═══════════════════════════════════════════════════════════════════════
# 价格推送循环
# ═══════════════════════════════════════════════════════════════════════
async def price_feed_loop(feed: MarketDataFeed, runner: StrategyRunner, interval: int = 2):
    _state["live_prices"] = {}
    while True:
        try:
            symbols = _state.get("symbols", [])
            if symbols:
                await feed.refresh(symbols)
                prices = feed.get_prices()
                changes = feed.get_changes()
                _state["live_prices"] = {
                    sym: {"price": px, "change": changes.get(sym, 0)}
                    for sym, px in prices.items()
                }
                # 实时更新持仓盈亏（每 2s）
                pos_list = []
                for sym, pos in runner.positions.items():
                    px = prices.get(sym, pos["entry"])
                    if pos["side"] == "long":
                        unrealized = pos["size"] * (px - pos["entry"])
                        pnl_pct = (px - pos["entry"]) / pos["entry"] * pos["leverage"] * 100
                    else:
                        unrealized = pos["size"] * (pos["entry"] - px)
                        pnl_pct = (pos["entry"] - px) / pos["entry"] * pos["leverage"] * 100
                    pos_list.append({
                        "symbol": sym, "side": pos["side"],
                        "size": pos["size"], "entry_price": pos["entry"],
                        "leverage": pos["leverage"], "margin": pos["margin"],
                        "unrealized_pnl": unrealized, "pnl_pct": pnl_pct,
                        "current_price": px,
                    })
                _state["position_list"] = pos_list
        except Exception as e:
            print(f"[价格推送错误] {e}")
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
        print(f"📡 合约规格加载完成: {len(specs)} 个合约")
    except Exception as e:
        print(f"[警告] 合约规格获取失败: {e}，使用默认值")

    while True:
        try:
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
            prices = feed.get_prices()
            volumes = {s: t.volume_24h for s, t in feed._cache.items()}
            changes = feed.get_changes()
            funding_rates = feed.get_funding_rates()
            mark_prices = feed.get_mark_prices()
            await runner.tick(prices, volumes, changes, funding_rates, mark_prices)
            _state["symbols"] = list(symbols)

            # 填充仓位列表
            pos_list = []
            for sym, pos in runner.positions.items():
                px = prices.get(sym, pos["entry"])
                if pos["side"] == "long":
                    unrealized = pos["size"] * (px - pos["entry"])
                    pnl_pct = (px - pos["entry"]) / pos["entry"] * pos["leverage"] * 100
                else:
                    unrealized = pos["size"] * (pos["entry"] - px)
                    pnl_pct = (pos["entry"] - px) / pos["entry"] * pos["leverage"] * 100
                pos_list.append({
                    "symbol": sym, "side": pos["side"],
                    "size": pos["size"], "entry_price": pos["entry"],
                    "leverage": pos["leverage"], "margin": pos["margin"],
                    "unrealized_pnl": unrealized, "pnl_pct": pnl_pct,
                })
            _state["position_list"] = pos_list
        except Exception as e:
            print(f"[错误] 交易循环: {e}")
        await asyncio.sleep(interval)


# ═══════════════════════════════════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════════════════════════════════
async def main():
    port = int(os.environ.get("SHARK_HTTP_PORT", "80"))
    feed = MarketDataFeed()
    runner = StrategyRunner(initial_balance=250.0)

    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)

    print(f"\U0001f988 Shark 2.0 启动成功 :{port}")
    price_feed = MarketDataFeed()
    await asyncio.gather(
        server.serve(),
        trading_loop(feed, runner),
        price_feed_loop(price_feed, runner, interval=1),
    )


if __name__ == "__main__":
    asyncio.run(main())
