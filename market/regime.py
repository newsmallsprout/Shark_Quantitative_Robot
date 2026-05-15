"""
行情分类检测器 v1.0
每个币对独立判断当前行情类型，指导差异化开单策略
不再一刀切 — 不同的行情用不同的打法
"""

from enum import Enum
from typing import Tuple, Optional
import time

class MarketRegime(Enum):
    STRONG_TREND_UP = "strong_trend_up"        # 强多头趋势：ADX>30, +DI主导, MA向上
    STRONG_TREND_DOWN = "strong_trend_down"     # 强空头趋势：ADX>30, -DI主导, MA向下
    WEAK_TREND_UP = "weak_trend_up"            # 弱多头趋势：ADX 20-30, MA向上
    WEAK_TREND_DOWN = "weak_trend_down"         # 弱空头趋势：ADX 20-30, MA向下
    HIGH_VOL_RANGING = "high_vol_ranging"       # 高波动震荡：宽区间+高ATR，可高抛低吸
    LOW_VOL_RANGING = "low_vol_ranging"         # 低波动震荡：窄区间+低ATR，网格小刀
    BREAKOUT_UP = "breakout_up"                # 向上突破：量突变+价格突破区间上沿
    BREAKOUT_DOWN = "breakout_down"             # 向下突破：量突变+价格跌破区间下沿
    CHOPPY = "choppy"                           # 乱震：低ADX+高波动+方向频繁切换 → 不开仓
    DEAD = "dead"                               # 死水：极低ADX+极低波动 → 不开仓

# ═══════════════════════════════════════════════
# 每种行情 → 对应的开单策略参数
# ═══════════════════════════════════════════════
REGIME_CONFIG = {
    MarketRegime.STRONG_TREND_UP: {
        "allowed_dir": "long",
        "margin_mult": 1.6,
        "stop_atr_mult": 3.0,       # 止损 = ATR × 3（趋势给呼吸空间）
        "tp_atr_mult": 5.0,         # 止盈 = ATR × 5（吃大波段）
        "pyramid": True,
        "pyramid_levels": 3,
        "cooldown_s": 5,
        "desc": "强多趋势·顺势加仓",
    },
    MarketRegime.STRONG_TREND_DOWN: {
        "allowed_dir": "short",
        "margin_mult": 1.6,
        "stop_atr_mult": 3.0,
        "tp_atr_mult": 5.0,
        "pyramid": True,
        "pyramid_levels": 3,
        "cooldown_s": 5,
        "desc": "强空趋势·顺势加仓",
    },
    MarketRegime.WEAK_TREND_UP: {
        "allowed_dir": "long",
        "margin_mult": 0.6,
        "stop_atr_mult": 2.0,       # 弱趋势止损紧
        "tp_atr_mult": 3.0,
        "pyramid": False,
        "cooldown_s": 10,
        "desc": "弱多趋势·轻仓试探",
    },
    MarketRegime.WEAK_TREND_DOWN: {
        "allowed_dir": "short",
        "margin_mult": 0.6,
        "stop_atr_mult": 2.0,
        "tp_atr_mult": 3.0,
        "pyramid": False,
        "cooldown_s": 10,
        "desc": "弱空趋势·轻仓试探",
    },
    MarketRegime.HIGH_VOL_RANGING: {
        "allowed_dir": "both",
        "margin_mult": 0.7,
        "stop_atr_mult": 2.5,       # 高波止损宽
        "tp_atr_mult": 3.5,         # 快进快出
        "pyramid": False,
        "cooldown_s": 6,
        "desc": "高波震荡·高抛低吸",
    },
    MarketRegime.LOW_VOL_RANGING: {
        "allowed_dir": "both",
        "margin_mult": 0.5,
        "stop_atr_mult": 2.0,       # 低波止损紧
        "tp_atr_mult": 3.0,
        "pyramid": False,
        "cooldown_s": 10,
        "desc": "低波震荡·网格小刀",
    },
    MarketRegime.BREAKOUT_UP: {
        "allowed_dir": "long",
        "margin_mult": 1.2,
        "stop_atr_mult": 2.0,       # 突破假破快跑
        "tp_atr_mult": 4.0,
        "pyramid": False,
        "cooldown_s": 6,
        "desc": "向上突破·追多",
    },
    MarketRegime.BREAKOUT_DOWN: {
        "allowed_dir": "short",
        "margin_mult": 1.2,
        "stop_atr_mult": 2.0,
        "tp_atr_mult": 4.0,
        "pyramid": False,
        "cooldown_s": 6,
        "desc": "向下突破·追空",
    },
    MarketRegime.CHOPPY: {
        "allowed_dir": "both",
        "margin_mult": 0.15,
        "stop_atr_mult": 1.5,       # 乱震止损极紧
        "tp_atr_mult": 2.5,
        "desc": "乱震·蚊子仓试探",
    },
    MarketRegime.DEAD: {
        "allowed_dir": None,
        "margin_mult": 0,
        "desc": "死水·休眠",
    },
}


