#!/usr/bin/env python3
"""
Shark K线数据缓存 — 为技术指标提供OHLCV数据
支持1m/5m/15m/1h周期，供进化策略使用
"""

import time
import asyncio
import aiohttp
from typing import Dict, List

GATE_KLINE = "https://api.gateio.ws/api/v4/futures/usdt/candlesticks"


class KlineCache:
    """K线数据缓存，支持多周期"""
    
    INTERVALS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}
    
    def __init__(self, symbols: List[str], max_bars=100):
        self.symbols = symbols
        self.max_bars = max_bars
        # {symbol: {interval: {"close":[], "high":[], "low":[], "open":[], "volume":[], "ts":[]}}}
        self._cache: Dict[str, Dict[str, dict]] = {}
        self._last_fetch: Dict[str, float] = {}
        
    async def init(self):
        """初始化所有币种的K线数据"""
        async with aiohttp.ClientSession() as s:
            for sym in self.symbols:
                await self._fetch_klines(s, sym, "1m", 100)
                await self._fetch_klines(s, sym, "5m", 100)
                await self._fetch_klines(s, sym, "15m", 100)
                await self._fetch_klines(s, sym, "1h", 100)
                await asyncio.sleep(0.1)  # rate limit
    
    async def _fetch_klines(self, session, symbol: str, interval: str, limit: int = 100):
        """拉取K线"""
        try:
            contract = symbol.replace("/", "_")
            params = {"contract": contract, "interval": interval, "limit": limit}
            async with session.get(GATE_KLINE, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.json()
            
            if not data or not isinstance(data, list):
                return
            
            closes, highs, lows, opens, volumes, timestamps = [], [], [], [], [], []
            for bar in data:
                ts = float(bar[0])
                opens.append(float(bar[3]))
                closes.append(float(bar[2]))
                highs.append(float(bar[5]))
                lows.append(float(bar[4]))
                volumes.append(float(bar[6]))
                timestamps.append(ts)
            
            if symbol not in self._cache:
                self._cache[symbol] = {}
            self._cache[symbol][interval] = {
                "close": closes, "high": highs, "low": lows,
                "open": opens, "volume": volumes, "ts": timestamps,
            }
        except Exception:
            pass  # 单币种失败不影响其他
    
    async def update(self, symbol: str):
        """增量更新单个币种（只拉最新1根）"""
        now = time.time()
        # 每30秒最多更新一次
        if symbol in self._last_fetch and now - self._last_fetch[symbol] < 30:
            return
        self._last_fetch[symbol] = now
        
        async with aiohttp.ClientSession() as s:
            await self._fetch_klines(s, symbol, "1m", 50)
    
    def get(self, symbol: str, interval: str = "1m") -> dict:
        """获取指定币种的K线数据"""
        return self._cache.get(symbol, {}).get(interval, {})
    
    def get_close(self, symbol: str, interval: str = "1m") -> List[float]:
        return self.get(symbol, interval).get("close", [])
    
    def get_high_low(self, symbol: str, interval: str = "1m"):
        d = self.get(symbol, interval)
        return d.get("high", []), d.get("low", [])
    
    # ── 技术指标（基于K线缓存） ──
    
    def rsi(self, symbol: str, period=14, interval="5m") -> float:
        """RSI(14) 相对强弱指标"""
        closes = self.get_close(symbol, interval)
        if len(closes) < period + 1:
            return 50.0
        
        gains = [max(0, closes[i] - closes[i-1]) for i in range(-period, 0)]
        losses = [max(0, closes[i-1] - closes[i]) for i in range(-period, 0)]
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            return 100.0
        return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
    
    def atr(self, symbol: str, period=14, interval="1m") -> float:
        """ATR(14) 平均真实波幅"""
        highs, lows = self.get_high_low(symbol, interval)
        closes = self.get_close(symbol, interval)
        if len(highs) < period + 1:
            return 0.0
        
        trs = []
        for i in range(-period, 0):
            h, l = highs[i], lows[i]
            pc = closes[i-1] if i > -period else closes[i]
            tr = max(h - l, abs(h - pc), abs(l - pc))
            trs.append(tr)
        return sum(trs) / period
    
    def adx(self, symbol: str, period=14, interval="1m") -> float:
        """ADX(14) 平均趋向指数"""
        highs, lows = self.get_high_low(symbol, interval)
        closes = self.get_close(symbol, interval)
        if len(highs) < period + 1:
            return 20.0
        
        trs, plus_dms, minus_dms = [], [], []
        for i in range(-period-1, -1):
            h, l = highs[i+1], lows[i+1]
            ph, pl = highs[i], lows[i]
            pc = closes[i]
            tr = max(h - l, abs(h - pc), abs(l - pc))
            trs.append(tr)
            up = h - ph
            down = pl - l
            plus_dms.append(up if up > down and up > 0 else 0)
            minus_dms.append(down if down > up and down > 0 else 0)
        
        if not trs:
            return 20.0
        atr = sum(trs) / period
        if atr == 0:
            return 20.0
        plus_di = (sum(plus_dms) / period / atr) * 100
        minus_di = (sum(minus_dms) / period / atr) * 100
        dx = abs(plus_di - minus_di) / (plus_di + minus_di) * 100 if (plus_di + minus_di) else 0
        return dx
    
    def ema(self, symbol: str, period=20, interval="1m") -> float:
        """EMA（简化版SMA）"""
        closes = self.get_close(symbol, interval)
        if len(closes) < period:
            return closes[-1] if closes else 0.0
        return sum(closes[-period:]) / period
    
    def ma_trend(self, symbol: str, fast=9, slow=21, interval="1m") -> str:
        """均线趋势方向"""
        ema_fast = self.ema(symbol, fast, interval)
        ema_slow = self.ema(symbol, slow, interval)
        if ema_fast > ema_slow * 1.001:
            return "up"
        elif ema_fast < ema_slow * 0.999:
            return "down"
        return "flat"
    
    def volatility_pct(self, symbol: str, interval="1m") -> float:
        """当前波动率（ATR/价格）"""
        closes = self.get_close(symbol, interval)
        if not closes:
            return 0.0
        atr_val = self.atr(symbol, 14, interval)
        price = closes[-1]
        return atr_val / price * 100 if price else 0.0


# 全局单例
_kline_cache: KlineCache = None

def get_kline_cache() -> KlineCache:
    return _kline_cache

async def init_kline_cache(symbols: List[str]):
    global _kline_cache
    _kline_cache = KlineCache(symbols)
    await _kline_cache.init()
    return _kline_cache
