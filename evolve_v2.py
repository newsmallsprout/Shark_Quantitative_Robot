#!/usr/bin/env python3
"""
Shark 自进化引擎 v2 — 实时模式
不是每小时批次分析，而是持续监控、即时进化

能力：
- 实时交易流分析（滑动窗口）
- 多交易所数据融合方向信号
- 战术库：进场/持仓/出场/风控 全覆盖
- 模式识别：连亏/震荡/趋势/高波动自动检测
- 进化：检测→匹配战术→沙箱回测→注入
"""

import time, json, os, sys
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from collections import deque, defaultdict
import urllib.request

BASE = Path(__file__).resolve().parent
API = os.environ.get("SHARK_API", "http://localhost:80/api")
EVOLVE_LOG = BASE / "evolve_v2_history.json"
STATE_FILE = BASE / "evolve_v2_state.json"


# ═══════════════════════════════════════════════
# Phase 1: 实时市场状态检测
# ═══════════════════════════════════════════════

@dataclass
class MarketRegime:
    """市场状态分类"""
    TRENDING_UP = "trending_up"      # 单边上涨
    TRENDING_DOWN = "trending_down"  # 单边下跌
    RANGING = "ranging"              # 震荡
    HIGH_VOL = "high_volatility"     # 高波动
    LOW_VOL = "low_volatility"       # 低波动躺平
    CHOPPY = "choppy"                # 多空绞杀


def detect_regime(trades: list, status: dict) -> MarketRegime:
    """检测当前市场状态"""
    if not trades:
        return MarketRegime.LOW_VOL
    
    # 最近20笔
    recent = trades[:20]
    
    # 连续同方向 = 趋势
    long_wins = sum(1 for t in recent if t.get("side") == "long" and t["realized_pnl"] > 0)
    short_wins = sum(1 for t in recent if t.get("side") == "short" and t["realized_pnl"] > 0)
    long_losses = sum(1 for t in recent if t.get("side") == "long" and t["realized_pnl"] <= 0)
    short_losses = sum(1 for t in recent if t.get("side") == "short" and t["realized_pnl"] <= 0)
    
    # 高低波动判断
    positions = status.get("position_list", [])
    avg_pnl_pct = sum(abs(p.get("pnl_pct", 0)) for p in positions) / max(len(positions), 1)
    
    # 止损频率
    stop_count = sum(1 for t in recent if "止损" in t.get("reason", ""))
    stop_ratio = stop_count / max(len(recent), 1)
    
    if stop_ratio > 0.6:
        return MarketRegime.CHOPPY  # 频繁止损 = 绞肉机行情
    
    if long_wins > long_losses * 2 and short_losses > short_wins:
        return MarketRegime.TRENDING_UP
    
    if short_wins > short_losses * 2 and long_losses > long_wins:
        return MarketRegime.TRENDING_DOWN
    
    if avg_pnl_pct > 8:
        return MarketRegime.HIGH_VOL
    
    if avg_pnl_pct < 1 and len(recent) >= 10:
        return MarketRegime.LOW_VOL
    
    return MarketRegime.RANGING


# ═══════════════════════════════════════════════
# Phase 2: 深度战术库
# ═══════════════════════════════════════════════

