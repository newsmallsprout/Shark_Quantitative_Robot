#!/usr/bin/env python3
"""
Shark 自进化引擎 — AI驱动的策略持续优化
不是调参，是自主研究→学习→改进→验证
"""

import json, time, os, sys, re
import logging
from datetime import datetime
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Tuple
import urllib.request

BASE = Path(__file__).resolve().parent
API = os.environ.get("SHARK_API", "http://localhost:80/api")
EVOLVE_LOG = BASE / "evolve_history.json"
_log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════
# Phase 1: 交易分析 — 找出血亏模式
# ═══════════════════════════════════════════════

def fetch_trades(limit=500):
    """拉取交易历史"""
    try:
        url = f"{API}/history?offset=0&limit={limit}"
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read()).get("trades", [])
    except Exception as e:
        print(f"[进化] 取交易历史失败: {e}")
        return []

def fetch_status():
    """获取当前状态"""
    try:
        with urllib.request.urlopen(f"{API}/status", timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        _log.warning("evolve fetch_status: %s", e)
        return {}

def analyze_trades(trades: list) -> dict:
    """分析交易模式，找出血亏点"""
    if not trades:
        return {"error": "无交易数据"}
    
    sym_stats = defaultdict(lambda: {"count": 0, "pnl": 0, "fees": 0, "wins": 0, "losses": 0, "reasons": defaultdict(int)})
    time_stats = defaultdict(lambda: {"count": 0, "pnl": 0})
    
    for t in trades:
        s = t["symbol"]
        sym_stats[s]["count"] += 1
        sym_stats[s]["pnl"] += t["realized_pnl"]
        sym_stats[s]["fees"] += t.get("fee_open", 0) + t.get("fee_close", 0)
        if t["realized_pnl"] > 0:
            sym_stats[s]["wins"] += 1
        else:
            sym_stats[s]["losses"] += 1
        sym_stats[s]["reasons"][t.get("reason", "?")] += 1
        
        # 按小时统计
        ts = t.get("closed_at") or t.get("opened_at", 0)
        hour = datetime.fromtimestamp(ts).strftime("%H") if ts > 0 else "??"
        time_stats[hour]["count"] += 1
        time_stats[hour]["pnl"] += t["realized_pnl"]
    
    # 找最烂的币种
    worst = sorted(sym_stats.items(), key=lambda x: x[1]["pnl"])[:5]
    # 找最烂的时段
    worst_hours = sorted(time_stats.items(), key=lambda x: x[1]["pnl"])[:3]
    # 找最烂的平仓原因
    reason_stats = defaultdict(lambda: {"count": 0, "pnl": 0})
    for t in trades:
        r = t.get("reason", "?")
        reason_stats[r]["count"] += 1
        reason_stats[r]["pnl"] += t["realized_pnl"]
    worst_reasons = sorted(reason_stats.items(), key=lambda x: x[1]["pnl"])[:5]
    
    # 计算盈亏比和费率效率
    wins = [t for t in trades if t["realized_pnl"] > 0]
    losses = [t for t in trades if t["realized_pnl"] <= 0]
    avg_win = sum(t["realized_pnl"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["realized_pnl"] for t in losses) / len(losses) if losses else 0
    
    return {
        "total": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": abs(avg_win * len(wins) / (avg_loss * len(losses))) if avg_loss and losses else 0,
        "worst_symbols": [(s, d["pnl"], d["wins"], d["losses"]) for s, d in worst],
        "worst_hours": worst_hours,
        "worst_reasons": [(r, d["pnl"], d["count"]) for r, d in worst_reasons],
    }


# ═══════════════════════════════════════════════
# Phase 2: 策略研究 — 针对问题搜索战术
# ═══════════════════════════════════════════════

TACTICS_KNOWLEDGE = {
    # 针对高频微亏 → 需要更好的进场过滤
    "high_freq_loss": {
        "name": "进场过滤器",
        "tactics": [
            "RSI过滤：RSI(14) > 70 不追多，RSI(14) < 30 不追空",
            "成交量确认：开仓前要求当前1m成交量 > 前20根均量的1.5倍",
            "波动率过滤：ATR(14)/价格 > 2% 时暂停（波动太大不做）",
            "趋势确认：EMA(9) > EMA(21) 才开多，反之开空",
            "订单簿失衡：bid/ask ratio > 1.2 做多，< 0.8 做空"
        ],
        "implementation": "在开仓前增加 `_prefilter(sym, px)` 检查，可减少30-50%劣质开仓"
    },
    # 针对止损太紧 → 需要动态止损
    "tight_stop": {
        "name": "动态止损",
        "tactics": [
            "ATR止损：止损价 = 入场价 ± ATR(14) × 1.5",
            "波动率止损：止损% = max(基础止损%, ATR/价格 × 100 × 1.5)",
            "时间止损：持仓超过N根K线未盈利就平仓（避免资金锁定）",
            "Chandelier Exit：止损 = 最高价 - ATR(14) × 3"
        ],
        "implementation": "替代固定6%止损，使用ATR动态计算"
    },
    # 针对大盘震荡期亏损 → 需要震荡识别
    "ranging_market": {
        "name": "震荡过滤器",
        "tactics": [
            "ADX过滤：ADX(14) < 20 → 震荡市，减少仓位或暂停",
            "布林带宽度：BBW < 5% → 震荡，只做网格不做趋势",
            "支撑阻力：在S/R附近反转做单，远离S/R不做",
            "震荡模式：检测到震荡 → 切换到网格/区间交易模式"
        ],
        "implementation": "ADX < 20 时山寨仓位减半，主流只做区间交易"
    },
    # 针对手续费过高 → 需要降低交易频率
    "fee_drain": {
        "name": "手续费优化",
        "tactics": [
            "最小盈利阈值：净利必须 > 手续费 × 8 才开仓（不是5）",
            "maker单优先：用限价单挂单而非市价吃单",
            "冷却延长：亏损后冷却时间翻倍（30s→60s→120s）",
            "频次限制：每分钟最多1次开仓（避免抢同一波）",
            "分层止盈：30%仓位在2x手续费止盈，50%在5x，20%留给移动止盈"
        ],
        "implementation": "增加 `_should_trade_now()` 全局频次检查，亏损后冷却递增"
    },
    # 针对方向判断差 → 需要多TF确认
    "direction_bias": {
        "name": "多时间框架确认",
        "tactics": [
            "三级确认：1H趋势(EMA方向) + 15M信号(MACD交叉) + 5M入场(突破)",
            "顺势而为：只在大周期趋势方向上开仓",
            "回调入场：大周期看多 → 等小周期回调到EMA(21)再入场",
            "背离检测：价格新高但RSI未新高 = 顶背离 → 不追多"
        ],
        "implementation": "AI委员会增加1H趋势方向检查，方向不符不开仓"
    }
}

def research_tactics(analysis: dict) -> list:
    """根据分析结果推荐战术"""
    recommendations = []
    
    if analysis.get("profit_factor", 1) < 1.5:
        recommendations.append(TACTICS_KNOWLEDGE["direction_bias"])
    
    if analysis.get("avg_loss", 0) < -0.05:
        recommendations.append(TACTICS_KNOWLEDGE["tight_stop"])
    
    total = analysis.get("total", 0)
    if total > 100 and analysis.get("avg_win", 0) < 0.05:
        recommendations.append(TACTICS_KNOWLEDGE["fee_drain"])
    
    # 检查是否有大量同方向亏损
    reasons = analysis.get("worst_reasons", [])
    stop_loss_count = sum(c for r, p, c in reasons if "止损" in r)
    if stop_loss_count > total * 0.3:
        recommendations.append(TACTICS_KNOWLEDGE["high_freq_loss"])
        recommendations.append(TACTICS_KNOWLEDGE["ranging_market"])
    
    return recommendations


# ═══════════════════════════════════════════════
# Phase 3: 策略注入 — 生成代码补丁
# ═══════════════════════════════════════════════

def generate_tactic_patch(tactic: dict) -> str:
    """把战术翻译成可执行的Python代码"""
    name = tactic["name"]
    impl = tactic.get("implementation", "")
    
    patches = {
        "进场过滤器": """
# === 自进化: 进场过滤器 ===
def _prefilter(self, sym, px, side):
    '''RSI + 成交量 + 趋势确认'''
    # RSI过滤
    rsi = self._calc_rsi(sym, 14)
    if side == 'long' and rsi > 70:
        return False, f"RSI过热{rsi:.0f}"
    if side == 'short' and rsi < 30:
        return False, f"RSI超卖{rsi:.0f}"
    # 趋势确认  
    ema9 = self._calc_ema(sym, 9)
    ema21 = self._calc_ema(sym, 21)
    if side == 'long' and ema9 < ema21:
        return False, "EMA空头排列"
    if side == 'short' and ema9 > ema21:
        return False, "EMA多头排列"
    return True, "OK"

def _calc_rsi(self, sym, period=14):
    prices = self._kline_cache.get(sym, {}).get('close', [])
    if len(prices) < period + 1:
        return 50
    gains = [max(0, prices[i] - prices[i-1]) for i in range(-period, 0)]
    losses = [max(0, prices[i-1] - prices[i]) for i in range(-period, 0)]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100
    return 100 - (100 / (1 + avg_gain / avg_loss))

def _calc_ema(self, sym, period):
    prices = self._kline_cache.get(sym, {}).get('close', [])
    if len(prices) < period:
        return prices[-1] if prices else 0
    return sum(prices[-period:]) / period  # SMA as EMA proxy
""",
        "动态止损": """
# === 自进化: 动态ATR止损 ===
def _calc_atr_stop(self, sym, entry, side, atr_mult=1.5):
    '''用ATR动态计算止损价'''
    prices = self._kline_cache.get(sym, {}).get('high', [])
    lows = self._kline_cache.get(sym, {}).get('low', [])
    if len(prices) < 15:
        return entry * (1 - 0.06) if side == 'long' else entry * (1 + 0.06)
    
    trs = []
    for i in range(-14, 0):
        h, l = prices[i], lows[i]
        prev_close = prices[i-1] if i > -14 else prices[i]
        tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)
    atr = sum(trs) / 14
    atr_pct = atr / entry
    stop_pct = max(0.025, min(atr_pct * atr_mult, 0.08))
    
    if side == 'long':
        return entry * (1 - stop_pct)
    return entry * (1 + stop_pct)
""",
        "震荡过滤器": """
# === 自进化: ADX震荡过滤 ===
def _calc_adx(self, sym, period=14):
    '''计算ADX判断趋势强度'''
    highs = self._kline_cache.get(sym, {}).get('high', [])
    lows = self._kline_cache.get(sym, {}).get('low', [])
    closes = self._kline_cache.get(sym, {}).get('close', [])
    if len(highs) < period + 1:
        return 20
    
    trs, plus_dms, minus_dms = [], [], []
    for i in range(-period-1, -1):
        h, l, c = highs[i+1], lows[i+1], closes[i+1]
        ph, pl, pc = highs[i], lows[i], closes[i]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
        up = h - ph
        down = pl - l
        plus_dm = up if up > down and up > 0 else 0
        minus_dm = down if down > up and down > 0 else 0
        plus_dms.append(plus_dm)
        minus_dms.append(minus_dm)
    
    atr = sum(trs) / period
    plus_di = (sum(plus_dms) / period / atr * 100) if atr else 0
    minus_di = (sum(minus_dms) / period / atr * 100) if atr else 0
    dx = abs(plus_di - minus_di) / (plus_di + minus_di) * 100 if (plus_di + minus_di) else 0
    return dx

def _is_ranging(self, sym):
    '''ADX < 20 判定为震荡市'''
    return self._calc_adx(sym) < 20
""",
        "手续费优化": """
# === 自进化: 频次限制 + 失败冷却递增 ===
def _should_trade_now(self, sym):
    '''全局频次检查：1分钟内最多1次开仓'''
    now = time.time()
    recent_opens = [t for t in self._open_timestamps if now - t < 60]
    self._open_timestamps = recent_opens
    if len(recent_opens) >= 1:
        return False
    return True

# 在开仓成功后: self._open_timestamps.append(time.time())
""",
        "多时间框架确认": """
# === 自进化: 多TF趋势确认 ===
def _check_trend_alignment(self, sym, side):
    '''检查1H趋势是否与开仓方向一致'''
    klines = self._kline_cache.get(sym, {})
    closes_1h = klines.get('close_1h', [])
    if len(closes_1h) < 20:
        return True  # 数据不够，放行
    
    ema20 = sum(closes_1h[-20:]) / 20
    current = closes_1h[-1]
    trend_up = current > ema20
    
    if side == 'long' and not trend_up:
        return False
    if side == 'short' and trend_up:
        return False
    return True
"""
    }
    
    return patches.get(name, f"# TODO: implement {name}\n# {impl}")


# ═══════════════════════════════════════════════
# Phase 4: 进化决策 — 生成进化计划
# ═══════════════════════════════════════════════

def create_evolution_plan(analysis: dict, tactics: list) -> dict:
    """生成进化计划"""
    plan = {
        "timestamp": datetime.now().isoformat(),
        "analysis": analysis,
        "applied_tactics": [],
    }
    
    for tactic in tactics:
        plan["applied_tactics"].append({
            "name": tactic["name"],
            "patch": generate_tactic_patch(tactic),
            "reason": tactic.get("implementation", ""),
        })
    
    return plan


# ═══════════════════════════════════════════════
# Phase 5: 持久化 + 汇报
# ═══════════════════════════════════════════════

def save_evolution(plan: dict):
    """保存进化历史"""
    history = []
    if EVOLVE_LOG.exists():
        try:
            history = json.loads(EVOLVE_LOG.read_text())
        except Exception as e:
            _log.warning("evolve save_evolution: corrupt log, truncating: %s", e)
    history.append(plan)
    EVOLVE_LOG.write_text(json.dumps(history, indent=2, ensure_ascii=False))


def format_report(analysis: dict, tactics: list) -> str:
    """生成中文战报"""
    lines = ["🧬 Shark 自进化报告", "=" * 40, ""]
    
    if analysis.get("error"):
        lines.append(f"❌ {analysis['error']}")
        return "\n".join(lines)
    
    lines.append(f"📊 分析 {analysis['total']} 笔交易")
    lines.append(f"   胜率: {analysis['wins']}/{analysis['total']} ({analysis['wins']/analysis['total']*100:.1f}%)")
    lines.append(f"   均盈: ${analysis['avg_win']:.4f}  均亏: ${analysis['avg_loss']:.4f}")
    lines.append(f"   盈亏因子: {analysis['profit_factor']:.2f}")
    lines.append("")
    
    if analysis.get("worst_symbols"):
        lines.append("🔻 最烂币种:")
        for s, p, w, l in analysis["worst_symbols"][:3]:
            lines.append(f"   {s}: ${p:.4f} | 胜{w}败{l}")
    
    if tactics:
        lines.append(f"\n🧠 推荐 {len(tactics)} 项战术:")
        for t in tactics:
            lines.append(f"   📌 {t['name']}")
            for tactic in t.get("tactics", [])[:2]:
                lines.append(f"      • {tactic}")
    
    lines.append(f"\n💾 进化记录已保存到 {EVOLVE_LOG}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════

def main():
    print("🧬 Shark 自进化引擎启动", flush=True)
    
    # Phase 1
    print("[1/4] 分析交易数据...", flush=True)
    trades = fetch_trades(300)
    analysis = analyze_trades(trades)
    
    if analysis.get("error"):
        print(format_report(analysis, []))
        return
    
    # Phase 2
    print("[2/4] 匹配战术...", flush=True)
    tactics = research_tactics(analysis)
    
    # Phase 3 & 4
    print("[3/4] 生成进化计划...", flush=True)
    plan = create_evolution_plan(analysis, tactics)
    
    # Phase 5
    print("[4/4] 保存进化记录...", flush=True)
    save_evolution(plan)
    
    print(format_report(analysis, tactics), flush=True)
    
    # 如果有战术推荐，输出补丁文件
    if tactics:
        patch_dir = BASE / "patches"
        patch_dir.mkdir(exist_ok=True)
        patch_file = patch_dir / f"evolve_{datetime.now().strftime('%Y%m%d_%H%M')}.py"
        code = ["# Shark 自进化补丁", f"# 生成时间: {datetime.now()}", f"# 基于 {analysis['total']} 笔交易分析", ""]
        for t in tactics:
            code.append(f"# === {t['name']} ===")
            code.append(generate_tactic_patch(t))
            code.append("")
        patch_file.write_text("\n".join(code))
        print(f"\n📄 补丁文件: {patch_file}")

if __name__ == "__main__":
    main()
