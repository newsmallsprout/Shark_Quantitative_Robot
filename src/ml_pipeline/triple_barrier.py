from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from src.core.config_manager import config_manager
from src.ml_pipeline.features import compute_atr


def _as_price_row(x: Dict[str, object]) -> Tuple[float, float, float, float]:
    return (
        float(x.get("open", 0.0) or 0.0),
        float(x.get("high", 0.0) or 0.0),
        float(x.get("low", 0.0) or 0.0),
        float(x.get("close", 0.0) or 0.0),
    )


def resolve_engine_pt_sl(pt_sl: Optional[Sequence[float]] = None) -> Tuple[float, float]:
    """
    与 engine.py 的 ATR 止盈止损保持一致。
    默认：pt=execution.sniper_atr_tp_mult, sl=execution.sniper_atr_sl_mult
    """
    if pt_sl and len(pt_sl) >= 2:
        return float(pt_sl[0]), float(pt_sl[1])
    ex = config_manager.get_config().execution
    return float(ex.sniper_atr_tp_mult), float(ex.sniper_atr_sl_mult)


def triple_barrier_labels(
    ohlcv: Sequence[Dict[str, object]],
    *,
    atr_values: Optional[Sequence[float]] = None,
    pt_sl: Optional[Sequence[float]] = None,
    t1: int = 15,
    side: str = "long",
) -> List[Dict[str, object]]:
    """
    Triple-Barrier 打标：
    - 先触发 TP -> 1
    - 先触发 SL -> -1
    - 时间屏障耗尽 -> 0
    """
    n = len(ohlcv)
    if n == 0:
        return []
    atr = list(atr_values) if atr_values is not None else compute_atr(ohlcv)
    pt_mult, sl_mult = resolve_engine_pt_sl(pt_sl)
    is_long = str(side).lower() not in {"short", "sell"}

    labels: List[Dict[str, object]] = []
    for i in range(n):
        _, _, _, entry_px = _as_price_row(ohlcv[i])
        atr_i = float(atr[i] if i < len(atr) else 0.0)
        if entry_px <= 0 or atr_i <= 0:
            labels.append(
                {
                    "index": i,
                    "label": 0,
                    "exit_index": i,
                    "exit_reason": "invalid_input",
                    "entry_price": entry_px,
                    "upper_barrier": None,
                    "lower_barrier": None,
                }
            )
            continue

        if is_long:
            upper = entry_px + pt_mult * atr_i
            lower = entry_px - sl_mult * atr_i
        else:
            upper = entry_px - pt_mult * atr_i
            lower = entry_px + sl_mult * atr_i

        horizon = min(n - 1, i + max(int(t1), 1))
        label = 0
        exit_idx = horizon
        exit_reason = "time_stop"

        for j in range(i + 1, horizon + 1):
            _, high_j, low_j, _ = _as_price_row(ohlcv[j])
            if is_long:
                if high_j >= upper:
                    label = 1
                    exit_idx = j
                    exit_reason = "take_profit"
                    break
                if low_j <= lower:
                    label = -1
                    exit_idx = j
                    exit_reason = "stop_loss"
                    break
            else:
                if low_j <= upper:
                    label = 1
                    exit_idx = j
                    exit_reason = "take_profit"
                    break
                if high_j >= lower:
                    label = -1
                    exit_idx = j
                    exit_reason = "stop_loss"
                    break

        labels.append(
            {
                "index": i,
                "label": label,
                "exit_index": exit_idx,
                "exit_reason": exit_reason,
                "entry_price": entry_px,
                "upper_barrier": upper,
                "lower_barrier": lower,
                "t1_index": horizon,
                "atr": atr_i,
                "pt_mult": pt_mult,
                "sl_mult": sl_mult,
            }
        )
    return labels

