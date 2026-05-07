"""Shark 2.0 震荡检测器 + 浮亏管理 — 双方向仓位调控"""

import time
from collections import deque


class OscillationDetector:
    """检测趋势/震荡模式，震荡时均值回归 + 浮亏补仓拉均价"""
    
    def __init__(self, lookback_ticks: int = 30, tightness_threshold: float = 0.018):
        self.lookback = lookback_ticks
        self.tightness_threshold = tightness_threshold
        self._history: dict = {}
        self._mode: dict = {}
        self._bands: dict = {}

    def feed(self, sym: str, price: float):
        if sym not in self._history:
            self._history[sym] = deque(maxlen=self.lookback)
        self._history[sym].append((price, time.time()))
        
        hist = self._history[sym]
        if len(hist) < self.lookback // 2:
            return
        
        prices = [p for p, _ in hist]
        range_high = max(prices)
        range_low = min(prices)
        range_mid = (range_high + range_low) / 2
        range_pct = (range_high - range_low) / max(range_mid, 1e-9)
        
        if range_pct < self.tightness_threshold:
            self._mode[sym] = "oscillation"
            self._bands[sym] = {
                "high": range_high, "low": range_low, "mid": range_mid,
                "range_pct": range_pct,
            }
        else:
            self._mode[sym] = "trend"
            if sym in self._bands:
                self._bands[sym]["range_pct"] = range_pct

    def get_mode(self, sym: str) -> str:
        return self._mode.get(sym, "trend")

    def get_bands(self, sym: str) -> dict:
        return self._bands.get(sym, {"high": 0, "low": 0, "mid": 0})

    def get_oscillation_signal(self, sym: str, price: float,
                                funding_rate: float = 0) -> tuple:
        """震荡模式开仓信号"""
        if self.get_mode(sym) != "oscillation":
            return None, 0, ""
        
        bands = self.get_bands(sym)
        if not bands["high"] or not bands["low"]:
            return None, 0, ""
        
        high, low = bands["high"], bands["low"]
        dist_to_low = (price - low) / max(low, 1e-9)
        dist_to_high = (high - price) / max(high, 1e-9)
        
        if dist_to_low < 0.005:
            conf = 65 - (20 if abs(funding_rate) > 0.001 else 0)
            return "long", max(conf, 40), f"震荡触底({low:.2f})"
        if dist_to_high < 0.005:
            conf = 65 - (20 if abs(funding_rate) > 0.001 else 0)
            return "short", max(conf, 40), f"震荡触顶({high:.2f})"
        return None, 0, ""

    def should_avg_down(self, sym: str, price: float, entry: float, 
                         pside: str, pnl_pct: float) -> tuple:
        """浮亏补仓判断：震荡模式+接近反向边界+亏损可控
        
        Returns: (should_add: bool, add_ratio: float, reason: str)
        """
        if self.get_mode(sym) != "oscillation":
            return False, 0, ""
        
        bands = self.get_bands(sym)
        if not bands["high"] or not bands["low"]:
            return False, 0, ""
        
        high, low = bands["high"], bands["low"]
        
        # 亏损必须 > 3% 才考虑补仓（太小不值得）
        if pnl_pct > -3.0:
            return False, 0, ""
        # 亏损 > 20% 不补（趋势可能反转）
        if pnl_pct < -20.0:
            return False, 0, "亏损过大不补"
        
        if pside == "long":
            # 做多亏损中 → 价格接近下边界补仓
            dist = (price - low) / max(low, 1e-9)
            if dist < 0.008:  # 0.8% 以内
                add_ratio = min(0.5, abs(pnl_pct) / 30)  # 亏损越大补越多，最多补50%
                return True, max(add_ratio, 0.2), f"震荡补仓({low:.2f})"
        else:
            # 做空亏损中 → 价格接近上边界补仓
            dist = (high - price) / max(high, 1e-9)
            if dist < 0.008:
                add_ratio = min(0.5, abs(pnl_pct) / 30)
                return True, max(add_ratio, 0.2), f"震荡补仓({high:.2f})"
        
        return False, 0, ""