TACTICS = {
    # ── 进场优化 ──
    "multi_exchange_confirm": {
        "name": "多交易所方向确认",
        "trigger": "单交易所信号频繁被止损（CHOPPY/止损率>50%）",
        "action": "开仓前必须多交易所共识方向一致，Gate价格不偏离共识>0.3%",
        "code": """
# 多交易所确认
def _multi_exchange_confirm(self, sym, side):
    '''多交易所方向一致才开仓'''
    try:
        from multi_exchange import get_multi_feed
        feed = get_multi_feed()
        if feed:
            sig = feed.direction_signal(sym)
            if sig['bias'] != side and sig['bias'] != 'neutral':
                return False, f"多交易所方向不一致(sig={sig['bias']})"
            if sig['divergence'] > 0.5:
                return False, f"交易所价差过大({sig['divergence']:.2f}%)"
    except:
        pass
    return True, "OK"
"""
    },
    
    "volume_profile_entry": {
        "name": "成交量分布入场",
        "trigger": "LOW_VOL或RANGING时开仓频繁被止损",
        "action": "只在成交量POC（最大成交量价位）附近开仓，远离POC不开",
        "code": """
# 成交量分布入场
def _volume_poc_entry(self, sym, px):
    '''计算成交量POC（Point of Control），只在附近入场'''
    closes = list(self._kline_cache.get(sym, {}).get('close', []))[-50:]
    if len(closes) < 20:
        return True, 0
    # 简化的POC：取近期价格中位数附近
    sorted_px = sorted(closes)
    poc = sorted_px[len(sorted_px)//2]
    deviation = abs(px - poc) / poc * 100
    if deviation > 1.5:
        return False, deviation
    return True, deviation
"""
    },
    
    "funding_rate_filter": {
        "name": "资金费率过滤器",
        "trigger": "负费率时做空或正费率时做多频繁亏损",
        "action": "极值费率(>0.1%或<-0.1%)反向操作；中性费率趋势跟随",
        "code": """
# 资金费率信号
def _funding_signal(self, sym, funding):
    '''资金费率极值=反向信号，中性=趋势跟随'''
    if funding > 0.001:  # 极高正费率 → 市场过热 → 做空
        return 'short', min(abs(funding)*10000, 70)
    elif funding < -0.001:  # 极高负费率 → 市场恐慌 → 做多
        return 'long', min(abs(funding)*10000, 70)
    return None, 0  # 中性，无信号
"""
    },
    
    # ── 持仓管理 ──
    "dynamic_position_sizing": {
        "name": "动态仓位管理",
        "trigger": "连亏3+笔或胜率<40%",
        "action": "连亏时仓位减半；连盈时仓位恢复；最大仓位=余额×波动衰减因子",
        "code": """
# 动态仓位
def _dynamic_position_size(self, margin, consecutive):
    '''连亏缩仓，连盈扩仓'''
    if consecutive >= 3:
        factor = 0.3  # 连亏3笔→仓位缩到30%
    elif consecutive >= 2:
        factor = 0.5
    elif consecutive >= 1:
        factor = 0.7
    else:
        factor = 1.0
    return margin * factor
"""
    },
    
    "time_based_exit": {
        "name": "时间止损",
        "trigger": "持仓时间>10分钟仍微利/微亏",
        "action": "持仓超时且盈利<3x手续费→平仓，释放资金",
        "code": """
# 时间止损
def _time_stop(self, pos, fee_est, max_minutes=10):
    '''持仓超时且微利，平仓释放资金'''
    age = time.time() - pos['opened']
    if age > max_minutes * 60:
        gross = self._gross_pnl_usd(sym, pos, px)
        if abs(gross) < fee_est * 5:
            return True, "超时微利"
    return False, ""
"""
    },
    
    "scale_out_profit": {
        "name": "分层止盈",
        "trigger": "经常盈利后回吐变成亏损",
        "action": "30%@2x手续费止盈, 40%@5x, 30%留给移动止盈",
        "code": """
# 分层止盈
def _scale_out(self, sym, pos, px, fee_est):
    '''分层止盈：越涨越卖，锁定利润'''
    gross = self._gross_pnl_usd(sym, pos, px)
    
    # 第一层：够本就走30%
    if gross > fee_est * 2 and not pos.get('tp1_done'):
        return {'action': 'close_partial', 'ratio': 0.3, 'reason': 'TP1保本'}
    
    # 第二层：5x手续费走40%
    if gross > fee_est * 5 and not pos.get('tp2_done'):
        return {'action': 'close_partial', 'ratio': 0.4, 'reason': 'TP2获利'}
    
    # 第三层：剩余留给移动止盈
    return None
"""
    },
    
    # ── 风控优化 ──
    "volatility_adaptive_stop": {
        "name": "波动率自适应止损",
        "trigger": "固定6%止损在高低波动时都不合适",
        "action": "止损% = ATR(14)/价格 × 2，高波动放宽止损，低波动收紧",
        "code": """
# 自适应止损
def _adaptive_stop_pct(self, sym, base_sl=-6.0):
    '''基于ATR的自适应止损'''
    try:
        from kline_cache import get_kline_cache
        cache = get_kline_cache()
        if cache:
            atr_pct = cache.volatility_pct(sym)
            # ATR波动率越高中止损越宽（避免被噪音震出）
            adaptive = max(base_sl, -atr_pct * 3)
            return max(adaptive, -15.0)  # 上限-15%
    except:
        pass
    return base_sl
"""
    },
    
    "correlation_hedge": {
        "name": "相关性对冲",
        "trigger": "多个高度相关币种同向持仓",
        "action": "SOL/BTC/ETH相关性>0.7时限制同向仓位数",
        "code": """
# 相关性检查
def _correlation_check(self, positions):
    '''高度相关币种限制同向仓位'''
    correlated_groups = [
        ('BTC/USDT', 'ETH/USDT'),  # BTC-ETH高相关
        ('SOL/USDT', 'SUI/USDT'),  # SOL-SUI同生态
    ]
    for a, b in correlated_groups:
        if a in positions and b in positions:
            if positions[a]['side'] == positions[b]['side']:
                return True, f'{a}/{b}同向持仓'
    return False, ''
"""
    },
    
    # ── 趋势/震荡自适应 ──
    "regime_adaptive": {
        "name": "市场状态自适应",
        "trigger": "市场状态切换时策略不匹配",
        "action": "TRENDING→趋势策略(追涨杀跌)；RANGING→网格/区间；CHOPPY→暂停",
        "code": """
# 状态自适应
def _regime_strategy(self, regime):
    '''根据市场状态切换策略模式'''
    if regime == 'trending_up':
        return {'mode': 'trend', 'prefer': 'long', 'pyramid': True}
    elif regime == 'trending_down':
        return {'mode': 'trend', 'prefer': 'short', 'pyramid': True}
    elif regime == 'ranging':
        return {'mode': 'range', 'grid_spacing': 0.02, 'max_grids': 5}
    elif regime in ('choppy', 'high_volatility'):
        return {'mode': 'pause', 'reason': '绞肉机/高波暂停'}
    else:
        return {'mode': 'normal'}
"""
    },
}


