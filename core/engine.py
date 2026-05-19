#!/usr/bin/env python3
"""Shark 2.0 — 真实模拟量化交易机器人。手续费、滑点、资金费率、合约最大杠杆全部实盘规格。"""

import asyncio
import os
from core.config import settings
import sys
import time
import json
import math
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from market.data import MarketDataFeed, ContractSpec, fetch_contract_specs, fetch_hot_volatile_symbols
from strategy.runner import StrategyRunner
import aiohttp
import uvicorn
from fastapi.staticfiles import StaticFiles
from observability.context import configure_logging
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
_log = logging.getLogger(__name__)
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from persistence.dialogue_store import DialogueStore, resolve_sync_psycopg_url
from persistence.bridge import PersistenceBridge, create_redis
from persistence.repository import AccountRepository
from persistence.session import create_engine_and_sessionmaker
from persistence.redis_rate_limit import fixed_window_allow
from execution.order_command import build_rl_order_command

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

from character.dialogue import (
    dialogue_ammo_loop,
    seed_offline_dialogue_if_needed,
    set_dialogue_store,
)
from character.voice import character_llm_config, fetch_loli_dialogue

# 导入AI策略
from strategy.ai import get_ai_targets, apply_ai_targets
AI_ENABLED = True

# 开仓方向：plan = 仅 Redis RangePlan（默认，与 SlowLoop 一致）；ai = DeepSeek 预取缓存
SHARK_SIGNAL_SOURCE = settings.SHARK_SIGNAL_SOURCE




# 导入双轨策略
from strategy.dual import (
    get_config, is_stable, get_capital_limit, is_high_vol_alt,
    set_dynamic_high_vol_alts, trading_track, trading_track_allows_open,
)
DUAL_STRATEGY = True

# K线缓存（自进化引擎依赖）
from market.kline import KlineCache, init_kline_cache, get_kline_cache
from market.regime import RegimeDetector, REGIME_CONFIG, init_detector, get_detector
from learning.reflector import Reflector, LossReason
from learning.online import OnlineLearner
from core.live import LiveEngine, create_live_engine
KLINE_ENABLED = True

# 多交易所价格聚合
# ═══════════════════════════════════════════════════════════════════════
# 手续费 / 滑点 / 真实参数
# ═══════════════════════════════════════════════════════════════════════
TAKER_FEE = 0.0005        # Gate.io taker 费率 0.05%
MAKER_FEE = 0.0002        # Gate.io maker 费率 0.02%
SLIPPAGE_MAX = 0.0003     # 最大滑点 0.03%
# 连续止损只记录告警，不再硬暂停开仓；数量策略需要平仓后立即续单。
_FUSE_SL_STREAK_LIMIT = 3
TRADE_INTERVAL = 1        # 交易循环间隔 1s（200ms盘口匹配）
ALT_PLAN_TTL_SEC = 600    # 高波动山寨约10分钟全量刷新计划
# TP_PCT = 2.0              # 已废弃：止盈改为 ATR 动态计算
SL_PCT = -6.0             # 止损 -6%
TIMEOUT_SEC = 300         # 超时平仓 5min
MAX_TOTAL_EXPOSURE = 0.95 # 最大总风险敞口 95%

# ═══════════════════════════════════════════════════════════════════════
# 合约规格获取
# ═══════════════════════════════════════════════════════════════════════




# ═══════════════════════════════════════════════════════════════════════
# 动态交易对发现
# ═══════════════════════════════════════════════════════════════════════




# ═══════════════════════════════════════════════════════════════════════
# 行情数据
# ═══════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════
# API Server
# ═══════════════════════════════════════════════════════════════════════
from api.routes import app, get_state, _default_paper_trading_enabled

