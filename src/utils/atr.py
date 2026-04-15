"""ATR (Average True Range) from OHLC candles (price units)."""

from typing import Any, Dict, List, Tuple


def true_range(high: float, low: float, prev_close: float) -> float:
    if prev_close <= 0:
        return max(high - low, 0.0)
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def compute_atr_from_candles(candles: List[Dict[str, Any]], period: int = 14) -> float:
    """
    Wilder-style ATR: first TR average, then smoothed.
    Candles: dicts with high, low, close (and optional open); sorted ascending by time.
    Returns last ATR in price units, or 0 if insufficient data.
    """
    if period < 1 or len(candles) < period + 1:
        return 0.0

    def _hlc(i: int) -> Tuple[float, float, float]:
        c = candles[i]
        return float(c.get("high", 0) or 0), float(c.get("low", 0) or 0), float(c.get("close", 0) or 0)

    trs: List[float] = []
    for i in range(1, len(candles)):
        h, l, _ = _hlc(i)
        _, _, pc = _hlc(i - 1)
        trs.append(true_range(h, l, pc))

    if len(trs) < period:
        return 0.0

    atr = sum(trs[:period]) / period
    for j in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[j]) / period
    return max(atr, 0.0)
