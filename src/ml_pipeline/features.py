from __future__ import annotations

from typing import Dict, Iterable, List, Sequence


def _to_float(v: object, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _sma(values: Sequence[float], end_idx: int, window: int) -> float:
    start = max(0, end_idx - window + 1)
    chunk = values[start : end_idx + 1]
    return sum(chunk) / max(len(chunk), 1)


def compute_atr(ohlcv: Sequence[Dict[str, object]], period: int = 14) -> List[float]:
    highs = [_to_float(x.get("high")) for x in ohlcv]
    lows = [_to_float(x.get("low")) for x in ohlcv]
    closes = [_to_float(x.get("close")) for x in ohlcv]

    trs: List[float] = []
    for i in range(len(ohlcv)):
        high = highs[i]
        low = lows[i]
        prev_close = closes[i - 1] if i > 0 else closes[i]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(max(tr, 0.0))

    atr: List[float] = []
    running = 0.0
    for i, tr in enumerate(trs):
        running += tr
        if i >= period:
            running -= trs[i - period]
        win = min(i + 1, period)
        atr.append(running / max(win, 1))
    return atr


def _relative_volume(volumes: Sequence[float], idx: int, window: int) -> float:
    curr = volumes[idx]
    baseline = _sma(volumes, idx, window)
    if baseline <= 1e-12:
        return 0.0
    return curr / baseline - 1.0


def _pseudo_obi(open_: float, high: float, low: float, close: float, vol_spike: float) -> float:
    rng = max(high - low, 1e-12)
    body_bias = (close - open_) / rng
    upper_wick = max(high - max(open_, close), 0.0)
    lower_wick = max(min(open_, close) - low, 0.0)
    wick_bias = (lower_wick - upper_wick) / rng
    volume_boost = max(min(vol_spike, 3.0), -1.0)
    return 0.65 * body_bias + 0.25 * wick_bias + 0.10 * volume_boost


def extract_features(
    ohlcv: Sequence[Dict[str, object]],
    *,
    atr_period: int = 14,
    ma_fast: int = 5,
    ma_slow: int = 15,
    vol_fast: int = 5,
    vol_slow: int = 15,
) -> List[Dict[str, float]]:
    """
    输入原始 OHLCV，输出可直接喂给训练器的特征序列。

    关键特征：
    - ATR 归一化的均线偏离
    - 5m/15m 相对成交量突变率
    - Pseudo-OBI（基于实体/影线/量能）
    """
    opens = [_to_float(x.get("open")) for x in ohlcv]
    highs = [_to_float(x.get("high")) for x in ohlcv]
    lows = [_to_float(x.get("low")) for x in ohlcv]
    closes = [_to_float(x.get("close")) for x in ohlcv]
    volumes = [_to_float(x.get("volume")) for x in ohlcv]
    ts = [int(_to_float(x.get("time"), 0.0)) for x in ohlcv]
    atr = compute_atr(ohlcv, period=atr_period)

    rows: List[Dict[str, float]] = []
    for i in range(len(ohlcv)):
        close = closes[i]
        atr_i = max(atr[i], 1e-12)
        ma5 = _sma(closes, i, ma_fast)
        ma15 = _sma(closes, i, ma_slow)
        dist_ma5_atr = (close - ma5) / atr_i
        dist_ma15_atr = (close - ma15) / atr_i
        vol_spike_5 = _relative_volume(volumes, i, vol_fast)
        vol_spike_15 = _relative_volume(volumes, i, vol_slow)
        pseudo_obi = _pseudo_obi(opens[i], highs[i], lows[i], close, vol_spike_5)
        atr_pct = atr_i / max(close, 1e-12)

        rows.append(
            {
                "time": float(ts[i]),
                "open": opens[i],
                "high": highs[i],
                "low": lows[i],
                "close": close,
                "volume": volumes[i],
                "atr": atr_i,
                "atr_pct": atr_pct,
                "dist_ma5_atr": dist_ma5_atr,
                "dist_ma15_atr": dist_ma15_atr,
                "volume_spike_5m": vol_spike_5,
                "volume_spike_15m": vol_spike_15,
                "pseudo_obi": pseudo_obi,
            }
        )
    return rows


def extract_features_from_iterable(rows: Iterable[Dict[str, object]]) -> List[Dict[str, float]]:
    return extract_features(list(rows))