_state = {"equity": 500.0, "balance": 500.0, "free_cash": 500.0, "initial_capital": 500.0,
          "unrealized_pnl": 0.0, "realized_pnl": 0.0, "win_rate": 0.0,
          "positions": 0, "safety_blocked": False, "fuse_reason": "", "live_api_ok": True,
          "last_tick_block": None, "symbols": [], "symbol_count": 0, "trades": 0, "wins": 0,
          "position_list": [], "trade_history": [], "total_fees": 0.0, "total_slippage": 0.0, "margin_locked": 0.0,
          "paper_trading": _default_paper_trading_enabled(), "live_trading": False, "shark_mode": "paper",
          "dynamic_high_vol_alts": [],
          "planning_status": {"active": False, "phase": "idle", "message": "等待计划刷新", "done": 0, "total": 0},
          "strategy_profile": {
              "stable_capital_pct": 0.60,
              "alt_capital_pct": 0.40,
              "stable_profile": "主流中长线重仓，BTC/ETH/SOL 三仓按60%资金桶分配，严格命中计划入场带才开",
              "alt_profile": "动态热门高波动山寨，方向趋势没坏可扛，10分钟全量刷新",
              "alt_plan_ttl_sec": ALT_PLAN_TTL_SEC,
          }}









































_PLAN_PANEL_SCRIPT = """
<script>
(function(){var p;function panel(){
if(p)return p;p=document.createElement('div');p.id='plan-panel'
p.style.cssText='position:fixed;bottom:12px;right:12px;z-index:99999;background:rgba(10,10,30,0.92);border:1px solid rgba(0,255,200,0.3);border-radius:10px;padding:10px 14px;font:11px/1.5 monospace;color:#0f8;min-width:240px;max-height:300px;overflow-y:auto;backdrop-filter:blur(8px);'
document.body.appendChild(p);return p}
function f(n){var x=Number(n);if(!Number.isFinite(x)||x<=0)return'--';var ax=Math.abs(x);if(ax>=1000)return x.toFixed(1);if(ax>=1)return x.toFixed(4);if(ax>=0.01)return x.toFixed(6);return x.toFixed(8)}
function ft(n){if(!n)return'';var m=Math.floor(n/60),s=Math.floor(n%60);return m+':'+s.toString().padStart(2,'0')}
function bdg(b){return b==='long'?'<b style=color:#0f0>LONG</b>':b==='short'?'<b style=color:#f44>SHORT</b>':'<span style=color:#888>--</span>'}
function rsk(lv){return lv>=2?'<span style=color:red>⚠</span>':lv>=1?'<span style=color:#fa0>⚡</span>':''}
function render(){
var pd=panel()
fetch('/api/plans').then(function(r){return r.json()}).then(function(d){
var plans=d.plans||{},fuse=d.fuse,ks=Object.keys(plans).sort()
var h=[]
if(ks.length===0){h.push('<div style=color:#888>等待 SlowLoop 生成计划...</div>')}
else{
var n=Math.min(ks.length,6)
h.push('<table style=width:100%;border-collapse:collapse>')
for(var i=0;i<n;i++){var sym=ks[i],p=plans[sym],b=p.bias||''
h.push('<tr><td colspan=4 style=font-weight:bold;color:#0ff>'+sym.replace('/USDT','')+' '+rsk(p.news_risk_level)+'</td></tr>')
if(b==='both'){
h.push('<tr><td>'+bdg('long')+'</td><td>'+f(p.long_entry_low)+'~'+f(p.long_entry_high)+'</td><td>SL '+f(p.long_stop_loss)+'</td><td>'+p.macro_regime+'</td></tr>')
h.push('<tr><td>'+bdg('short')+'</td><td>'+f(p.short_entry_low)+'~'+f(p.short_entry_high)+'</td><td>SL '+f(p.short_stop_loss)+'</td><td></td></tr>')
}else{
h.push('<tr><td>'+bdg(b)+'</td><td>'+f(p.entry_zone_low)+'~'+f(p.entry_zone_high)+'</td><td>SL '+f(p.stop_loss)+'</td><td>'+p.macro_regime+'</td></tr>')}
// AI 信息行
if(p.ai_rationale||p.ai_model){
var ai_tag=(p.ai_model||'').toUpperCase()
var ai_conf=p.ai_confidence?Math.round(p.ai_confidence)+'%':''
var sz=p.position_size_pct?'仓位'+Math.round(p.position_size_pct*100)+'%':''
var lv=p.leverage?'杠杆'+p.leverage+'x':''
h.push('<tr style=font-size:9px;color:#4a8><td colspan=4><span style=color:#0f8>🤖 '+ai_tag+' '+ai_conf+'</span> '+(p.ai_rationale||'').substring(0,25)+' '+sz+' '+lv+'</td></tr>')}
}}
h.push('</table>')
if(ks.length>n)h.push('<div style=font-size:9px;color:#555>... 还有'+(ks.length-n)+'个计划</div>')}
if(fuse&&fuse.triggered){var ps=fuse.per_symbol||{};var fusedSyms=Object.keys(ps);if(fusedSyms.length)h.push('<div style=color:red;font-weight:bold;margin-top:4px>⛔ 熔断: '+fusedSyms.join(', ')+' ('+ft(fuse.remaining)+')</div>')}
else h.push('<div style=font-size:9px;color:#555;margin-top:4px>'+ks.length+'个计划 · 无熔断</div>')
pd.innerHTML='<div style=font-weight:bold;color:#0ff;margin-bottom:4px>📊 <a href=/plans style=color:#0ff;text-decoration:underline>RangePlan 全屏看板→</a></div>'+h.join('')
}).catch(function(e){panel().innerHTML='<div style=color:#f44>📊 Plan API 错误</div>'})}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',function(){render();setInterval(render,5000)})
else{render();setInterval(render,5000)}
})()
</script>"""

