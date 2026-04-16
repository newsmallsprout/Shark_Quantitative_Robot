"""
锚定合约（默认 BTC/USDT）大级别趋势：用 1 分钟收 sampling + 双 EMA 分离度区分 STRONG_UP / STRONG_DOWN / STABLE。
供 BetaNeutralHF：强涨时禁止山寨做空、强跌时禁止山寨做多；震荡时双向。
"""
from __future__ import annotations

import time
from typing import Dict, List, Literal, Optional

Bias = Literal["STRONG_UP", "STRONG_DOWN", "STABLE"]
Micro = Literal["UPTREND", "DOWNTREND", "FLAT"]


def _ema_last(values: List[float], span: int) -> float:
    if not values or span <= 0:
        return float(values[-1]) if values else 0.0
    k = 2.0 / (float(span) + 1.0)
    e = float(values[0])
    for x in values[1:]:
        e = float(x) * k + e * (1.0 - k)
    return float(e)


# minute_bucket -> last price seen in that minute
_minute_close: Dict[int, float] = {}
_MAX_MINUTES = 450


def feed_anchor_minute_close(anchor_symbol: str, price: float, ts: Optional[float] = None) -> None:
    """写入锚定标的分钟收（同一分钟内覆盖为最新价）。"""
    if not anchor_symbol or float(price) <= 0:
        return
    t = float(ts if ts is not None else time.time())
    b = int(t // 60)
    _minute_close[b] = float(price)
    keys = sorted(_minute_close.keys())
    for k in keys[: max(0, len(keys) - _MAX_MINUTES)]:
        _minute_close.pop(k, None)


def get_anchor_trend_bias(
    *,
    fast_min: int = 15,
    slow_min: int = 60,
    strong_sep_bps: float = 28.0,
    min_minutes: int = 65,
) -> Bias:
    """
    在最近 up to 400 根分钟收上算两条 EMA（span=fast/slow 分钟），
    若 (ema_fast/ema_slow-1) 超过 strong_sep_bps → STRONG_UP，低于负对称 → STRONG_DOWN，否则 STABLE。
    """
    if len(_minute_close) < 10:
        return "STABLE"
    keys = sorted(_minute_close.keys())
    if len(keys) < min_minutes:
        return "STABLE"
    closes = [_minute_close[k] for k in keys[-min(300, len(keys)) :]]
    if len(closes) < min_minutes:
        return "STABLE"
    ef = _ema_last(closes, max(3, int(fast_min)))
    es = _ema_last(closes, max(5, int(slow_min)))
    if es <= 0 or ef <= 0:
        return "STABLE"
    sep_bps = (ef / es - 1.0) * 1e4
    thr = float(strong_sep_bps)
    if sep_bps > thr:
        return "STRONG_UP"
    if sep_bps < -thr:
        return "STRONG_DOWN"
    return "STABLE"


def reset_anchor_trend_debug() -> None:
    """单测 / 回放用：清空分钟序列。"""
    _minute_close.clear()


# ---------- Per-symbol 1m closes (ALT / 任意合约) ----------
_symbol_minute: Dict[str, Dict[int, float]] = {}
_SYM_MAX_MIN = 320


def feed_symbol_minute_close(symbol: str, price: float, ts: Optional[float] = None) -> None:
    """写入某标的分钟收，供 EMA20 / 与 15 分钟前价比."""
    sym = str(symbol or "").strip()
    if not sym or float(price) <= 0:
        return
    t = float(ts if ts is not None else time.time())
    b = int(t // 60.0)
    bucket = _symbol_minute.setdefault(sym, {})
    bucket[b] = float(price)
    ks = sorted(bucket.keys())
    for k in ks[: max(0, len(ks) - _SYM_MAX_MIN)]:
        bucket.pop(k, None)


def get_symbol_micro_trend(
    symbol: str,
    *,
    ema_minutes: int = 20,
    lookback_minutes: int = 15,
    min_bars: int = 24,
    bps_confirm: float = 8.0,
) -> Micro:
    """
    微观趋势（1m 收）：
    - UPTREND: 最后价高于 EMA(ema) 且高于 lookback 分钟前价（二者均需小幅确认 bps）
    - DOWNTREND: 对称
    - FLAT: 数据不足或未跨过阈值
    """
    sym = str(symbol or "").strip()
    bucket = _symbol_minute.get(sym) or {}
    if len(bucket) < int(min_bars):
        return "FLAT"
    keys = sorted(bucket.keys())
    closes = [bucket[k] for k in keys[-min(300, len(keys)) :]]
    if len(closes) < int(min_bars):
        return "FLAT"
    last = float(closes[-1])
    span = max(3, int(ema_minutes))
    ema = _ema_last(closes, span)
    lb = max(1, int(lookback_minutes))
    old = float(closes[-lb - 1]) if len(closes) > lb else float(closes[0])
    thr = float(bps_confirm) / 1e4
    if last > ema * (1.0 + thr) and last > old * (1.0 + thr * 0.5):
        return "UPTREND"
    if last < ema * (1.0 - thr) and last < old * (1.0 - thr * 0.5):
        return "DOWNTREND"
    return "FLAT"


def reset_symbol_trends_debug() -> None:
    """单测 / 回放：清空各标的分钟序列。"""
    _symbol_minute.clear()

