"""
阿尔法掠食者矩阵：成交量分布(POC/VA)、流动性清扫、资金费率×OBI 背离、波动率压缩突破。
纯函数 + 配置，供 CoreAttack 调用。
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import math

from src.ai.regime import MarketRegime


def _candles_sorted(candles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(candles, key=lambda x: int(x.get("time", 0) or 0))


def _vol(c) -> float:
    v = float(c.get("volume", 0) or 0)
    if v > 0:
        return v
    h, l = float(c.get("high", 0) or 0), float(c.get("low", 0) or 0)
    return max(h - l, 1e-12)


def volume_profile_poc_vah_val(
    candles: List[Dict[str, Any]],
    bins: int = 32,
    value_area_pct: float = 0.70,
) -> Optional[Tuple[float, float, float, float]]:
    """
    将每根 K 的成交量在 [low, high] 区间均匀摊到价格桶，得到 POC / VAL / VAH。
    返回 (poc, vah, val, bin_width)；数据不足返回 None。
    """
    if not candles or bins < 8:
        return None
    cs = _candles_sorted(candles)
    if len(cs) < 10:
        return None

    lo = min(float(c["low"]) for c in cs)
    hi = max(float(c["high"]) for c in cs)
    if hi <= lo:
        return None
    bw = (hi - lo) / float(bins)
    bucket = [0.0] * bins
    for c in cs:
        h, l = float(c["high"]), float(c["low"])
        v = _vol(c)
        if h <= l:
            idx = min(bins - 1, max(0, int((0.5 * (h + l) - lo) / max(bw, 1e-12))))
            bucket[idx] += v
            continue
        i0 = int((l - lo) / bw)
        i1 = int((h - lo) / bw)
        i0 = max(0, min(bins - 1, i0))
        i1 = max(0, min(bins - 1, i1))
        if i0 > i1:
            i0, i1 = i1, i0
        span = i1 - i0 + 1
        per = v / span
        for i in range(i0, i1 + 1):
            bucket[i] += per

    total = sum(bucket)
    if total <= 0:
        return None
    peak_i = max(range(bins), key=lambda i: bucket[i])
    poc = lo + (peak_i + 0.5) * bw

    target = total * float(value_area_pct)
    acc = bucket[peak_i]
    lo_i = hi_i = peak_i
    while acc < target and (lo_i > 0 or hi_i < bins - 1):
        left = bucket[lo_i - 1] if lo_i > 0 else -1.0
        right = bucket[hi_i + 1] if hi_i < bins - 1 else -1.0
        if right >= left:
            if hi_i < bins - 1:
                hi_i += 1
                acc += bucket[hi_i]
            elif lo_i > 0:
                lo_i -= 1
                acc += bucket[lo_i]
            else:
                break
        else:
            if lo_i > 0:
                lo_i -= 1
                acc += bucket[lo_i]
            elif hi_i < bins - 1:
                hi_i += 1
                acc += bucket[hi_i]
            else:
                break

    val = lo + lo_i * bw
    vah = lo + (hi_i + 1) * bw
    return poc, vah, val, bw


def vp_structure_scores(price: float, poc: float, vah: float, val: float) -> Tuple[float, float]:
    """价格相对价值区的多空技术分 0–100（与旧 SMA 分同量级）。"""
    if poc <= 0:
        return 0.0, 0.0
    eps = max(poc * 0.0005, 1e-8)
    bull = 50.0
    bear = 50.0
    if price < val - eps:
        bull = 72.0
        bear = 35.0
    elif price > vah + eps:
        bull = 68.0
        bear = 38.0
    elif val <= price <= poc:
        bull = 58.0
        bear = 45.0
    elif poc < price <= vah:
        bull = 55.0
        bear = 48.0
    else:
        d = abs(price - poc) / poc
        bull = min(65.0, 50.0 + d * 5000.0)
        bear = min(65.0, 50.0 + d * 5000.0)
    return bull, bear


def liquidity_sweep_long(
    candles: List[Dict[str, Any]], obi: float, lookback: int, obi_floor: float
) -> bool:
    """假跌破：前低被刺穿后收回，且盘口未恐慌 (OBI 未深负)。"""
    cs = _candles_sorted(candles)
    if len(cs) < lookback + 3:
        return False
    body = cs[-(lookback + 2) : -1]
    if len(body) < lookback:
        return False
    swing_low = min(float(x["low"]) for x in body)
    last = cs[-1]
    low_last = float(last["low"])
    close_last = float(last["close"])
    if swing_low <= 0:
        return False
    swept = low_last < swing_low * 0.9995
    reclaim = close_last > swing_low
    calm_book = obi >= obi_floor
    return swept and reclaim and calm_book


def liquidity_sweep_short(
    candles: List[Dict[str, Any]], obi: float, lookback: int, obi_ceiling: float
) -> bool:
    cs = _candles_sorted(candles)
    if len(cs) < lookback + 3:
        return False
    body = cs[-(lookback + 2) : -1]
    swing_high = max(float(x["high"]) for x in body)
    last = cs[-1]
    high_last = float(last["high"])
    close_last = float(last["close"])
    swept = high_last > swing_high * 1.0005
    reclaim = close_last < swing_high
    calm_book = obi <= obi_ceiling
    return swept and reclaim and calm_book


def funding_obi_divergence_points(
    funding_rate: float,
    obi: float,
    funding_neg_extreme: float,
    funding_pos_extreme: float,
    obi_min_align: float,
    boost: float,
) -> Tuple[float, float]:
    """
    费率极端与盘口背离加分。
    负费率(空头付多头) + 买盘 OBI → bull_boost；正费率 + 卖盘 OBI → bear_boost。
    """
    bull = 0.0
    bear = 0.0
    if funding_rate <= funding_neg_extreme and obi >= obi_min_align:
        bull = boost
    if funding_rate >= funding_pos_extreme and obi <= -obi_min_align:
        bear = boost
    return bull, bear


def volatility_squeeze_breakout(
    candles: List[Dict[str, Any]],
    bb_period: int = 20,
    bbw_percentile: float = 0.05,
    volume_mult: float = 1.2,
    donchian_lookback: int = 15,
    bbw_history: int = 120,
) -> int:
    """
    布林带带宽处于近期极低分位（压缩）+ 成交量放大 + Donchian 突破。
    返回 +1 多 / -1 空 / 0 无。
    """
    cs = _candles_sorted(candles)
    need = max(bb_period + 3, donchian_lookback + 3, 40)
    if len(cs) < need:
        return 0

    closes = [float(c["close"]) for c in cs]
    highs = [float(c["high"]) for c in cs]
    lows = [float(c["low"]) for c in cs]
    vols = [_vol(c) for c in cs]
    n = len(closes)

    bbw_series: List[Tuple[int, float]] = []
    for i in range(bb_period - 1, n):
        chunk = closes[i - bb_period + 1 : i + 1]
        m = sum(chunk) / bb_period
        if m <= 0:
            continue
        var = sum((x - m) ** 2 for x in chunk) / bb_period
        sd = math.sqrt(max(var, 0.0))
        bbw = (4.0 * sd) / m
        bbw_series.append((i, bbw))

    if len(bbw_series) < 30:
        return 0

    last_i, last_bbw = bbw_series[-1]
    hist = [b for _, b in bbw_series[-bbw_history:]]
    hist_sorted = sorted(hist)
    pk = int(len(hist_sorted) * bbw_percentile)
    pk = max(0, min(len(hist_sorted) - 1, pk))
    thr = hist_sorted[pk]
    squeeze = last_bbw <= thr * 1.05
    if not squeeze:
        return 0

    i = last_i
    if i < 20:
        return 0
    vma = sum(vols[i - 20 : i]) / 20.0
    if vols[i] < volume_mult * max(vma, 1e-12):
        return 0

    start = max(0, i - donchian_lookback)
    hh = max(highs[start:i])
    ll = min(lows[start:i])
    if closes[i] > hh:
        return 1
    if closes[i] < ll:
        return -1
    return 0


def normalize_attack_weights(ai: float, tech: float, obi: float) -> Tuple[float, float, float]:
    s = ai + tech + obi
    if s <= 1e-9:
        return 0.4, 0.3, 0.3
    return ai / s, tech / s, obi / s


def weights_for_regime(regime: MarketRegime, regime_weights: Dict[str, Any]) -> Tuple[float, float, float]:
    """regime_weights: name -> {ai, tech, obi}"""
    key = regime.value
    w = regime_weights.get(key) or regime_weights.get("DEFAULT")
    if w is None:
        return 0.4, 0.3, 0.3
    if hasattr(w, "ai"):
        return normalize_attack_weights(float(w.ai), float(w.tech), float(w.obi))
    if isinstance(w, dict):
        return normalize_attack_weights(
            float(w.get("ai", 0.4)),
            float(w.get("tech", 0.3)),
            float(w.get("obi", 0.3)),
        )
    return 0.4, 0.3, 0.3


def combine_tech_scores(
    sma_bull: float,
    sma_bear: float,
    vp_bull: float,
    vp_bear: float,
    vp_weight: float = 0.55,
) -> Tuple[float, float]:
    """SMA 与成交量分布结构混合。"""
    w = max(0.0, min(1.0, vp_weight))
    bull = (1.0 - w) * sma_bull + w * vp_bull
    bear = (1.0 - w) * sma_bear + w * vp_bear
    return bull, bear
