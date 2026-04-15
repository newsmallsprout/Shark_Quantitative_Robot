"""
刺客成本感知：动态 Hurdle（双边费 + 相对价差）与净利润止盈校验。
"""

from __future__ import annotations

from typing import Any, Tuple


def assassin_hurdle_rate(symbol: str, pe: Any) -> float:
    """
    Hurdle = fee_taker + fee_maker + (ask - bid) / mid
    全部为小数形式（0.001 = 0.1%）。
    """
    if hasattr(pe, "_fee_rates_for_symbol"):
        taker, maker = pe._fee_rates_for_symbol(symbol)
    else:
        taker = float(getattr(pe, "taker_fee", 0.0) or 0.0)
        maker = float(getattr(pe, "maker_fee", 0.0) or 0.0)
    spread_frac = 0.0
    try:
        bb, ba = pe._best_bid_ask(symbol)
        if bb > 0 and ba > 0 and ba >= bb:
            mid = 0.5 * (bb + ba)
            if mid > 0:
                spread_frac = (ba - bb) / mid
    except Exception:
        pass
    return max(0.0, taker + maker + spread_frac)


def reversion_space_fraction_long(last: float, vwap: float) -> float:
    if last <= 0 or vwap <= last:
        return 0.0
    return (vwap - last) / last


def reversion_space_fraction_short(last: float, vwap: float) -> float:
    if last <= 0 or vwap >= last:
        return 0.0
    return (last - vwap) / last


def long_hard_tp_price(entry: float, hurdle: float, target_net_frac: float) -> float:
    return entry * (1.0 + max(0.0, hurdle) + max(0.0, target_net_frac))


def short_hard_tp_price(entry: float, hurdle: float, target_net_frac: float) -> float:
    return entry * (1.0 - max(0.0, hurdle) - max(0.0, target_net_frac))


def long_entry_net_floor_ok(
    last: float,
    vwap: float,
    hurdle: float,
    net_space_mult: float,
    min_dev_frac: float,
    target_net_frac: float,
) -> Tuple[bool, str]:
    """净利润保底：回归空间 > max(hurdle*mult, min_dev_frac)，且硬止盈价 < VWAP。"""
    if last <= 0 or vwap <= 0:
        return False, "bad_price"
    space = reversion_space_fraction_long(last, vwap)
    need = max(float(hurdle) * float(net_space_mult), float(min_dev_frac))
    if space <= need:
        return False, "garbage_volatility"
    tp_probe = long_hard_tp_price(last, hurdle, target_net_frac)
    if tp_probe >= vwap:
        return False, "tp_ge_vwap"
    return True, "ok"


def short_entry_net_floor_ok(
    last: float,
    vwap: float,
    hurdle: float,
    net_space_mult: float,
    min_dev_frac: float,
    target_net_frac: float,
) -> Tuple[bool, str]:
    if last <= 0 or vwap <= 0:
        return False, "bad_price"
    space = reversion_space_fraction_short(last, vwap)
    need = max(float(hurdle) * float(net_space_mult), float(min_dev_frac))
    if space <= need:
        return False, "garbage_volatility"
    tp_probe = short_hard_tp_price(last, hurdle, target_net_frac)
    if tp_probe <= vwap:
        return False, "tp_le_vwap"
    return True, "ok"
