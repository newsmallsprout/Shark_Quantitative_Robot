"""
前 N 档盘口失衡度 OBI = (sum bid_sz - sum ask_sz) / (sum bid_sz + sum ask_sz)。
用于激进 Taker 前最后一道微观门：买盘堆积、卖单变薄 → OBI 显著为正利好做多吃单。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence


def _sum_levels(levels: Sequence[Any], top_n: int, size_index: int = 1) -> float:
    s = 0.0
    for row in (levels or [])[: max(0, int(top_n))]:
        if isinstance(row, (list, tuple)) and len(row) > size_index:
            try:
                s += abs(float(row[size_index]))
            except (TypeError, ValueError):
                continue
        elif isinstance(row, dict):
            try:
                s += abs(float(row.get("s", row.get("size", 0)) or 0))
            except (TypeError, ValueError):
                continue
    return float(s)


def calc_obi(
    bids: Sequence[Any],
    asks: Sequence[Any],
    *,
    top_n: int = 5,
) -> float:
    """与 StrategyEngine.process_ws_orderbook 中公式一致。"""
    bv = _sum_levels(bids, top_n)
    av = _sum_levels(asks, top_n)
    if bv + av <= 1e-18:
        return 0.0
    return float(bv - av) / float(bv + av)


def obi_from_orderbook_dict(ob: Optional[Dict[str, Any]], *, top_n: int = 5) -> float:
    if not ob or not isinstance(ob, dict):
        return 0.0
    return calc_obi(ob.get("bids") or [], ob.get("asks") or [], top_n=top_n)


def taker_direction_allowed(
    obi: float,
    side: str,
    *,
    min_abs: float = 0.28,
) -> bool:
    """side=buy → 需要 OBI>=+阈值（买盘占优）；side=sell 新开空 → OBI<=-阈值。"""
    thr = max(0.0, float(min_abs))
    s = str(side or "").lower()
    if s == "buy":
        return obi >= thr
    if s == "sell":
        return obi <= -thr
    return True
