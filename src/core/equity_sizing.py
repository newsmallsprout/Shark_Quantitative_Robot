"""
按账户权益（USDT）分档：单笔保证金占权益比例区间 + 狂鲨净利止盈/止损美元缩放。

规则（与产品约定对齐）：
- 权益 < 1000：保证金占比 10%–20%
- 1000 ≤ 权益 < 10000：上限自 10% 线性降至 5%，下限 5%
- 权益 ≥ 10000：3%–5%

止盈/止损净利（USDT）：以「100U 权益 → +1U 止盈 / -0.5U 止损」为比例，
即 target_net ≈ 权益×tp_net_equity_fraction（默认 1%），risk_net ≈ 权益×sl_net_equity_fraction（默认 0.5%）。
"""

from __future__ import annotations

from typing import Tuple


def margin_bounds_for_equity(equity: float) -> Tuple[float, float]:
    """返回 (min_frac, max_frac)，单笔初始保证金 / 权益。"""
    e = max(float(equity), 1e-9)
    if e < 1000.0:
        return (0.10, 0.20)
    if e < 10000.0:
        t = (e - 1000.0) / 9000.0
        mx = 0.10 + t * (0.05 - 0.10)
        return (0.05, mx)
    return (0.03, 0.05)


def margin_cap_fraction(equity: float) -> float:
    """风控单笔上限（不超过区间内 max）。"""
    return margin_bounds_for_equity(equity)[1]


def margin_target_fraction(equity: float) -> float:
    """Grinder 开单默认取区间中点，在上下限之间。"""
    lo, hi = margin_bounds_for_equity(equity)
    return (lo + hi) / 2.0


def shark_net_tp_sl_usdt(
    equity: float,
    tp_equity_fraction: float,
    sl_equity_fraction: float,
) -> Tuple[float, float]:
    """狂鲨括号：净利目标 / 可承受亏损（USDT）随权益线性缩放。"""
    e = max(float(equity), 1e-9)
    return (
        max(e * float(tp_equity_fraction), 0.0),
        max(e * float(sl_equity_fraction), 0.0),
    )