class RegimeDetector:
    """每个币对独立检测行情类型"""

    def __init__(self, kline_cache):
        self.kc = kline_cache
        self._cache: dict = {}      # sym -> (regime, diag, timestamp)

    def _plus_minus_di(self, symbol: str, period=14, interval="1m"):
        """计算 +DI 和 -DI"""
        highs, lows = self.kc.get_high_low(symbol, interval)
        closes = self.kc.get_close(symbol, interval)
        n = len(highs)
        if n < period + 1:
            return 25.0, 25.0

        trs, pdi_v, mdi_v = [], [], []
        for i in range(-period - 1, -1):
            h, l = highs[i + 1], lows[i + 1]
            ph, pl = highs[i], lows[i]
            pc = closes[i]
            tr = max(h - l, abs(h - pc), abs(l - pc))
            trs.append(tr)
            up_move = h - ph
            down_move = pl - l
            pdi_v.append(up_move if up_move > down_move and up_move > 0 else 0)
            mdi_v.append(down_move if down_move > up_move and down_move > 0 else 0)

        atr_val = sum(trs) / period
        if atr_val == 0:
            return 25.0, 25.0
        pdi = (sum(pdi_v) / period / atr_val) * 100
        mdi = (sum(mdi_v) / period / atr_val) * 100
        return pdi, mdi

    def _price_position(self, symbol: str, lookback=20, interval="1m") -> float:
        """价格在最近N根K线区间中的位置 0-100"""
        highs, lows = self.kc.get_high_low(symbol, interval)
        closes = self.kc.get_close(symbol, interval)
        n = min(lookback, len(highs))
        if n < 3:
            return 50.0
        hh = max(highs[-n:])
        ll = min(lows[-n:])
        if hh == ll:
            return 50.0
        return (closes[-1] - ll) / (hh - ll) * 100

    def _volume_spike(self, symbol: str, lookback=10, interval="1m") -> bool:
        """量突变：当前K线成交量 > 近期均值2倍"""
        data = self.kc.get(symbol, interval)
        vols = data.get("volume", [])
        if len(vols) < lookback + 1:
            return False
        recent_avg = sum(vols[-lookback - 1:-1]) / lookback
        if recent_avg == 0:
            return False
        return vols[-1] > recent_avg * 2.0

    def detect(self, symbol: str) -> Tuple[MarketRegime, dict]:
        """
        检测行情类型，返回 (regime, 诊断信息)
        有5秒缓存避免重复计算
        """
        now = time.time()
        if symbol in self._cache:
            r, d, ts = self._cache[symbol]
            if now - ts < 5:
                return r, d

        adx = self.kc.adx(symbol, period=14, interval="1m")
        pdi, mdi = self._plus_minus_di(symbol)
        vol_pct = self.kc.volatility_pct(symbol, interval="1m")
        rsi = self.kc.rsi(symbol, period=14, interval="5m")
        trend = self.kc.ma_trend(symbol, fast=9, slow=21, interval="1m")
        pos = self._price_position(symbol)
        vspike = self._volume_spike(symbol)

        diag = {
            "adx": round(adx, 1), "pdi": round(pdi, 1), "mdi": round(mdi, 1),
            "vol_pct": round(vol_pct, 2), "rsi": round(rsi, 1),
            "trend": trend, "pos": round(pos, 1), "vspike": vspike,
        }

        di_spread = abs(pdi - mdi)

        # ── 分层判定 ──

        # 死水
        if adx < 10 and vol_pct < 0.5:
            regime = MarketRegime.DEAD

        # 乱震
        elif adx < 15 and vol_pct > 2.0:
            regime = MarketRegime.CHOPPY

        # 突破（量突变 + 价格在区间极值 + 趋势确认）
        elif vspike and pos > 80 and adx > 18 and trend == "up":
            regime = MarketRegime.BREAKOUT_UP
        elif vspike and pos < 20 and adx > 18 and trend == "down":
            regime = MarketRegime.BREAKOUT_DOWN

        # 强趋势
        elif adx > 30 and di_spread > 8:
            if trend == "up" and pdi > mdi:
                regime = MarketRegime.STRONG_TREND_UP
            elif trend == "down" and mdi > pdi:
                regime = MarketRegime.STRONG_TREND_DOWN
            else:
                regime = MarketRegime.HIGH_VOL_RANGING  # 强ADX但方向矛盾

        # 弱趋势
        elif adx >= 20 and di_spread > 3:
            if trend == "up" and pdi > mdi:
                regime = MarketRegime.WEAK_TREND_UP
            elif trend == "down" and mdi > pdi:
                regime = MarketRegime.WEAK_TREND_DOWN
            else:
                regime = MarketRegime.LOW_VOL_RANGING

        # 高波震荡
        elif vol_pct > 2.0:
            regime = MarketRegime.HIGH_VOL_RANGING

        # 低波震荡（兜底）
        else:
            regime = MarketRegime.LOW_VOL_RANGING

        self._cache[symbol] = (regime, diag, now)
        return regime, diag


# 全局单例
_detector: Optional[RegimeDetector] = None


def get_detector() -> Optional[RegimeDetector]:
    return _detector


def init_detector(kline_cache) -> RegimeDetector:
    global _detector
    _detector = RegimeDetector(kline_cache)
    return _detector