_PLANS_FULL_PAGE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>🦈 Shark RangePlan 看板</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0e17;color:#e0e0e0;font:14px/1.5 system-ui,sans-serif;padding:16px}
h1{font-size:20px;color:#00d4ff;margin-bottom:4px}
.sub{font-size:12px;color:#64748b;margin-bottom:16px}
.fuse{background:#3b0000;border:1px solid #f44;color:#f44;padding:10px 14px;border-radius:8px;margin-bottom:16px;font-weight:bold}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:12px}
.card{background:#111827;border:1px solid #1e293b;border-radius:10px;padding:14px}
.card h2{font-size:14px;color:#0ff;margin-bottom:4px}
.card .meta{font-size:11px;color:#64748b;margin-bottom:8px}
.row{display:flex;justify-content:space-between;align-items:center;font-size:12px;padding:3px 0;border-bottom:1px solid #1a1a2e}
.row .lbl{color:#64748b;white-space:nowrap}
.row .val{text-align:right;font-weight:600}
.val.long{color:#0f8}.val.short{color:#f44}.val.both{color:#fa0}
.val.good{color:#0f8}.val.warn{color:#fa0}.val.bad{color:#f44}
.ai{font-size:11px;color:#4a8;margin-top:6px;padding-top:6px;border-top:1px solid #1e293b;line-height:1.4}
.ai b{color:#0f8}
.empty{text-align:center;padding:60px 20px;color:#64748b;font-size:15px}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.dot{display:inline-block;width:6px;height:6px;border-radius:50%;margin-right:4px}
.dot.live{background:#0f8;animation:pulse 2s infinite}
.dot.paused{background:#f44}
.countdown{font-size:11px;color:#555;text-align:right}
@media(max-width:400px){.grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<h1>🦈 Shark RangePlan 看板 <span id=refresh></span></h1>
<div class=sub id=sub></div>
<div id=fuse></div>
<div class=grid id=grid><div class=empty>加载中...</div></div>
<script>
function f(n){var x=Number(n);if(!Number.isFinite(x)||x<=0)return'--';var ax=Math.abs(x);if(ax>=1000)return x.toFixed(1);if(ax>=1)return x.toFixed(4);if(ax>=0.01)return x.toFixed(6);return x.toFixed(8)}
function ft(n){if(!n)return'';var m=Math.floor(n/60),s=Math.floor(n%60);return m+':'+s.toString().padStart(2,'0')}
function bdg(b){return b==='long'?'<span class="val long">LONG</span>':b==='short'?'<span class="val short">SHORT</span>':'<span class="val both">BOTH</span>'}
function rsk(lv){return lv>=2?'<span class="val bad">⚠ 熔断级</span>':lv>=1?'<span class="val warn">⚡ 警告</span>':''}
function am(m){return m==='deepseek'?'🤖 DeepSeek':m==='qwen'?'💡 Qwen':m==='math'?'📐 数学':'--'}

function render(){
fetch('/api/plans').then(r=>r.json()).then(d=>{
var plans=d.plans||{},fuse=d.fuse,ks=Object.keys(plans).sort()
var g=document.getElementById('grid'),fs=document.getElementById('fuse')
var sub=document.getElementById('sub')
sub.textContent=ks.length+'个计划 · '+(fuse&&fuse.triggered?'⛔ 单币对熔断':'无熔断')+' · 每30分钟更新'

var ps=fuse&&fuse.per_symbol||{}
if(fuse&&fuse.triggered){
  var fusedList=Object.keys(ps).map(function(s){return s.replace('/USDT','')+'('+ps[s].reason+')'}).join(', ')
  fs.innerHTML='<div class=fuse>⛔ 熔断保护中 '+ft(fuse.remaining)+' — '+fusedList+'</div>'
}else fs.innerHTML=''

if(ks.length===0){g.innerHTML='<div class=empty>等待 SlowLoop 生成计划...</div>';return}
var h=''
for(var i=0;i<ks.length;i++){
var sym=ks[i],p=plans[sym],bias=p.bias||''
h+='<div class=card>'
h+='<h2>'+sym.replace('/USDT','')+' <span style=font-size:11px>'+bdg(bias)+'</span> '+rsk(p.news_risk_level||0)+'</h2>'
if(ps&&ps[sym])h+='<div style=background:#3b0000;color:#f44;font-size:10px;padding:2px 6px;border-radius:4px;margin-bottom:4px;display:inline-block>⛔ '+ps[sym].reason+'</div>'
h+='<div class=meta>'+am(p.ai_model)+' 置信'+(p.ai_confidence?Math.round(p.ai_confidence)+'%':'--')+' · '+ (p.macro_regime||'')+' · ATR '+f(p.atr14)+'</div>'
h+='<div class=row><span class=lbl>区间</span><span class=val>'+f(p.range_low)+' ~ '+f(p.range_high)+'</span></div>'
if(bias==='both'){
h+='<div class=row><span class=lbl>做多入场</span><span class="val long">'+f(p.long_entry_low)+' ~ '+f(p.long_entry_high)+'</span></div>'
h+='<div class=row><span class=lbl>做多SL/TP</span><span class=val>SL '+f(p.long_stop_loss)+' / TP '+(p.long_take_profit||[]).map(f).join(',')+'</span></div>'
h+='<div class=row><span class=lbl>做空入场</span><span class="val short">'+f(p.short_entry_low)+' ~ '+f(p.short_entry_high)+'</span></div>'
h+='<div class=row><span class=lbl>做空SL/TP</span><span class=val>SL '+f(p.short_stop_loss)+' / TP '+(p.short_take_profit||[]).map(f).join(',')+'</span></div>'
}else{
h+='<div class=row><span class=lbl>入场带</span><span class=val>'+f(p.entry_zone_low)+' ~ '+f(p.entry_zone_high)+'</span></div>'
h+='<div class=row><span class=lbl>SL/TP</span><span class=val>SL '+f(p.stop_loss)+' / TP '+(p.take_profit||[]).map(f).join(',')+'</span></div>'
}
if(p.position_size_pct||p.leverage){
h+='<div class=row><span class=lbl>仓位/杠杆</span><span class=val>'+(p.position_size_pct?Math.round(p.position_size_pct*100)+'%':'--')+' / '+(p.leverage?p.leverage+'x':'--')+'</span></div>'}
if(p.pyramid_prices&&p.pyramid_prices.length)h+='<div class=row><span class=lbl>补仓点</span><span class=val>'+p.pyramid_prices.map(f).join(' , ')+'</span></div>'
if(p.cut_loss_pct)h+='<div class=row><span class=lbl>割肉线</span><span class="val bad">'+Math.round(p.cut_loss_pct*100)+'%</span></div>'
h+='<div class=row><span class=lbl>费率</span><span class=val>'+(p.funding_rate?p.funding_rate.toFixed(4)+'%':'--')+'</span></div>'
if(p.ai_rationale)h+='<div class=ai><b>AI分析:</b> '+p.ai_rationale+'</div>'
h+='</div>'
}
g.innerHTML=h
}).catch(function(e){document.getElementById('grid').innerHTML='<div class=empty style=color:#f44>API 错误: '+e.message+'</div>'})}
render();setInterval(render,10000)
</script>
</body>
</html>"""



# Mount React static assets if available
_react_dist = ROOT / "web" / "dist"
if _react_dist.exists() and (_react_dist / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(_react_dist / "assets")), name="react_assets")

# Mount public static assets (background images)
_public_dir = ROOT / "web" / "public"
if _public_dir.exists():
    app.mount("/public", StaticFiles(directory=str(_public_dir)), name="public")

# Mount repo static assets (device-deny image, payment QR codes, etc.)
_static_dir = ROOT / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

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
MIN_CHANGE = 1.0          # 最小 24h 涨跌幅 1.0%
MAX_CHANGE = 35.0         # 最大 24h 涨跌幅 35%
MIN_PRICE = 0.01          # 最低价格 $0.01
MAX_POSITIONS = 0          # 0=不限制，有信号就开
MARGIN_PCT = 0.005        # 每仓保证金占权益 0.5%
MAX_MARGIN_PER_POS = 5.0  # 单仓最大保证金

# 看板娘事件序号（前端可对齐最新一条）
# character sequence state removed


# ═══════════════════════════════════════════════════════════════════════
async def price_feed_loop(feed: MarketDataFeed, runner: StrategyRunner, interval: int = 2):
    get_state()["live_prices"] = {}
    while True:
        try:
            symbols = get_state().get("symbols", [])
            if isinstance(symbols, int):
                symbols = []
            
            # P1 FIX: Ensure all holding symbols are always refreshed
            for psym in runner.positions.keys():
                if psym not in symbols:
                    symbols.append(psym)

            if symbols:
                await feed.refresh(symbols)
            prices = dict(feed.get_prices())
            # 持仓币对必须参与权益重算（否则不在本轮 watchlist 时用入场价占位）
            for psym, pos in runner.positions.items():
                if psym not in prices:
                    prices[psym] = float(pos.get("entry", 0) or 0)
            changes = feed.get_changes()
            runner._update_state(prices)
            get_state()["live_prices"] = {
                sym: {"price": px, "change": changes.get(sym, 0)}
                for sym, px in prices.items()
            }
        except Exception as e:
            detail = str(e).strip() or repr(e)
            _log.info(f"[价格推送错误] {type(e).__name__}: {detail}")
        await asyncio.sleep(interval)


# ═══════════════════════════════════════════════════════════════════════
# 主交易循环
# ═══════════════════════════════════════════════════════════════════════
async def trading_loop(feed: MarketDataFeed, runner: StrategyRunner,
                       interval: int = TRADE_INTERVAL):
    # 启动时获取合约规格
    _log.info("📡 获取合约规格...")
    try:
        specs = await fetch_contract_specs()
        runner.update_contracts(specs)
        _log.info(f"📡 合约规格加载完成: {len(specs)} 个合约")
    except Exception as e:
        _log.info(f"[警告] 合约规格获取失败: {e}，使用默认值")

    _kline_inited = False
    _tick = 0
    _cached_hot_alts: List[str] = []
    _last_alt_refresh = 0.0
    ALT_REFRESH_SEC = 600  # 山寨池每10分钟全量重扫

    while True:
        try:
            _tick += 1
            _trk = trading_track()
            now = time.time()

            # ── 山寨池刷新：每10分钟重扫Gate.io，替换高波币对 ──
            if _trk in ("dual", "volatile") and now - _last_alt_refresh >= ALT_REFRESH_SEC:
                try:
                    hot_alts = await fetch_hot_volatile_symbols(n=18)
                    set_dynamic_high_vol_alts(hot_alts, runner._plan_gate._redis if runner._plan_gate else None)
                    _cached_hot_alts = list(hot_alts)
                    _last_alt_refresh = now
                    if hot_alts:
                        _log.info(f"[山寨池] 10分钟刷新: {len(hot_alts)}个高波币 {hot_alts[:3]}...")
                except Exception as e:
                    _log.info(f"[山寨池] 刷新失败: {e}，沿用旧池")

            if _trk == "stable":
                set_dynamic_high_vol_alts([], runner._plan_gate._redis if runner._plan_gate else None)
                _cached_hot_alts = []
                symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
            elif _trk == "volatile":
                symbols = list(_cached_hot_alts)
                if not symbols:
                    if _tick % 60 == 1:
                        _log.info("[volatile] 山寨池暂空，等待刷新...")
                    await asyncio.sleep(interval)
                    continue
            else:
                symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT"] + _cached_hot_alts
            # 去重保持顺序
            seen = set()
            symbols = [s for s in symbols if not (s in seen or seen.add(s))]
            get_state()["dynamic_high_vol_alts"] = list(_cached_hot_alts)
            if _tick == 1:
                _trk_label = {"dual": "双轨(主流+山寨)", "stable": "单线·仅主流", "volatile": "单线·仅山寨池"}.get(
                    _trk, _trk
                )
                _log.info(f"🛤️ SHARK_TRADING_TRACK={_trk} → {_trk_label}")
            # 价格由 price_feed_loop 维护，trading_loop 只读缓存
            # 移到最后获取 prices，避免 await 阻塞导致价格滞后

            # 初始化K线缓存（首次）
            if not _kline_inited:
                try:
                    await init_kline_cache(symbols)
                    _log.info(f"📊 K线缓存初始化完成: {len(symbols)} 个币对")
                    # 初始化行情检测器（依赖K线缓存）
                    kc = get_kline_cache()
                    if kc:
                        init_detector(kc)
                        _log.info("🔍 行情检测器就绪")
                    _kline_inited = True
                except Exception as e:
                    _log.info(f"[警告] K线缓存初始化失败: {e}")
            
            # 定期刷新K线（每60s更新一次，保持RSI/ADX新鲜）
            if _kline_inited and _tick % 10 == 0:
                try:
                    kc = get_kline_cache()
                    if kc:
                        update_tasks = [kc.update(s) for s in symbols]
                        if update_tasks:
                            await asyncio.gather(*update_tasks, return_exceptions=True)
                except Exception:
                    pass
                    
            # 确保获取的是等待完K线等各种IO操作后，最新鲜的价格
            prices = dict(feed.get_prices())
            for psym, pos in runner.positions.items():
                if psym not in prices or prices.get(psym, 0) <= 0:
                    prices[psym] = float(pos.get("entry", 0) or 0)
                    
            volumes = {s: t.volume_24h for s, t in feed._cache.items()}
            changes = feed.get_changes()
            funding_rates = feed.get_funding_rates()
            mark_prices = feed.get_mark_prices()
            await runner.tick(prices, volumes, changes, funding_rates, mark_prices)
            get_state()["symbols"] = list(symbols)
            get_state()["trading_track"] = _trk

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

    # ── 启动等待依赖服务就绪 ──
    from execution.prod_utils import wait_for_redis
    if redis_url:
        wait_for_redis(redis_url, timeout=60)

    redis_client = await create_redis(redis_url) if redis_url else None
    _storage_bridge = PersistenceBridge(repository=repo, redis_client=redis_client)
    if repo:
        _log.info("Postgres persistence enabled (orders/trades/balance_logs)")
    if redis_client:
        _log.info("Redis enabled (state cache + gate REST rate limit)")
    port = int(os.environ.get("SHARK_HTTP_PORT", "80"))
    feed = MarketDataFeed()
    runner = StrategyRunner(initial_balance=500.0, persistence=_storage_bridge)
    # ── 尝试从 Redis 恢复模拟盘状态 ──
    paper_restored = runner._load_paper_state()
    if paper_restored:
        _log.info(f"[启动] 已从 Redis 恢复模拟盘状态 (余额=${runner.balance:.2f})")
    else:
        # 首次启动，初始化 Redis 状态
        runner._save_paper_state()
    # FastLoop 门禁注入
    if redis_client:
        from execution.plan_gate import PlanGate
        import redis as sync_redis
        sync_rdb = sync_redis.from_url(redis_url or "redis://redis:6379/0", decode_responses=True)

        # ── 启动时清空所有计划，触发 Go planner 重新生成 ──
        try:
            old_keys = list(sync_rdb.scan_iter(match="shark:plan:*", count=100))
            if old_keys:
                sync_rdb.delete(*old_keys)
                _log.info(f"[启动] 已清除 {len(old_keys)} 个旧计划")
            # 通知 Go planner 立即重新 Bootstrap
            for sym in ("BTC/USDT", "ETH/USDT", "SOL/USDT"):
                sync_rdb.publish("shark:plan:replan", json.dumps({"symbol": sym}))
        except Exception as e:
            _log.info(f"[启动] 计划清理失败: {e}")

        runner._plan_gate = PlanGate(sync_rdb)
        get_state()["_plan_gate"] = runner._plan_gate
        get_state()["_redis_client"] = redis_client  # 供 Plans API 直接读取（async）
    get_state()["initial_capital"] = runner._initial_capital
    get_state()["free_cash"] = runner.balance
    get_state()["balance"] = runner.balance
    get_state()["equity"] = runner.equity

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level="warning",
        access_log=False,
        ws_ping_interval=None,
        ws_ping_timeout=None,
    )
    server = uvicorn.Server(config)

    _log.info(f"🦈 Shark 2.0 启动成功 :{port}")
    # 启动告警
    try:
        from execution.prod_alert import _send_slack
        asyncio.create_task(_send_slack("🟢 [Shark] 系统启动 paper模式 初始$500"))
    except Exception:
        pass
    
    # 启动状态一览
    _shark_mode = os.environ.get("SHARK_MODE", "paper").lower()
    _paper_state = "关闭" if not get_state().get("paper_trading") else "开启"
    _live_state = "关闭" if not get_state().get("live_trading") else "开启"
    _log.info(f"📋 当前模式: {_shark_mode} | 模拟盘: {_paper_state} | 实盘: {_live_state}")
    _log.info("💡 提示: 前端点击「开始交易」后才开仓")
    
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

    # Go 进化引擎订阅：监听 shark:evo:pending → 加入待审批队列
    async def evo_subscriber():
        if not redis_client:
            return
        pubsub = redis_client.pubsub()
        await pubsub.subscribe("shark:evo:pending")
        _log.info("[进化订阅] 已订阅 shark:evo:pending")
        async for msg in pubsub.listen():
            if msg["type"] != "message":
                continue
            try:
                raw = msg["data"]
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", errors="replace")
                change = json.loads(raw)
                runner.merge_evo_suggestion(change)
                _log.info(f"[进化订阅] 收到建议 #{change.get('id')}: {change.get('type')}")
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
        _log.info("[RL订阅] 已订阅 shark:rl:action")
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
                    if os.environ.get("SHARK_ENABLE_RL_ORDERS", "").strip().lower() not in ("1", "true", "yes", "on"):
                        continue
                    # 实盘走 executor，模拟盘走 matcher；RL 命令也必须包含 size/leverage 等完整字段
                    mode = "live" if (runner._live and runner._live.active) else "paper"
                    cmd = build_rl_order_command(action, mode=mode)
                    await redis_client.publish("shark:orders:new", cmd)
            except Exception as e:
                _log.debug("rl_action_subscriber: %s", e)

    rl_task = asyncio.create_task(rl_action_subscriber()) if redis_client else None

    # ── 优雅关闭：捕获 CancelledError（Docker SIGTERM → uvicorn 取消 → gather 抛异常）──
    _last_prices: dict = {}
    _orig_update = StrategyRunner._update_state
    def _patched_update(self, prices):
        if prices:
            _last_prices.update(prices)
        _orig_update(self, prices)
    StrategyRunner._update_state = _patched_update

    try:
        await asyncio.gather(
            server.serve(),
            trading_loop(feed, runner),
            price_feed_loop(feed, runner, interval=1),
            dialogue_ammo_loop(),
        )
    except asyncio.CancelledError:
        pass
    finally:
        _log.info("\n[关闭] 正在强平持仓...")
        runner._force_close_all_positions(_last_prices)
        runner._save_paper_state()
        # 等待异步持久化完成
        await asyncio.sleep(0.5)
        _log.info("[关闭] 完成")


if __name__ == "__main__":
    asyncio.run(main())