# ═══════════════════════════════════════════════
# Phase 3: 模式识别引擎
# ═══════════════════════════════════════════════

class PatternDetector:
    """实时交易模式识别"""
    
    def __init__(self):
        self.window = deque(maxlen=50)  # 滑动窗口
        self.patterns: Dict[str, int] = defaultdict(int)
        self.last_detection = 0
    
    def feed(self, trade: dict):
        """喂入一笔新交易"""
        self.window.append(trade)
    
    def detect(self) -> List[Tuple[str, float, str]]:
        """检测当前模式，返回(模式名, 置信度, 推荐战术名)"""
        if len(self.window) < 10:
            return []
        
        now = time.time()
        if now - self.last_detection < 30:  # 30秒内不重复检测
            return []
        self.last_detection = now
        
        trades = list(self.window)
        results = []
        
        # 模式1: 连续亏损
        recent = trades[-10:]
        consecutive_losses = 0
        for t in reversed(recent):
            if t["realized_pnl"] <= 0:
                consecutive_losses += 1
            else:
                break
        
        if consecutive_losses >= 3:
            conf = min(95, consecutive_losses * 25)
            results.append(("consecutive_loss", conf, "dynamic_position_sizing"))
            results.append(("consecutive_loss", conf, "funding_rate_filter"))
        
        if consecutive_losses >= 5:
            results.append(("consecutive_loss", 95, "regime_adaptive"))
        
        # 模式2: 止损频率过高
        stops = sum(1 for t in recent if "止损" in t.get("reason", ""))
        if stops > len(recent) * 0.5:
            results.append(("high_stop_rate", 80, "volatility_adaptive_stop"))
            results.append(("high_stop_rate", 80, "multi_exchange_confirm"))
        
        # 模式3: 微利就反转为亏损
        wins_then_lose = 0
        for t in recent:
            if t["realized_pnl"] > 0 and t["realized_pnl"] < 0.05:
                # 检查下一笔是否亏损
                idx = recent.index(t)
                if idx < len(recent) - 1 and recent[idx+1]["realized_pnl"] < -0.03:
                    wins_then_lose += 1
        if wins_then_lose >= 2:
            results.append(("profit_reversal", 70, "scale_out_profit"))
        
        # 模式4: 长时间微利持仓
        # (需要持仓时长数据，这里简化)
        
        # 模式5: 同币种同方向反复开仓
        sym_sides = defaultdict(int)
        for t in recent[-20:]:
            key = f"{t['symbol']}_{t.get('side','')}"
            sym_sides[key] += 1
        for key, count in sym_sides.items():
            if count >= 4:
                results.append(("overtrading", 70, "volume_profile_entry"))
                break
        
        return results


