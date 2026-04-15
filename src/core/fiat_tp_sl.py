"""
绝对金额 ↔ 价格跳动：固定目标净利 / 最大净亏（USDT）下，按张数×合约面值反推 TP/SL 价格，
并显式计入开/平双边手续费（与 Gate USDT 线性永续 PnL = 张×CS×ΔP 一致）。
"""

from __future__ import annotations

from typing import Literal, Tuple

Side = Literal["long", "short"]


def usdt_pnl_per_unit_price(contracts: float, contract_size: float) -> float:
    """价格每上涨 1 USDT（标的计价）时，多头的 USDT 毛利（不含费）。"""
    return max(float(contracts), 0.0) * max(float(contract_size), 0.0)


def compute_tp_sl_prices_net_usdt(
    side: Side,
    entry_px: float,
    contracts: float,
    contract_size: float,
    *,
    target_net_usdt: float,
    risk_net_usdt: float,
    fee_open_usdt: float,
    fee_close_tp_usdt: float,
    fee_close_sl_usdt: float,
) -> Tuple[float, float]:
    """
    多头：
      净利 +target = +(P_tp-P_e)*Q*CS - fee_open - fee_close_tp
      => P_tp = P_e + (target + fee_open + fee_close_tp) / (Q*CS)
      净亏 -risk = +(P_sl-P_e)*Q*CS - fee_open - fee_close_sl  (P_sl<P_e)
      => P_e - P_sl = (risk - fee_open - fee_close_sl) / (Q*CS)
    空头对称。
    """
    entry_px = float(entry_px)
    if entry_px <= 0:
        raise ValueError("entry_px must be positive")
    q = usdt_pnl_per_unit_price(contracts, contract_size)
    if q <= 1e-18:
        raise ValueError("contracts*contract_size too small")

    tgt = max(float(target_net_usdt), 0.0)
    rsk = max(float(risk_net_usdt), 0.0)
    fo = max(float(fee_open_usdt), 0.0)
    fctp = max(float(fee_close_tp_usdt), 0.0)
    fcsl = max(float(fee_close_sl_usdt), 0.0)

    if side == "long":
        d_tp = (tgt + fo + fctp) / q
        inner = rsk - fo - fcsl
        if inner <= 1e-12:
            inner = max(rsk * 0.5, 1e-8)
        d_sl = inner / q
        return entry_px + d_tp, entry_px - d_sl
    else:
        d_tp = (tgt + fo + fctp) / q
        inner = rsk - fo - fcsl
        if inner <= 1e-12:
            inner = max(rsk * 0.5, 1e-8)
        d_sl = inner / q
        return entry_px - d_tp, entry_px + d_sl


def estimate_fees_usdt(
    notional_entry: float,
    notional_exit_tp: float,
    notional_exit_sl: float,
    *,
    taker_rate: float,
    maker_rate: float,
    tp_as_maker: bool = True,
    sl_as_taker: bool = True,
) -> Tuple[float, float, float]:
    """返回 (fee_open, fee_close_tp, fee_close_sl)，均为正 USDT。"""
    fo = abs(float(notional_entry)) * max(float(taker_rate), 0.0)
    rt = max(float(taker_rate), 0.0)
    rm = max(float(maker_rate), 0.0)
    fctp = abs(float(notional_exit_tp)) * (rm if tp_as_maker else rt)
    fcsl = abs(float(notional_exit_sl)) * (rt if sl_as_taker else rm)
    return fo, fctp, fcsl