# ═══════════════════════════════════════════════
# Phase 4: 进化决策
# ═══════════════════════════════════════════════

@dataclass
class EvolutionRecord:
    timestamp: str
    pattern: str
    tactic: str
    reason: str
    applied: bool
    result: Optional[str] = None


class EvolutionEngine:
    """实时进化引擎"""
    
    def __init__(self):
        self.detector = PatternDetector()
        self.history: List[EvolutionRecord] = []
        self.applied_tactics: set = set()  # 已应用战术（避免重复）
        self.cooldowns: Dict[str, float] = {}  # 战术冷却
        self._load_state()
    
    def _load_state(self):
        if STATE_FILE.exists():
            try:
                state = json.loads(STATE_FILE.read_text())
                self.applied_tactics = set(state.get("applied", []))
                self.cooldowns = state.get("cooldowns", {})
            except:
                pass
    
    def _save_state(self):
        STATE_FILE.write_text(json.dumps({
            "applied": list(self.applied_tactics),
            "cooldowns": self.cooldowns,
            "updated": datetime.now().isoformat(),
        }))
    
    def feed_trade(self, trade: dict):
        """喂入交易，检测模式，触发进化"""
        self.detector.feed(trade)
        
        patterns = self.detector.detect()
        if not patterns:
            return None
        
        # 取置信度最高的模式
        best = max(patterns, key=lambda x: x[1])
        pattern_name, confidence, tactic_name = best
        
        # 检查冷却
        now = time.time()
        if tactic_name in self.cooldowns and now - self.cooldowns[tactic_name] < 600:
            return None
        
        # 检查是否已应用
        if tactic_name in self.applied_tactics:
            return None
        
        # 检查战术是否存在
        tactic = TACTICS.get(tactic_name)
        if not tactic:
            return None
        
        # 触发进化
        record = EvolutionRecord(
            timestamp=datetime.now().isoformat(),
            pattern=pattern_name,
            tactic=tactic_name,
            reason=f"检测到{pattern_name}(置信度{confidence}%), 触发{tactic['name']}",
            applied=False,
        )
        
        return {
            "record": record,
            "tactic": tactic,
            "confidence": confidence,
        }
    
    def mark_applied(self, tactic_name: str):
        """标记战术已应用"""
        self.applied_tactics.add(tactic_name)
        self.cooldowns[tactic_name] = time.time()
        self._save_state()
    
    def get_tactic_code(self, tactic_name: str) -> str:
        """获取战术代码"""
        tactic = TACTICS.get(tactic_name, {})
        return tactic.get("code", "# 战术未找到")
    
    def format_evolution_report(self) -> str:
        """格式化进化报告"""
        if not self.history:
            return "无进化记录"
        
        lines = ["🧬 Shark 实时进化报告", "=" * 45, ""]
        
        recent = self.history[-5:]
        for r in recent:
            status = "✅" if r.applied else "⏳"
            lines.append(f"{status} [{r.timestamp[11:19]}] {r.tactic}")
            lines.append(f"   触发: {r.pattern} → {r.reason}")
            if r.result:
                lines.append(f"   结果: {r.result}")
            lines.append("")
        
        lines.append(f"累计进化: {len(self.history)} 次 | 已应用: {len(self.applied_tactics)} 个战术")
        return "\n".join(lines)


# ═══════════════════════════════════════════════
# Phase 5: API接口
# ═══════════════════════════════════════════════

def fetch_trades(limit=100):
    try:
        url = f"{API}/history?offset=0&limit={limit}"
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read()).get("trades", [])
    except:
        return []

def fetch_status():
    try:
        with urllib.request.urlopen(f"{API}/status", timeout=10) as r:
            return json.loads(r.read())
    except:
        return {}

def analyze_batch(trades: list, status: dict) -> dict:
    """批量分析（用于定期汇总）"""
    if not trades:
        return {"error": "无数据"}
    
    recent = trades[:50]
    wins = [t for t in recent if t["realized_pnl"] > 0]
    losses = [t for t in recent if t["realized_pnl"] <= 0]
    
    regime = detect_regime(recent, status)
    
    # 按原因分组
    reason_pnl = defaultdict(lambda: {"count": 0, "pnl": 0})
    for t in recent:
        r = t.get("reason", "?")
        reason_pnl[r]["count"] += 1
        reason_pnl[r]["pnl"] += t["realized_pnl"]
    
    # 按币种分组
    sym_pnl = defaultdict(lambda: {"count": 0, "pnl": 0, "wins": 0, "losses": 0})
    for t in recent:
        s = t["symbol"]
        sym_pnl[s]["count"] += 1
        sym_pnl[s]["pnl"] += t["realized_pnl"]
        if t["realized_pnl"] > 0:
            sym_pnl[s]["wins"] += 1
        else:
            sym_pnl[s]["losses"] += 1
    
    return {
        "total": len(recent),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / max(len(recent), 1),
        "avg_win": sum(t["realized_pnl"] for t in wins) / max(len(wins), 1),
        "avg_loss": sum(t["realized_pnl"] for t in losses) / max(len(losses), 1),
        "regime": regime,
        "positions": status.get("positions", 0),
        "equity": status.get("equity", 0),
        "worst_reasons": sorted(reason_pnl.items(), key=lambda x: x[1]["pnl"])[:3],
        "worst_symbols": sorted(sym_pnl.items(), key=lambda x: x[1]["pnl"])[:3],
    }


# ═══════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════

_engine = EvolutionEngine()

def get_engine() -> EvolutionEngine:
    return _engine

def run_batch_analysis():
    """批量分析模式（cron调用）"""
    trades = fetch_trades(200)
    status = fetch_status()
    analysis = analyze_batch(trades, status)
    
    # 喂入检测器
    for t in trades[:50]:
        _engine.detector.feed(t)
    
    lines = ["🧬 Shark 进化引擎 v2", "=" * 45, ""]
    lines.append(f"📊 分析 {analysis.get('total', 0)} 笔 | 胜率 {analysis.get('win_rate', 0)*100:.1f}%")
    lines.append(f"   市场状态: {analysis.get('regime', '?')}")
    lines.append(f"   均盈 ${analysis.get('avg_win', 0):.4f} | 均亏 ${analysis.get('avg_loss', 0):.4f}")
    
    patterns = _engine.detector.detect()
    if patterns:
        lines.append(f"\n⚠️ 检测到 {len(patterns)} 个模式:")
        for name, conf, tactic in patterns:
            t = TACTICS.get(tactic, {})
            lines.append(f"   [{conf:.0f}%] {name} → 推荐: {t.get('name', tactic)}")
            lines.append(f"         {t.get('action', '')}")
    
    return "\n".join(lines)


if __name__ == "__main__":
    print(run_batch_analysis())
